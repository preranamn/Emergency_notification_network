"""Ryu OpenFlow 1.3 SDN Controller for Priority Emergency Notification Network.

Compatible with Ryu 4.34 (Python 3.8+) and Open vSwitch OpenFlow 1.3.

Responsibilities:
- Manage Ingress Switch S1 (dpid=1) and Egress Switch S2 (dpid=2).
- Install DSCP-based QoS OpenFlow 1.3 forwarding rules:
    * DSCP 46 (EF  - MEDICAL) -> OF priority 300 -> OVS Queue 1 (Highest)
    * DSCP 34 (AF41 - FIRE)   -> OF priority 200 -> OVS Queue 2 (Medium)
    * DSCP 0  (BE  - GENERAL) -> OF priority 100 -> OVS Queue 3 (Normal / Best-Effort)
- Perform L2 MAC learning for ARP and control traffic.
- Poll live OpenFlow Flow, Queue, and Port statistics every second.
- Expose a REST API (default port 8080) for the Flask backend and NOC frontend.
"""

import json
import time
from typing import Any, Dict, List

from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ethernet, ether_types, ipv4, packet
from ryu.ofproto import ofproto_v1_3


SDN_INSTANCE_NAME = "emergency_sdn_app"

# DSCP -> (Queue ID, OpenFlow Rule Priority, Category Label)
DSCP_QOS_POLICY = {
    46: (1, 300, "MEDICAL"),
    34: (2, 200, "FIRE"),
    0: (3, 100, "GENERAL"),
}

# Deterministic Mininet topology routing table:
# dpid -> { dest_ipv4: out_port }
STATIC_IP_PORTS = {
    1: {  # S1 (Ingress Switch)
        "10.0.0.1": 1,  # H1 Server
        "10.0.0.5": 2,  # H5 Background Traffic Generator
        "10.0.0.2": 3,  # H2 via S1-S2 trunk (port 3)
        "10.0.0.3": 3,  # H3 via S1-S2 trunk (port 3)
        "10.0.0.4": 3,  # H4 via S1-S2 trunk (port 3)
        "10.0.0.6": 3,  # H6 via S1-S2 trunk (port 3)
    },
    2: {  # S2 (Egress Switch)
        "10.0.0.1": 1,  # H1 via S2-S1 trunk (port 1)
        "10.0.0.5": 1,  # H5 via S2-S1 trunk (port 1)
        "10.0.0.2": 2,  # H2 Client 1
        "10.0.0.3": 3,  # H3 Client 2
        "10.0.0.4": 4,  # H4 Client 3
        "10.0.0.6": 5,  # H6 Background Traffic Sink
    },
}

SWITCH_NAMES = {1: "S1", 2: "S2"}


class EmergencySDNController(app_manager.RyuApp):
    """OpenFlow 1.3 QoS Controller for the Emergency Notification Network."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(EmergencySDNController, self).__init__(*args, **kwargs)
        self.mac_to_port: Dict[int, Dict[str, int]] = {}
        self.datapaths: Dict[int, Any] = {}
        self.flow_stats: Dict[int, List[Dict[str, Any]]] = {}
        self.queue_stats: Dict[int, List[Dict[str, Any]]] = {}
        self.port_stats: Dict[int, List[Dict[str, Any]]] = {}
        self.started_at = time.time()
        self.last_stats_update = 0.0

        wsgi = kwargs["wsgi"]
        wsgi.register(EmergencySDNRestController, {SDN_INSTANCE_NAME: self})

        self.monitor_thread = hub.spawn(self._stats_monitor)
        self.logger.info("EmergencySDNController initialized (OpenFlow 1.3 + REST API)")

    # ------------------------------------------------------------------
    # OpenFlow Helpers
    # ------------------------------------------------------------------
    def add_flow(
        self,
        datapath: Any,
        priority: int,
        match: Any,
        actions: List[Any],
        idle_timeout: int = 0,
        hard_timeout: int = 0,
        buffer_id: Any = None,
    ) -> None:
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id is not None:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                priority=priority,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                match=match,
                instructions=inst,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                match=match,
                instructions=inst,
            )
        datapath.send_msg(mod)

    def _install_proactive_qos_flows(self, datapath: Any) -> None:
        """Install deterministic IPv4 DSCP-to-Queue QoS forwarding flows on S1 and S2."""
        dpid = datapath.id
        parser = datapath.ofproto_parser
        port_map = STATIC_IP_PORTS.get(dpid)
        if not port_map:
            return

        installed = 0
        for dst_ip, out_port in port_map.items():
            for dscp, (queue_id, of_prio, _label) in DSCP_QOS_POLICY.items():
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst=dst_ip,
                    ip_dscp=dscp,
                )
                actions = [
                    parser.OFPActionSetQueue(queue_id),
                    parser.OFPActionOutput(out_port),
                ]
                self.add_flow(datapath, priority=of_prio, match=match, actions=actions)
                installed += 1

            # Fallback IPv4 flow (for any non-standard DSCP value -> Normal Queue 3)
            fallback_match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=dst_ip,
            )
            fallback_actions = [
                parser.OFPActionSetQueue(3),
                parser.OFPActionOutput(out_port),
            ]
            self.add_flow(datapath, priority=50, match=fallback_match, actions=fallback_actions)
            installed += 1

        sw_name = SWITCH_NAMES.get(dpid, "S{}".format(dpid))
        self.logger.info("[%s] Installed %d proactive OpenFlow 1.3 DSCP QoS rules", sw_name, installed)

    # ------------------------------------------------------------------
    # OpenFlow Event Handlers
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev: Any) -> None:
        datapath = ev.datapath
        dpid = datapath.id
        if ev.state == MAIN_DISPATCHER:
            if dpid not in self.datapaths:
                self.logger.info("Switch connected: dpid=%s (%s)", dpid, SWITCH_NAMES.get(dpid, "Unknown"))
                self.datapaths[dpid] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                self.logger.info("Switch disconnected: dpid=%s", dpid)
                del self.datapaths[dpid]
                self.flow_stats.pop(dpid, None)
                self.queue_stats.pop(dpid, None)
                self.port_stats.pop(dpid, None)
                self.mac_to_port.pop(dpid, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev: Any) -> None:
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss flow entry -> send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, priority=0, match=match, actions=actions)

        # Proactive DSCP QoS flows for H1..H6
        self._install_proactive_qos_flows(datapath)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev: Any) -> None:
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        # Check IPv4 DSCP if an IPv4 packet hits the controller dynamically
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is not None and out_port != ofproto.OFPP_FLOOD:
            dscp = (ip_pkt.tos >> 2) & 0x3F
            queue_id, of_prio, _label = DSCP_QOS_POLICY.get(dscp, (3, 100, "GENERAL"))
            actions = [parser.OFPActionSetQueue(queue_id), parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=ip_pkt.dst,
                ip_dscp=dscp,
            )
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, of_prio, match, actions, idle_timeout=120, buffer_id=msg.buffer_id)
                return
            self.add_flow(datapath, of_prio, match, actions, idle_timeout=120)
        else:
            actions = [parser.OFPActionOutput(out_port)]
            # Install L2 flow for non-IPv4 (e.g. ARP unicast replies)
            if out_port != ofproto.OFPP_FLOOD and eth.ethertype != ether_types.ETH_TYPE_IP:
                match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_type=eth.ethertype)
                if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                    self.add_flow(datapath, 20, match, actions, idle_timeout=60, buffer_id=msg.buffer_id)
                    return
                self.add_flow(datapath, 20, match, actions, idle_timeout=60)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    # ------------------------------------------------------------------
    # Live Statistics Polling (Flows, Queues, Ports)
    # ------------------------------------------------------------------
    def _stats_monitor(self) -> None:
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(1.0)

    def _request_stats(self, datapath: Any) -> None:
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        try:
            datapath.send_msg(parser.OFPFlowStatsRequest(datapath))
            datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY))
            datapath.send_msg(parser.OFPQueueStatsRequest(datapath, 0, ofproto.OFPP_ANY, ofproto.OFPQ_ALL))
        except Exception as exc:
            self.logger.debug("Stats request failed for dpid=%s: %s", datapath.id, exc)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev: Any) -> None:
        dpid = ev.msg.datapath.id
        flows: List[Dict[str, Any]] = []
        for stat in sorted(ev.msg.body, key=lambda s: -s.priority):
            if stat.priority == 0:
                continue
            match_dict = {}
            for k, v in stat.match.items():
                match_dict[str(k)] = str(v)

            dscp_val = stat.match.get("ip_dscp")
            category = None
            queue_id = None
            if dscp_val is not None and int(dscp_val) in DSCP_QOS_POLICY:
                queue_id, _, category = DSCP_QOS_POLICY[int(dscp_val)]

            flows.append(
                {
                    "switch": SWITCH_NAMES.get(dpid, "S{}".format(dpid)),
                    "dpid": dpid,
                    "priority": stat.priority,
                    "match": match_dict,
                    "category": category,
                    "queue_id": queue_id,
                    "packet_count": stat.packet_count,
                    "byte_count": stat.byte_count,
                    "duration_sec": stat.duration_sec,
                }
            )
        self.flow_stats[dpid] = flows
        self.last_stats_update = time.time()

    @set_ev_cls(ofp_event.EventOFPQueueStatsReply, MAIN_DISPATCHER)
    def _queue_stats_reply_handler(self, ev: Any) -> None:
        dpid = ev.msg.datapath.id
        queues: List[Dict[str, Any]] = []
        for stat in ev.msg.body:
            q_label = {
                0: "DEFAULT",
                1: "MEDICAL",
                2: "FIRE",
                3: "GENERAL",
            }.get(stat.queue_id, "QUEUE_{}".format(stat.queue_id))
            queues.append(
                {
                    "switch": SWITCH_NAMES.get(dpid, "S{}".format(dpid)),
                    "dpid": dpid,
                    "port_no": stat.port_no,
                    "queue_id": stat.queue_id,
                    "label": q_label,
                    "tx_packets": stat.tx_packets,
                    "tx_bytes": stat.tx_bytes,
                    "tx_errors": stat.tx_errors,
                }
            )
        self.queue_stats[dpid] = queues
        self.last_stats_update = time.time()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev: Any) -> None:
        dpid = ev.msg.datapath.id
        ports: List[Dict[str, Any]] = []
        for stat in ev.msg.body:
            if stat.port_no > 100:  # Skip OFPP_LOCAL
                continue
            ports.append(
                {
                    "switch": SWITCH_NAMES.get(dpid, "S{}".format(dpid)),
                    "dpid": dpid,
                    "port_no": stat.port_no,
                    "rx_packets": stat.rx_packets,
                    "tx_packets": stat.tx_packets,
                    "rx_bytes": stat.rx_bytes,
                    "tx_bytes": stat.tx_bytes,
                    "rx_dropped": stat.rx_dropped,
                    "tx_dropped": stat.tx_dropped,
                    "rx_errors": stat.rx_errors,
                    "tx_errors": stat.tx_errors,
                }
            )
        self.port_stats[dpid] = ports
        self.last_stats_update = time.time()


class EmergencySDNRestController(ControllerBase):
    """REST endpoints exposed by Ryu on port 8080."""

    def __init__(self, req: Any, link: Any, data: Dict[str, Any], **config: Any) -> None:
        super(EmergencySDNRestController, self).__init__(req, link, data, **config)
        self.app: EmergencySDNController = data[SDN_INSTANCE_NAME]

    @staticmethod
    def _json_response(payload: Dict[str, Any], status: int = 200) -> Response:
        body = json.dumps(payload)
        return Response(content_type="application/json", body=body.encode("utf-8"), status=status)

    @route("sdn", "/sdn/status", methods=["GET"])
    def get_status(self, _req: Any, **_kwargs: Any) -> Response:
        switches = [
            {"dpid": dpid, "name": SWITCH_NAMES.get(dpid, "S{}".format(dpid)), "connected": True}
            for dpid in sorted(self.app.datapaths.keys())
        ]
        return self._json_response(
            {
                "controller": "Ryu SDN Controller",
                "openflow_version": "1.3",
                "running": True,
                "uptime_sec": round(time.time() - self.app.started_at, 2),
                "connected_switches": switches,
                "switch_count": len(switches),
                "last_stats_update": self.app.last_stats_update,
            }
        )

    @route("sdn", "/sdn/flows", methods=["GET"])
    def get_flows(self, _req: Any, **_kwargs: Any) -> Response:
        all_flows: List[Dict[str, Any]] = []
        for dpid in sorted(self.app.flow_stats.keys()):
            all_flows.extend(self.app.flow_stats[dpid])

        # Aggregate packet counters by switch and category
        summary: Dict[str, Dict[str, int]] = {}
        for f in all_flows:
            sw = f["switch"]
            cat = f["category"]
            if cat:
                summary.setdefault(sw, {"MEDICAL": 0, "FIRE": 0, "GENERAL": 0})
                summary[sw][cat] += int(f["packet_count"])

        return self._json_response({"flows": all_flows, "category_packet_counts": summary})

    @route("sdn", "/sdn/queues", methods=["GET"])
    def get_queues(self, _req: Any, **_kwargs: Any) -> Response:
        all_queues: List[Dict[str, Any]] = []
        for dpid in sorted(self.app.queue_stats.keys()):
            all_queues.extend(self.app.queue_stats[dpid])
        return self._json_response({"queues": all_queues})

    @route("sdn", "/sdn/ports", methods=["GET"])
    def get_ports(self, _req: Any, **_kwargs: Any) -> Response:
        all_ports: List[Dict[str, Any]] = []
        for dpid in sorted(self.app.port_stats.keys()):
            all_ports.extend(self.app.port_stats[dpid])
        return self._json_response({"ports": all_ports})

    @route("sdn", "/sdn/topology", methods=["GET"])
    def get_topology(self, _req: Any, **_kwargs: Any) -> Response:
        return self._json_response(
            {
                "switches": [
                    {"dpid": dpid, "name": SWITCH_NAMES.get(dpid, "S{}".format(dpid))}
                    for dpid in sorted(self.app.datapaths.keys())
                ],
                "paths": {
                    "H2": {"path": ["H1", "S1", "S2", "H2"], "link_hops": 3, "switch_hops": 2},
                    "H3": {"path": ["H1", "S1", "S2", "H3"], "link_hops": 3, "switch_hops": 2},
                    "H4": {"path": ["H1", "S1", "S2", "H4"], "link_hops": 3, "switch_hops": 2},
                },
                "qos_policy": {
                    "MEDICAL": {"dscp": 46, "dscp_name": "EF", "queue_id": 1, "of_priority": 300},
                    "FIRE": {"dscp": 34, "dscp_name": "AF41", "queue_id": 2, "of_priority": 200},
                    "GENERAL": {"dscp": 0, "dscp_name": "BE", "queue_id": 3, "of_priority": 100},
                },
            }
        )

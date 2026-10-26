"""
emergency_controller.py - Ryu (OpenFlow 1.3) controller, Member C

1. Learning switch baseline (pingall works).
2. Emergency classification: UDP packets to port 5001 get priority 100 flows
   (match eth_type=0x0800, ip_proto=17, udp_dst=5001). Normal traffic gets 10.
   These flows are installed PROACTIVELY as soon as a host MAC is learned, so a
   lower-priority L2 flow (e.g. one created by ARP) can never hide emergency traffic.
3. Optional OVS queue: emergency flows use queue 1 (needs `topology.py --qos`).
4. Link failure: on a port status change, flush flows and learned MACs so
   paths are relearned.

Run:
    ryu-manager emergency_controller.py                  # emergency-aware
    USE_QUEUE=1 ryu-manager emergency_controller.py      # + OVS queues (--qos topology)
    EMERGENCY_MODE=0 ryu-manager emergency_controller.py # static baseline for experiments

Member D can extend this class (self.datapaths holds every connected switch,
handy for port-stats polling).
"""

import os

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import ethernet, ether_types, in_proto, ipv4, packet, udp
from ryu.ofproto import ofproto_v1_3

EMERGENCY_UDP_PORT = 5001   # alerts to clients
CONTROL_UDP_PORT = 5000     # REGISTER / ACK to server

PRIORITY_TABLE_MISS = 0
PRIORITY_NORMAL = 10
PRIORITY_CONTROL = 50
PRIORITY_EMERGENCY = 100

EMERGENCY_QUEUE = 1
IDLE_TIMEOUT = 30

# Port towards the other switch (topology.py: s1 -> s2 is s1 port 3, s2 -> s1 is s2 port 1).
# The OVS queues only exist on these two ports, so set_queue is only used there.
TRUNK_PORTS = {1: 3, 2: 1}


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}   # dpid -> {mac: port}
        self.datapaths = {}     # dpid -> datapath
        self.emergency_mode = os.environ.get("EMERGENCY_MODE", "1") != "0"
        self.use_queue = os.environ.get("USE_QUEUE", "0") == "1"
        self.logger.info("Emergency prioritisation: %s, OVS queue: %s",
                         "ON" if self.emergency_mode else "OFF (baseline)",
                         "ON" if self.use_queue else "OFF")

    # ---------- helpers ----------

    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle_timeout))

    def install_table_miss(self, dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, PRIORITY_TABLE_MISS, match, actions)

    def flush_switch(self, dp):
        """Delete every flow and forget learned MACs, then restore table-miss."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL,
                                      command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))
        self.mac_to_port.pop(dp.id, None)
        self.install_table_miss(dp)

    def install_priority_flows(self, dp, mac, port):
        """Priority flows for traffic going TO this MAC (called when the MAC is learned)."""
        if not self.emergency_mode:
            return
        parser = dp.ofproto_parser
        out = [parser.OFPActionOutput(port)]

        emergency_actions = out
        if self.use_queue and port == TRUNK_PORTS.get(dp.id):
            emergency_actions = [parser.OFPActionSetQueue(EMERGENCY_QUEUE)] + out

        self.add_flow(dp, PRIORITY_EMERGENCY,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                      ip_proto=in_proto.IPPROTO_UDP,
                                      udp_dst=EMERGENCY_UDP_PORT, eth_dst=mac),
                      emergency_actions)
        # REGISTER / ACK traffic: medium priority so ACKs are not starved.
        self.add_flow(dp, PRIORITY_CONTROL,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                      ip_proto=in_proto.IPPROTO_UDP,
                                      udp_dst=CONTROL_UDP_PORT, eth_dst=mac),
                      out)
        self.logger.info("s%s: EMERGENCY flow (prio %d, udp_dst=%d) to %s via port %s%s",
                         dp.id, PRIORITY_EMERGENCY, EMERGENCY_UDP_PORT, mac, port,
                         " [queue 1]" if emergency_actions is not out else "")

    # ---------- switch connect / disconnect ----------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.install_table_miss(dp)
        self.logger.info("Switch s%s connected", dp.id)

    # ---------- link failure handling ----------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port = msg.desc
        reasons = {ofp.OFPPR_ADD: "ADD", ofp.OFPPR_DELETE: "DELETE", ofp.OFPPR_MODIFY: "MODIFY"}
        link_down = bool(port.state & ofp.OFPPS_LINK_DOWN) or bool(port.config & ofp.OFPPC_PORT_DOWN)
        self.logger.warning("Port status s%s port %s: %s, link %s -> flushing flows",
                            dp.id, port.port_no, reasons.get(msg.reason, msg.reason),
                            "DOWN" if link_down else "UP")
        self.flush_switch(dp)

    # ---------- packet-in: learning switch + classification ----------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Learn where the source MAC lives; (re)install priority flows when it is new/moved.
        table = self.mac_to_port.setdefault(dp.id, {})
        if table.get(eth.src) != in_port:
            table[eth.src] = in_port
            self.install_priority_flows(dp, eth.src, in_port)
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]

        # Ordinary L2 flow (priority 10) once the destination is known.
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
            self.add_flow(dp, PRIORITY_NORMAL, match, actions, IDLE_TIMEOUT)

        # Send the packet that triggered this packet-in (table-miss uses NO_BUFFER).
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=msg.data))

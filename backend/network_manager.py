"""Unified Network, SDN Controller, Congestion, and IPC Event Manager."""

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

from backend.classifier import EmergencyClassifier
from backend.config import (
    BOTTLENECK_LINK_RATE_BPS,
    CLIENT_EVIDENCE_DIR,
    CLIENT_IDS,
    CLIENT_UDP_PORTS,
    HOSTS,
    RYU_REST_URL,
    SERVER_IPC_SOCK,
    SERVER_UDP_PORT,
)
from backend.metrics import MetricsCSVLogger
from backend.server import ipc_call
from sdn.qos_config import get_ovs_queue_stats
from sdn.topology import MININET_AVAILABLE, MininetEmergencyNetwork

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class EventBroker:
    """Thread-safe pub/sub broker for Server-Sent Events (SSE) clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: List[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        payload = {
            "event": event_type,
            "ts_wall_ms": round(time.time() * 1000.0, 3),
            "data": data,
        }
        with self._lock:
            dead: List[queue.Queue] = []
            for q in self._queues:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:
                        dead.append(q)
            for q in dead:
                self._queues.remove(q)


class NetworkManager:
    """Coordinates Mininet/Loopback runtime, Ryu SDN stats, congestion, packet loss, and H1 IPC."""

    def __init__(self) -> None:
        self.broker = EventBroker()
        self.csv_logger = MetricsCSVLogger()
        self._lock = threading.RLock()

        self._mininet: Optional[MininetEmergencyNetwork] = None
        self._local_procs: Dict[str, subprocess.Popen] = {}
        self._mode: str = "STOPPED"  # STOPPED | MININET_SDN | EXTERNAL_MININET | LOOPBACK_TEST
        self._congestion_active: bool = False
        self._congestion_rate_mbps: float = 0.0
        self._packet_loss_config: Dict[str, Dict[str, float]] = {
            cid: {"forward_loss_pct": 0.0, "ack_loss_pct": 0.0} for cid in CLIENT_IDS
        }
        self._qos_info: Dict[str, Any] = {}
        self._last_sdn_stats: Dict[str, Any] = {
            "controller_online": False,
            "switches": [],
            "flows": [],
            "queues": [],
            "ports": [],
            "category_packet_counts": {},
        }
        self._experiment_running = False
        self._last_experiment_summary: Optional[Dict[str, Any]] = None

        self._stop_threads = False
        self._ipc_sub_thread = threading.Thread(target=self._ipc_subscribe_loop, name="h1-ipc-sub", daemon=True)
        self._sdn_poll_thread = threading.Thread(target=self._sdn_poll_loop, name="sdn-poll", daemon=True)
        self._ipc_sub_thread.start()
        self._sdn_poll_thread.start()

    # ------------------------------------------------------------------
    # Ryu REST API helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _ryu_get(path: str, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        url = f"{RYU_REST_URL.rstrip('/')}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        return None

    def fetch_sdn_stats(self) -> Dict[str, Any]:
        status = self._ryu_get("/sdn/status")
        if not status:
            self._last_sdn_stats = {
                "controller_online": False,
                "openflow_version": "1.3",
                "switches": [],
                "flows": [],
                "queues": [],
                "ports": [],
                "category_packet_counts": {},
            }
            return self._last_sdn_stats

        flows_resp = self._ryu_get("/sdn/flows") or {}
        queues_resp = self._ryu_get("/sdn/queues") or {}
        ports_resp = self._ryu_get("/sdn/ports") or {}

        queues = queues_resp.get("queues", [])
        # If running as root and Ryu queue stats are empty, supplement with ovs-ofctl queue-stats
        if not queues and os.geteuid() == 0 and self._mode in ("MININET_SDN", "EXTERNAL_MININET"):
            queues = get_ovs_queue_stats()

        self._last_sdn_stats = {
            "controller_online": True,
            "openflow_version": status.get("openflow_version", "1.3"),
            "uptime_sec": status.get("uptime_sec", 0.0),
            "switches": status.get("connected_switches", []),
            "flows": flows_resp.get("flows", []),
            "queues": queues,
            "ports": ports_resp.get("ports", []),
            "category_packet_counts": flows_resp.get("category_packet_counts", {}),
        }
        return self._last_sdn_stats

    def _sdn_poll_loop(self) -> None:
        while not self._stop_threads:
            try:
                stats = self.fetch_sdn_stats()
                # Auto-detect externally started Mininet topology if H1 IPC is reachable
                if self._mode == "STOPPED" and self._is_h1_ipc_alive():
                    with self._lock:
                        if self._mode == "STOPPED":
                            self._mode = "EXTERNAL_MININET" if stats.get("switches") else "LOOPBACK_TEST"
                self.broker.publish("sdn_stats", stats)
            except Exception:
                pass
            time.sleep(2.0)

    # ------------------------------------------------------------------
    # H1 Server Cross-Namespace IPC Bridge
    # ------------------------------------------------------------------
    @staticmethod
    def _is_h1_ipc_alive() -> bool:
        if not os.path.exists(SERVER_IPC_SOCK):
            return False
        try:
            resp = ipc_call({"cmd": "ping"}, timeout=0.6)
            return bool(resp.get("ok"))
        except Exception:
            return False

    def _ipc_subscribe_loop(self) -> None:
        """Continuously maintain a subscription to H1's IPC event stream and forward to SSE."""
        while not self._stop_threads:
            if not os.path.exists(SERVER_IPC_SOCK):
                time.sleep(0.4)
                continue
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(SERVER_IPC_SOCK)
                sock.sendall(b'{"cmd":"subscribe"}\n')
                buf = b""
                while not self._stop_threads:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line.decode("utf-8"))
                            self.broker.publish(ev.get("event", "server_event"), ev.get("data", {}))
                        except Exception:
                            pass
            except OSError:
                time.sleep(0.5)
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def call_h1(self, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        if not os.path.exists(SERVER_IPC_SOCK):
            raise RuntimeError("Emergency Notification Server (H1) is not running. Activate the network first.")
        return ipc_call(payload, timeout=timeout)

    # ------------------------------------------------------------------
    # Network Lifecycle (Mininet SDN or Local Loopback Test Mode)
    # ------------------------------------------------------------------
    def activate_network(self, mode: str = "mininet", bottleneck_mbps: float = 10.0) -> Dict[str, Any]:
        with self._lock:
            req_mode = (mode or "mininet").lower()

            # Check if externally started Mininet is already running
            if self._mode in ("MININET_SDN", "EXTERNAL_MININET", "LOOPBACK_TEST") and self._is_h1_ipc_alive():
                return {"ok": True, "mode": self._mode, "already_running": True, "status": self.get_full_status()}

            if req_mode == "mininet":
                if not MININET_AVAILABLE:
                    return {
                        "ok": False,
                        "error": "Mininet is not installed in this Python environment.",
                        "can_use_loopback": True,
                    }
                if os.geteuid() != 0:
                    return {
                        "ok": False,
                        "error": (
                            "Mininet requires root privileges (sudo). Start the platform with "
                            "`sudo ./run.sh` (or run `sudo python3 sdn/topology.py` in another terminal), "
                            "or activate Loopback UDP Test Mode for non-root testing."
                        ),
                        "requires_root": True,
                        "can_use_loopback": True,
                    }

                sdn = self.fetch_sdn_stats()
                if not sdn.get("controller_online"):
                    return {
                        "ok": False,
                        "error": (
                            f"Ryu SDN Controller is not reachable on {RYU_REST_URL} (OpenFlow :6653). "
                            "Start the Ryu controller first (`ryu-manager --wsapi-port 8088 sdn/controller.py` or `sudo ./run.sh`)."
                        ),
                        "controller_offline": True,
                    }

                self._mininet = MininetEmergencyNetwork(
                    bottleneck_bps=int(max(1.0, float(bottleneck_mbps)) * 1_000_000)
                )
                start_res = self._mininet.start(launch_apps=True)
                self._mode = "MININET_SDN"
                self._qos_info = start_res.get("qos", {})
                self._congestion_active = False
                self._congestion_rate_mbps = 0.0

                # Wait briefly for Ryu stats to register S1 & S2
                time.sleep(0.8)
                self.fetch_sdn_stats()
                status = self.get_full_status()
                self.broker.publish("network_status", status)
                return {"ok": True, "mode": self._mode, "qos": self._qos_info, "status": status}

            elif req_mode == "loopback":
                return self._start_loopback_mode()

            return {"ok": False, "error": f"Unknown activation mode: {mode}"}

    def _start_loopback_mode(self) -> Dict[str, Any]:
        """Start real UDP Server (127.0.0.1:9000) and 3 UDP Clients (9102/9103/9104) without Mininet."""
        self.stop_network()

        if CLIENT_EVIDENCE_DIR.exists():
            shutil.rmtree(CLIENT_EVIDENCE_DIR, ignore_errors=True)
        CLIENT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        log_dir = Path("/tmp/emergency_notify_logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        py = sys.executable
        h1_log = open(log_dir / "h1_server_loopback.log", "w", encoding="utf-8")
        self._local_procs["H1"] = subprocess.Popen(
            [
                py,
                str(PROJECT_ROOT / "backend" / "server.py"),
                "--bind", "127.0.0.1",
                "--port", str(SERVER_UDP_PORT),
                "--ipc", SERVER_IPC_SOCK,
                "--mode", "LOOPBACK_TEST",
            ],
            stdout=h1_log,
            stderr=subprocess.STDOUT,
        )

        for _ in range(40):
            if os.path.exists(SERVER_IPC_SOCK):
                break
            time.sleep(0.1)

        for cid in CLIENT_IDS:
            clog = open(log_dir / f"{cid.lower()}_loopback.log", "w", encoding="utf-8")
            self._local_procs[cid] = subprocess.Popen(
                [
                    py,
                    str(PROJECT_ROOT / "backend" / "client.py"),
                    "--id", cid,
                    "--server-ip", "127.0.0.1",
                    "--server-port", str(SERVER_UDP_PORT),
                    "--bind", "127.0.0.1",
                    "--port", str(CLIENT_UDP_PORTS[cid]),
                ],
                stdout=clog,
                stderr=subprocess.STDOUT,
            )

        time.sleep(0.8)
        self._mode = "LOOPBACK_TEST"
        self._qos_info = {
            "ok": False,
            "note": "Loopback UDP test mode (no OVS switches). Run with sudo for live Mininet + OVS HTB QoS.",
        }
        status = self.get_full_status()
        self.broker.publish("network_status", status)
        return {"ok": True, "mode": self._mode, "status": status}

    def stop_network(self) -> Dict[str, Any]:
        with self._lock:
            if self._mininet is not None:
                try:
                    self._mininet.stop()
                except Exception:
                    pass
                self._mininet = None

            for name, proc in list(self._local_procs.items()):
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._local_procs.pop(name, None)

            if os.path.exists(SERVER_IPC_SOCK):
                try:
                    os.unlink(SERVER_IPC_SOCK)
                except OSError:
                    pass

            self._mode = "STOPPED"
            self._congestion_active = False
            self._congestion_rate_mbps = 0.0
            self._qos_info = {}
            for cid in CLIENT_IDS:
                self._packet_loss_config[cid] = {"forward_loss_pct": 0.0, "ack_loss_pct": 0.0}

            status = self.get_full_status()
            self.broker.publish("network_status", status)
            return {"ok": True, "mode": self._mode, "status": status}

    # ------------------------------------------------------------------
    # Congestion & Controlled Packet Loss (Tests 5 & 6)
    # ------------------------------------------------------------------
    def start_congestion(self, rate_mbps: float = 15.0) -> Dict[str, Any]:
        with self._lock:
            if self._mode == "MININET_SDN" and self._mininet is not None:
                res = self._mininet.start_congestion(rate_mbps=rate_mbps)
                self._congestion_active = True
                self._congestion_rate_mbps = float(res["rate_mbps"])
                self.broker.publish("congestion_changed", res)
                return res
            raise RuntimeError(
                "H5 -> H6 network congestion requires active Mininet SDN mode (run with sudo)."
            )

    def stop_congestion(self) -> Dict[str, Any]:
        with self._lock:
            if self._mininet is not None:
                res = self._mininet.stop_congestion()
            else:
                res = {"ok": True, "congestion_active": False, "rate_mbps": 0.0}
            self._congestion_active = False
            self._congestion_rate_mbps = 0.0
            self.broker.publish("congestion_changed", res)
            return res

    def set_packet_loss(
        self,
        client_id: str,
        forward_loss_pct: float = 0.0,
        ack_loss_pct: float = 0.0,
    ) -> Dict[str, Any]:
        cid = client_id.upper()
        if cid not in CLIENT_IDS:
            raise ValueError(f"Unsupported client_id: {client_id}")
        fwd = max(0.0, min(100.0, float(forward_loss_pct)))
        ack = max(0.0, min(100.0, float(ack_loss_pct)))

        with self._lock:
            if self._mode == "MININET_SDN" and self._mininet is not None:
                # Real kernel-level tc netem packet drops on S2 egress and client eth0!
                res = self._mininet.set_packet_loss(cid, forward_loss_pct=fwd, ack_loss_pct=ack)
                # Clear software fault file so tc netem is the sole mechanism in Mininet mode
                ctrl = CLIENT_EVIDENCE_DIR / f"{cid}_control.json"
                if ctrl.exists():
                    ctrl.unlink()
            else:
                # In Loopback Test Mode, use client-side controlled drop probabilities
                CLIENT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                ctrl = CLIENT_EVIDENCE_DIR / f"{cid}_control.json"
                ctrl.write_text(
                    json.dumps({"drop_forward_prob": fwd / 100.0, "drop_ack_prob": ack / 100.0}),
                    encoding="utf-8",
                )
                res = {
                    "ok": True,
                    "client_id": cid,
                    "forward_loss_pct": fwd,
                    "ack_loss_pct": ack,
                    "mechanism": "client_fault_control (loopback mode)",
                }

            self._packet_loss_config[cid] = {"forward_loss_pct": fwd, "ack_loss_pct": ack}
            self.broker.publish("packet_loss_changed", {"client_id": cid, "config": self._packet_loss_config})
            return res

    # ------------------------------------------------------------------
    # Sending Emergency Alerts
    # ------------------------------------------------------------------
    def send_alert(
        self,
        category: Optional[str],
        message: Optional[str] = None,
        ack_timeout_sec: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_interval_sec: Optional[float] = None,
        payload_bytes: int = 0,
    ) -> Dict[str, Any]:
        resp = self.call_h1(
            {
                "cmd": "send_alert",
                "category": category,
                "message": message,
                "ack_timeout_sec": ack_timeout_sec,
                "max_retries": max_retries,
                "retry_interval_sec": retry_interval_sec,
                "congestion_active": self._congestion_active,
                "congestion_rate_mbps": self._congestion_rate_mbps,
                "network_mode": self._mode,
                "payload_bytes": payload_bytes,
            }
        )
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "Failed to send alert"))
        return resp["result"]

    # ------------------------------------------------------------------
    # Automated 6-Scenario QoS Experiment Runner (Section 8)
    # ------------------------------------------------------------------
    def run_qos_experiments(
        self,
        alerts_per_scenario: int = 5,
        congestion_rate_mbps: float = 18.0,
        interval_sec: float = 0.45,
    ) -> Dict[str, Any]:
        """Execute all 6 QoS experiments (Medical/Fire/General x Normal/Congestion) and log results."""
        if self._experiment_running:
            raise RuntimeError("An experiment suite is already running.")
        if not self._is_h1_ipc_alive():
            raise RuntimeError("Network is not active. Start Mininet first.")

        self._experiment_running = True
        run_id = datetime.now().strftime("EXP-%Y%m%d-%H%M%S")
        results: List[Dict[str, Any]] = []

        scenarios = [
            ("MEDICAL", False),
            ("FIRE", False),
            ("GENERAL", False),
        ]
        # Only run congestion scenarios if Mininet SDN is active
        if self._mode == "MININET_SDN":
            scenarios.extend([
                ("MEDICAL", True),
                ("FIRE", True),
                ("GENERAL", True),
            ])

        try:
            self.broker.publish(
                "experiment_started",
                {"run_id": run_id, "total_scenarios": len(scenarios), "alerts_per_scenario": alerts_per_scenario},
            )

            for idx, (category, congested) in enumerate(scenarios, start=1):
                condition = "congestion" if congested else "normal"
                scenario_name = f"{category}_{condition.upper()}"

                if congested and not self._congestion_active:
                    self.start_congestion(rate_mbps=congestion_rate_mbps)
                    time.sleep(1.2)  # Allow HTB queue saturation to stabilize
                elif not congested and self._congestion_active:
                    self.stop_congestion()
                    time.sleep(0.5)

                profile = EmergencyClassifier.get_profile(category)
                q_before = self._queue_counter_for(profile.queue_id)

                alert_ids: List[str] = []
                for n in range(alerts_per_scenario):
                    res = self.send_alert(
                        category=category,
                        message=f"[{scenario_name} #{n+1}] {profile.default_message}",
                        ack_timeout_sec=2.0,
                        max_retries=1,
                        retry_interval_sec=0.8,
                        payload_bytes=512,
                    )
                    alert_ids.append(res["alert"]["alert_id"])
                    time.sleep(interval_sec)

                # Wait for all alerts in this scenario to complete
                deadline = time.time() + 4.0
                completed_metrics: List[Dict[str, Any]] = []
                while time.time() < deadline:
                    completed_metrics = []
                    all_done = True
                    for aid in alert_ids:
                        ares = self.call_h1({"cmd": "get_alert", "alert_id": aid})
                        if ares.get("ok"):
                            am = ares["alert_metrics"]
                            completed_metrics.append(am)
                            if not am.get("completed"):
                                all_done = False
                    if all_done:
                        break
                    time.sleep(0.15)

                time.sleep(0.4)
                q_after = self._queue_counter_for(profile.queue_id)

                intended = sum(m["intended_deliveries"] for m in completed_metrics)
                delivered = sum(m["packets_delivered"] for m in completed_metrics)
                lost = sum(m["packets_lost"] for m in completed_metrics)
                ack_fail = sum(m["ack_failures"] for m in completed_metrics)

                lats: List[float] = []
                rtts: List[float] = []
                for m in completed_metrics:
                    for cdata in m["per_client"].values():
                        if cdata["delivery_latency_ms"] is not None:
                            lats.append(cdata["delivery_latency_ms"])
                        if cdata["rtt_ms"] is not None:
                            rtts.append(cdata["rtt_ms"])

                row = {
                    "run_id": run_id,
                    "timestamp_ms": round(time.time() * 1000.0, 3),
                    "scenario": scenario_name,
                    "category": category,
                    "condition": condition,
                    "congestion_rate_mbps": congestion_rate_mbps if congested else 0.0,
                    "alerts_sent": len(alert_ids),
                    "intended_deliveries": intended,
                    "packets_delivered": delivered,
                    "packets_lost": lost,
                    "ack_failures": ack_fail,
                    "pdr_percent": round((delivered / intended) * 100.0, 2) if intended else None,
                    "plr_percent": round((lost / intended) * 100.0, 2) if intended else None,
                    "avg_delivery_latency_ms": round(sum(lats) / len(lats), 3) if lats else None,
                    "min_delivery_latency_ms": round(min(lats), 3) if lats else None,
                    "max_delivery_latency_ms": round(max(lats), 3) if lats else None,
                    "avg_rtt_ms": round(sum(rtts) / len(rtts), 3) if rtts else None,
                    "queue_tx_packets": max(0, q_after["tx_packets"] - q_before["tx_packets"]),
                    "queue_tx_bytes": max(0, q_after["tx_bytes"] - q_before["tx_bytes"]),
                    "network_mode": self._mode,
                }
                self.csv_logger.log_experiment_row(row)
                results.append(row)
                self.broker.publish(
                    "experiment_progress",
                    {"run_id": run_id, "step": idx, "total": len(scenarios), "result": row},
                )

            if self._congestion_active:
                self.stop_congestion()

            summary = {"ok": True, "run_id": run_id, "results": results, "mode": self._mode}
            self._last_experiment_summary = summary
            self.broker.publish("experiment_completed", summary)
            return summary
        finally:
            self._experiment_running = False

    def _queue_counter_for(self, queue_id: int) -> Dict[str, int]:
        stats = self.fetch_sdn_stats()
        pkts = 0
        bts = 0
        for q in stats.get("queues", []):
            if int(q.get("queue_id", -1)) == int(queue_id) and q.get("switch") == "S1":
                pkts += int(q.get("tx_packets", 0))
                bts += int(q.get("tx_bytes", 0))
        return {"tx_packets": pkts, "tx_bytes": bts}

    # ------------------------------------------------------------------
    # Full System State Snapshot
    # ------------------------------------------------------------------
    def get_full_status(self) -> Dict[str, Any]:
        h1_alive = self._is_h1_ipc_alive()
        server_state: Optional[Dict[str, Any]] = None
        if h1_alive:
            try:
                resp = self.call_h1({"cmd": "get_state"}, timeout=2.0)
                if resp.get("ok"):
                    server_state = resp["state"]
            except Exception:
                h1_alive = False

        sdn = self._last_sdn_stats
        clients_map = {c["client_id"]: c for c in (server_state.get("clients", []) if server_state else [])}
        switches_connected = {s["name"] for s in sdn.get("switches", [])}

        nodes = {
            "RYU": {
                "id": "RYU",
                "role": "Ryu SDN Controller (OF 1.3)",
                "ip": "127.0.0.1:6653",
                "online": bool(sdn.get("controller_online")),
            },
            "S1": {
                "id": "S1",
                "role": "Ingress Switch (QoS Classifier)",
                "dpid": "1",
                "online": "S1" in switches_connected or self._mode == "MININET_SDN",
            },
            "S2": {
                "id": "S2",
                "role": "Egress Switch (Client Fanout)",
                "dpid": "2",
                "online": "S2" in switches_connected or self._mode == "MININET_SDN",
            },
            "H1": {
                "id": "H1",
                "role": HOSTS["H1"]["role"],
                "ip": HOSTS["H1"]["ip"] if self._mode != "LOOPBACK_TEST" else "127.0.0.1",
                "online": h1_alive,
            },
            "H5": {
                "id": "H5",
                "role": HOSTS["H5"]["role"],
                "ip": HOSTS["H5"]["ip"],
                "online": self._mode in ("MININET_SDN", "EXTERNAL_MININET"),
                "active_traffic": self._congestion_active,
            },
            "H6": {
                "id": "H6",
                "role": HOSTS["H6"]["role"],
                "ip": HOSTS["H6"]["ip"],
                "online": self._mode in ("MININET_SDN", "EXTERNAL_MININET"),
                "active_traffic": self._congestion_active,
            },
        }
        for cid in CLIENT_IDS:
            cinfo = clients_map.get(cid)
            nodes[cid] = {
                "id": cid,
                "role": HOSTS[cid]["role"],
                "ip": cinfo["ip"] if cinfo else HOSTS[cid]["ip"],
                "port": cinfo["port"] if cinfo else CLIENT_UDP_PORTS[cid],
                "online": cinfo is not None,
                "registered": cinfo is not None,
                "packet_loss": self._packet_loss_config.get(cid, {"forward_loss_pct": 0.0, "ack_loss_pct": 0.0}),
            }

        return {
            "network_active": self._mode != "STOPPED" and h1_alive,
            "mode": self._mode,
            "is_root": os.geteuid() == 0,
            "mininet_available": MININET_AVAILABLE,
            "congestion_active": self._congestion_active,
            "congestion_rate_mbps": self._congestion_rate_mbps,
            "qos_info": self._qos_info,
            "packet_loss_config": self._packet_loss_config,
            "sdn": sdn,
            "nodes": nodes,
            "server": server_state,
            "profiles": EmergencyClassifier.all_profiles(),
            "experiment_running": self._experiment_running,
            "last_experiment": self._last_experiment_summary,
        }

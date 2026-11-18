"""Central UDP Emergency Notification Server (runs inside Mininet host H1 or locally)."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure project root is on sys.path when executed directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.acknowledgement import (
    AcknowledgementManager,
    AlertRecord,
    FAILURE_ACK_LOST,
    FAILURE_DELIVERY,
    STATUS_ACK_RECEIVED,
)
from backend.classifier import EmergencyClassifier
from backend.config import (
    CLIENT_EVIDENCE_DIR,
    DEFAULT_ACK_TIMEOUT_SEC,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_INTERVAL_SEC,
    SERVER_IPC_SOCK,
    SERVER_UDP_PORT,
)
from backend.metrics import MetricsCSVLogger, compute_aggregate_metrics, compute_alert_metrics
from backend.protocol import (
    build_alert_packet,
    build_register_ack_packet,
    decode_packet,
    encode_packet,
    now_timestamps,
)


class EmergencyNotificationServer:
    """UDP Emergency Notification Server with DSCP QoS tagging, ACK tracking, and IPC."""

    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        udp_port: int = SERVER_UDP_PORT,
        ipc_sock_path: Optional[str] = SERVER_IPC_SOCK,
        evidence_dir: Path = CLIENT_EVIDENCE_DIR,
        ack_timeout_sec: float = DEFAULT_ACK_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_interval_sec: float = DEFAULT_RETRY_INTERVAL_SEC,
        network_mode: str = "MININET_SDN",
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.bind_ip = bind_ip
        self.udp_port = udp_port
        self.ipc_sock_path = ipc_sock_path
        self.evidence_dir = Path(evidence_dir)
        self.ack_timeout_sec = ack_timeout_sec
        self.max_retries = max_retries
        self.retry_interval_sec = retry_interval_sec
        self.network_mode = network_mode

        self.ack_manager = AcknowledgementManager(evidence_dir=self.evidence_dir)
        self.csv_logger = MetricsCSVLogger()

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._clients: Dict[str, Dict[str, Any]] = {}
        self._alert_seq = 0
        self._running = False

        self._udp_sock: Optional[socket.socket] = None
        self._ipc_sock: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []

        self._event_callback = event_callback
        self._subscribers: List[socket.socket] = []
        self._sub_lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        # Open real UDP socket
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.bind((self.bind_ip, self.udp_port))
        self._udp_sock.settimeout(0.2)

        t_udp = threading.Thread(target=self._udp_recv_loop, name="udp-recv", daemon=True)
        t_mon = threading.Thread(target=self._monitor_loop, name="ack-monitor", daemon=True)
        t_udp.start()
        t_mon.start()
        self._threads = [t_udp, t_mon]

        if self.ipc_sock_path:
            self._start_ipc_server()

    def stop(self) -> None:
        self._running = False
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except OSError:
                pass
            self._udp_sock = None

        if self._ipc_sock:
            try:
                self._ipc_sock.close()
            except OSError:
                pass
            self._ipc_sock = None

        if self.ipc_sock_path and os.path.exists(self.ipc_sock_path):
            try:
                os.unlink(self.ipc_sock_path)
            except OSError:
                pass

        with self._sub_lock:
            for conn in self._subscribers:
                try:
                    conn.close()
                except OSError:
                    pass
            self._subscribers.clear()

        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()

    def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        event = {
            "event": event_type,
            "ts_wall_ms": round(time.time() * 1000.0, 3),
            "data": data,
        }
        if self._event_callback:
            try:
                self._event_callback(event)
            except Exception:
                pass

        payload = (json.dumps(event) + "\n").encode("utf-8")
        dead: List[socket.socket] = []
        with self._sub_lock:
            for conn in self._subscribers:
                try:
                    conn.sendall(payload)
                except OSError:
                    dead.append(conn)
            for conn in dead:
                self._subscribers.remove(conn)
                try:
                    conn.close()
                except OSError:
                    pass

    def _send_udp_with_dscp(self, payload: Dict[str, Any], addr: Tuple[str, int], tos: int) -> None:
        """Send a UDP datagram with explicit IPv4 ToS/DSCP header value."""
        if not self._udp_sock:
            raise RuntimeError("UDP server socket is not running.")
        raw = encode_packet(payload)
        with self._send_lock:
            try:
                self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(tos))
            except OSError:
                pass
            self._udp_sock.sendto(raw, addr)

    def _udp_recv_loop(self) -> None:
        while self._running and self._udp_sock:
            try:
                raw, addr = self._udp_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            rx_wall_ms, rx_mono_ns = now_timestamps()
            try:
                pkt = decode_packet(raw)
            except Exception:
                continue

            pkt_type = pkt.get("type")
            if pkt_type == "REGISTER":
                self._handle_register(pkt, addr, rx_wall_ms)
            elif pkt_type == "HEARTBEAT":
                self._handle_heartbeat(pkt, addr, rx_wall_ms)
            elif pkt_type == "ACK":
                self._handle_ack(pkt, rx_wall_ms, rx_mono_ns)

    def _handle_register(self, pkt: Dict[str, Any], addr: Tuple[str, int], rx_wall_ms: float) -> None:
        client_id = str(pkt.get("client_id", "")).strip()
        if not client_id:
            return
        client_ip = addr[0]
        client_port = int(pkt.get("port") or addr[1])
        is_new = False
        with self._lock:
            is_new = client_id not in self._clients
            self._clients[client_id] = {
                "client_id": client_id,
                "ip": client_ip,
                "port": client_port,
                "registered_at_ms": self._clients.get(client_id, {}).get("registered_at_ms", round(rx_wall_ms, 3)),
                "last_seen_ms": round(rx_wall_ms, 3),
                "status": "REGISTERED",
            }
            client_info = dict(self._clients[client_id])

        ack = build_register_ack_packet(client_id)
        try:
            self._send_udp_with_dscp(ack, (client_ip, client_port), tos=0)
        except OSError:
            pass

        if is_new:
            self._emit_event("client_registered", client_info)

    def _handle_heartbeat(self, pkt: Dict[str, Any], addr: Tuple[str, int], rx_wall_ms: float) -> None:
        client_id = str(pkt.get("client_id", "")).strip()
        if not client_id:
            return
        with self._lock:
            if client_id in self._clients:
                self._clients[client_id]["last_seen_ms"] = round(rx_wall_ms, 3)
                self._clients[client_id]["status"] = "REGISTERED"
            else:
                self._clients[client_id] = {
                    "client_id": client_id,
                    "ip": addr[0],
                    "port": int(pkt.get("port") or addr[1]),
                    "registered_at_ms": round(rx_wall_ms, 3),
                    "last_seen_ms": round(rx_wall_ms, 3),
                    "status": "REGISTERED",
                }

    def _handle_ack(self, pkt: Dict[str, Any], rx_wall_ms: float, rx_mono_ns: int) -> None:
        alert_id = pkt.get("alert_id")
        client_id = pkt.get("client_id")
        if not alert_id or not client_id:
            return

        alert, rec, is_dup = self.ack_manager.process_ack(
            alert_id=alert_id,
            client_id=client_id,
            client_rx_wall_ms=float(pkt.get("client_rx_wall_ms", rx_wall_ms)),
            client_rx_mono_ns=int(pkt.get("client_rx_mono_ns", rx_mono_ns)),
            ack_tx_wall_ms=float(pkt.get("ack_tx_wall_ms", rx_wall_ms)),
            ack_tx_mono_ns=int(pkt.get("ack_tx_mono_ns", rx_mono_ns)),
            server_ack_rx_wall_ms=rx_wall_ms,
            server_ack_rx_mono_ns=rx_mono_ns,
        )
        if not alert or not rec:
            return

        if is_dup:
            self._emit_event(
                "duplicate_ack_ignored",
                {
                    "alert_id": alert_id,
                    "client_id": client_id,
                    "duplicate_acks_ignored": rec.duplicate_acks_ignored,
                },
            )
            return

        e_rx = alert.add_timeline(
            stage="Client receives message",
            detail=f"{client_id} received {alert.category} alert (latency: {rec.delivery_latency_ms:.3f} ms)",
            wall_ms=rec.client_rx_wall_ms,
            client_id=client_id,
        )
        e_ack_tx = alert.add_timeline(
            stage="Client generates ACK",
            detail=f"{client_id} generated ACK for {alert_id} (DSCP {alert.dscp})",
            wall_ms=rec.ack_tx_wall_ms,
            client_id=client_id,
        )
        e_ack_rx = alert.add_timeline(
            stage="Server receives ACK",
            detail=f"H1 received ACK from {client_id} (RTT: {rec.rtt_ms:.3f} ms)",
            wall_ms=rec.server_ack_rx_wall_ms,
            client_id=client_id,
        )

        self._emit_event(
            "ack_received",
            {
                "alert_id": alert_id,
                "category": alert.category,
                "client_id": client_id,
                "record": rec.to_dict(),
                "timeline_entries": [e_rx, e_ack_tx, e_ack_rx],
                "alert_metrics": compute_alert_metrics(alert),
            },
        )

        if alert.is_all_finalized() and not alert.completed:
            self._complete_alert(alert, rx_wall_ms)

    def _complete_alert(self, alert: AlertRecord, now_wall_ms: float) -> None:
        alert.completed = True
        alert.completed_wall_ms = now_wall_ms
        metrics = compute_alert_metrics(alert)
        e_done = alert.add_timeline(
            stage="Delivery measurement completed",
            detail=(
                f"Delivered {metrics['packets_delivered']}/{metrics['intended_deliveries']} "
                f"(PDR {metrics['pdr_percent']}%, Avg Lat: "
                f"{metrics['avg_delivery_latency_ms']} ms, Avg RTT: {metrics['avg_rtt_ms']} ms)"
            ),
            wall_ms=now_wall_ms,
        )
        try:
            self.csv_logger.log_completed_alert(alert)
        except Exception:
            pass

        self._emit_event(
            "alert_completed",
            {
                "alert_id": alert.alert_id,
                "alert": alert.to_dict(),
                "alert_metrics": metrics,
                "aggregate_metrics": compute_aggregate_metrics(self.ack_manager.list_alerts()),
                "timeline_entry": e_done,
            },
        )

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(0.05)
            now_wall_ms, now_mono_ns = now_timestamps()
            retries_due, timed_out, newly_completed = self.ack_manager.evaluate_timeouts_and_retries(
                now_mono_ns, now_wall_ms
            )

            for alert, rec in retries_due:
                profile = EmergencyClassifier.get_profile(alert.category)
                tx_wall_ms, tx_mono_ns = now_timestamps()
                updated = self.ack_manager.record_retransmission(
                    alert.alert_id, rec.client_id, tx_wall_ms, tx_mono_ns
                )
                if not updated:
                    continue
                pkt = build_alert_packet(
                    alert_id=alert.alert_id,
                    target_client=rec.client_id,
                    category=alert.category,
                    priority=alert.priority,
                    dscp=alert.dscp,
                    dscp_name=alert.dscp_name,
                    queue_id=alert.queue_id,
                    message=alert.message,
                    attempt=updated.attempts,
                    tx_wall_ms=updated.orig_tx_wall_ms,
                    tx_mono_ns=updated.orig_tx_mono_ns,
                )
                try:
                    self._send_udp_with_dscp(pkt, (rec.client_ip, rec.client_port), tos=profile.tos)
                    entry = alert.add_timeline(
                        stage="UDP packet retransmitted",
                        detail=f"Retransmission #{updated.attempts - 1} to {rec.client_id} ({rec.client_ip}:{rec.client_port})",
                        wall_ms=tx_wall_ms,
                        client_id=rec.client_id,
                    )
                    self._emit_event(
                        "packet_retransmitted",
                        {
                            "alert_id": alert.alert_id,
                            "client_id": rec.client_id,
                            "attempt": updated.attempts,
                            "timeline_entry": entry,
                        },
                    )
                except OSError:
                    pass

            for alert, rec in timed_out:
                if rec.failure_reason == FAILURE_ACK_LOST:
                    detail = (
                        f"ACK timeout for {rec.client_id}: client evidence confirms delivery "
                        f"(latency {rec.delivery_latency_ms:.3f} ms) — classified as ACK Loss"
                        if rec.delivery_latency_ms is not None
                        else f"ACK timeout for {rec.client_id}: client confirmed delivery — ACK Loss"
                    )
                elif rec.failure_reason == FAILURE_DELIVERY:
                    detail = f"Delivery failed for {rec.client_id}: packet did not reach client after {rec.attempts} attempt(s)"
                else:
                    detail = f"ACK timeout for {rec.client_id}: no ACK received and delivery unconfirmed"

                entry = alert.add_timeline(
                    stage=f"Client {rec.status}",
                    detail=detail,
                    wall_ms=now_wall_ms,
                    client_id=rec.client_id,
                )
                self._emit_event(
                    "client_timeout",
                    {
                        "alert_id": alert.alert_id,
                        "client_id": rec.client_id,
                        "record": rec.to_dict(),
                        "timeline_entry": entry,
                        "alert_metrics": compute_alert_metrics(alert),
                    },
                )

            for alert in newly_completed:
                self._complete_alert(alert, now_wall_ms)

    def send_alert(
        self,
        category: Optional[str],
        message: Optional[str] = None,
        ack_timeout_sec: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_interval_sec: Optional[float] = None,
        congestion_active: bool = False,
        congestion_rate_mbps: float = 0.0,
        network_mode: Optional[str] = None,
        payload_bytes: int = 0,
    ) -> Dict[str, Any]:
        """Classify an emergency notification, tag DSCP, and broadcast UDP packets to all registered clients."""
        with self._lock:
            registered = [dict(c) for c in self._clients.values()]

        if not registered:
            raise RuntimeError("No registered clients available. Start the network and register H2, H3, H4 first.")

        created_wall_ms, _ = now_timestamps()
        # Allow default message if empty
        prelim_cat = (category or "AUTO").strip().upper()
        if not message or not message.strip():
            if prelim_cat in ("MEDICAL", "FIRE", "GENERAL"):
                message = EmergencyClassifier.get_profile(prelim_cat).default_message
            else:
                message = EmergencyClassifier.get_profile("GENERAL").default_message
        else:
            message = message.strip()

        profile = EmergencyClassifier.classify(message, explicit_category=category)

        with self._lock:
            self._alert_seq += 1
            seq = self._alert_seq
        stamp = datetime.now().strftime("%H%M%S")
        alert_id = f"ALT-{stamp}-{seq:04d}"

        timeout_s = float(ack_timeout_sec if ack_timeout_sec is not None else self.ack_timeout_sec)
        retries = int(max_retries if max_retries is not None else self.max_retries)
        retry_int = float(retry_interval_sec if retry_interval_sec is not None else self.retry_interval_sec)

        tx_wall_ms, tx_mono_ns = now_timestamps()
        alert = AlertRecord(
            alert_id=alert_id,
            category=profile.category,
            priority=profile.priority,
            priority_label=profile.priority_label,
            dscp=profile.dscp,
            dscp_name=profile.dscp_name,
            queue_id=profile.queue_id,
            queue_label=profile.queue_label,
            color=profile.color,
            message=message,
            created_wall_ms=created_wall_ms,
            tx_wall_ms=tx_wall_ms,
            tx_mono_ns=tx_mono_ns,
            ack_timeout_sec=timeout_s,
            max_retries=retries,
            retry_interval_sec=retry_int,
            congestion_active=bool(congestion_active),
            congestion_rate_mbps=float(congestion_rate_mbps),
            network_mode=network_mode or self.network_mode,
        )

        # Timeline steps 1, 2, 3
        alert.add_timeline(
            stage="Emergency message generated",
            detail=f"Alert {alert_id}: \"{message}\"",
            wall_ms=created_wall_ms,
        )
        alert.add_timeline(
            stage="Message classified",
            detail=f"Category: {profile.category} (DSCP {profile.dscp_name} = {profile.dscp}, ToS 0x{profile.tos:02X})",
            wall_ms=created_wall_ms + 0.05,
        )
        alert.add_timeline(
            stage="Priority assigned",
            detail=f"Priority {profile.priority} ({profile.priority_label}) → OVS {profile.queue_label}",
            wall_ms=created_wall_ms + 0.10,
        )

        self.ack_manager.register_alert(alert)

        # Sort clients deterministically (H2, H3, H4)
        registered.sort(key=lambda c: c["client_id"])

        for client in registered:
            cid = client["client_id"]
            cip = client["ip"]
            cport = int(client["port"])
            c_tx_wall, c_tx_mono = now_timestamps()
            self.ack_manager.add_target_client(
                alert_id=alert_id,
                client_id=cid,
                client_ip=cip,
                client_port=cport,
                tx_wall_ms=c_tx_wall,
                tx_mono_ns=c_tx_mono,
            )
            pkt = build_alert_packet(
                alert_id=alert_id,
                target_client=cid,
                category=profile.category,
                priority=profile.priority,
                dscp=profile.dscp,
                dscp_name=profile.dscp_name,
                queue_id=profile.queue_id,
                message=message,
                attempt=1,
                tx_wall_ms=c_tx_wall,
                tx_mono_ns=c_tx_mono,
                payload_bytes=payload_bytes,
            )
            self._send_udp_with_dscp(pkt, (cip, cport), tos=profile.tos)
            alert.add_timeline(
                stage="UDP packet transmitted",
                detail=f"H1 → {cid} ({cip}:{cport}) | DSCP={profile.dscp} ({profile.dscp_name}) | Queue {profile.queue_id}",
                wall_ms=c_tx_wall,
                client_id=cid,
            )

        # Timeline steps 5 & 6 (labeled clearly based on mode; controller flow delta updates verified status)
        is_sdn = (alert.network_mode == "MININET_SDN")
        alert.add_timeline(
            stage="Packet forwarded through S1",
            detail=(
                f"Ingress Switch S1 matches ip_dscp={profile.dscp} → SetQueue({profile.queue_id}) → Port 3 (Trunk to S2)"
                if is_sdn
                else f"Topology illustration: Ingress Switch S1 QoS Queue {profile.queue_id} (Loopback test mode)"
            ),
            wall_ms=tx_wall_ms + 0.25,
            verified=is_sdn,
        )
        alert.add_timeline(
            stage="Packet forwarded through S2",
            detail=(
                f"Egress Switch S2 forwards DSCP={profile.dscp} packets to registered clients {', '.join(c['client_id'] for c in registered)}"
                if is_sdn
                else f"Topology illustration: Egress Switch S2 forwarding to {', '.join(c['client_id'] for c in registered)} (Loopback test mode)"
            ),
            wall_ms=tx_wall_ms + 0.50,
            verified=is_sdn,
        )

        result = {
            "alert": alert.to_dict(),
            "alert_metrics": compute_alert_metrics(alert),
        }
        self._emit_event("alert_transmitted", result)
        return result

    def get_clients(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted([dict(c) for c in self._clients.values()], key=lambda x: x["client_id"])

    def get_state(self) -> Dict[str, Any]:
        alerts = self.ack_manager.list_alerts()
        latest = alerts[-1] if alerts else None
        return {
            "server_running": self._running,
            "bind_ip": self.bind_ip,
            "udp_port": self.udp_port,
            "network_mode": self.network_mode,
            "policy": {
                "ack_timeout_sec": self.ack_timeout_sec,
                "max_retries": self.max_retries,
                "retry_interval_sec": self.retry_interval_sec,
            },
            "clients": self.get_clients(),
            "latest_alert": latest.to_dict() if latest else None,
            "latest_alert_metrics": compute_alert_metrics(latest) if latest else None,
            "recent_alerts": [compute_alert_metrics(a) for a in reversed(alerts[-25:])],
            "aggregate_metrics": compute_aggregate_metrics(alerts),
        }

    def update_policy(
        self,
        ack_timeout_sec: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_interval_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if ack_timeout_sec is not None:
                self.ack_timeout_sec = max(0.2, float(ack_timeout_sec))
            if max_retries is not None:
                self.max_retries = max(0, int(max_retries))
            if retry_interval_sec is not None:
                self.retry_interval_sec = max(0.1, float(retry_interval_sec))
            return {
                "ack_timeout_sec": self.ack_timeout_sec,
                "max_retries": self.max_retries,
                "retry_interval_sec": self.retry_interval_sec,
            }

    def reset_metrics(self) -> None:
        self.ack_manager.clear()
        self._emit_event("metrics_reset", {})

    # ------------------------------------------------------------------
    # UNIX Domain Socket IPC (bridges H1 Mininet namespace <-> Flask API)
    # ------------------------------------------------------------------
    def _start_ipc_server(self) -> None:
        assert self.ipc_sock_path is not None
        if os.path.exists(self.ipc_sock_path):
            try:
                os.unlink(self.ipc_sock_path)
            except OSError:
                pass
        self._ipc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._ipc_sock.bind(self.ipc_sock_path)
        os.chmod(self.ipc_sock_path, 0o666)
        self._ipc_sock.listen(16)
        self._ipc_sock.settimeout(0.3)

        t_ipc = threading.Thread(target=self._ipc_accept_loop, name="ipc-accept", daemon=True)
        t_ipc.start()
        self._threads.append(t_ipc)

    def _ipc_accept_loop(self) -> None:
        while self._running and self._ipc_sock:
            try:
                conn, _ = self._ipc_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_ipc_client, args=(conn,), daemon=True).start()

    def _handle_ipc_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    conn.close()
                    return
                buf += chunk
            line = buf.split(b"\n", 1)[0].decode("utf-8")
            req = json.loads(line)
            cmd = req.get("cmd")

            if cmd == "subscribe":
                conn.settimeout(None)
                with self._sub_lock:
                    self._subscribers.append(conn)
                # Keep socket open for streaming events
                return

            resp = self._dispatch_ipc_cmd(cmd, req)
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            with self._sub_lock:
                is_sub = conn in self._subscribers
            if not is_sub:
                try:
                    conn.close()
                except OSError:
                    pass

    def _dispatch_ipc_cmd(self, cmd: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid(), "bind_ip": self.bind_ip, "udp_port": self.udp_port}
        if cmd == "get_state":
            return {"ok": True, "state": self.get_state()}
        if cmd == "send_alert":
            result = self.send_alert(
                category=req.get("category"),
                message=req.get("message"),
                ack_timeout_sec=req.get("ack_timeout_sec"),
                max_retries=req.get("max_retries"),
                retry_interval_sec=req.get("retry_interval_sec"),
                congestion_active=bool(req.get("congestion_active", False)),
                congestion_rate_mbps=float(req.get("congestion_rate_mbps", 0.0)),
                network_mode=req.get("network_mode"),
                payload_bytes=int(req.get("payload_bytes", 0)),
            )
            return {"ok": True, "result": result}
        if cmd == "get_alert":
            alert = self.ack_manager.get_alert(str(req.get("alert_id", "")))
            if not alert:
                return {"ok": False, "error": "Alert not found"}
            return {"ok": True, "alert": alert.to_dict(), "alert_metrics": compute_alert_metrics(alert)}
        if cmd == "update_policy":
            policy = self.update_policy(
                ack_timeout_sec=req.get("ack_timeout_sec"),
                max_retries=req.get("max_retries"),
                retry_interval_sec=req.get("retry_interval_sec"),
            )
            return {"ok": True, "policy": policy}
        if cmd == "reset_metrics":
            self.reset_metrics()
            return {"ok": True}
        return {"ok": False, "error": f"Unknown command: {cmd}"}


def ipc_call(req: Dict[str, Any], sock_path: str = SERVER_IPC_SOCK, timeout: float = 5.0) -> Dict[str, Any]:
    """Helper for Flask/scripts to invoke commands on the H1 UDP server across namespaces."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            raise RuntimeError("Empty IPC response from H1 server")
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="UDP Emergency Notification Server (H1)")
    parser.add_argument("--bind", default="0.0.0.0", help="IP address to bind UDP socket")
    parser.add_argument("--port", type=int, default=SERVER_UDP_PORT, help="UDP listening port")
    parser.add_argument("--ipc", default=SERVER_IPC_SOCK, help="UNIX domain socket path for IPC")
    parser.add_argument("--timeout", type=float, default=DEFAULT_ACK_TIMEOUT_SEC, help="ACK timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help="Max UDP retransmissions")
    parser.add_argument("--mode", default="MININET_SDN", help="Network mode label (MININET_SDN or LOOPBACK_TEST)")
    args = parser.parse_args()

    srv = EmergencyNotificationServer(
        bind_ip=args.bind,
        udp_port=args.port,
        ipc_sock_path=args.ipc,
        ack_timeout_sec=args.timeout,
        max_retries=args.retries,
        network_mode=args.mode,
    )

    stop_event = threading.Event()

    def _sig_handler(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    srv.start()
    print(f"[H1-SERVER] UDP server listening on {args.bind}:{args.port} | IPC: {args.ipc} | Mode: {args.mode}", flush=True)

    try:
        while not stop_event.is_set():
            time.sleep(0.25)
    finally:
        srv.stop()
        print("[H1-SERVER] Stopped cleanly.", flush=True)


if __name__ == "__main__":
    main()

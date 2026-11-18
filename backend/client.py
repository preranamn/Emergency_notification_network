"""UDP Emergency Notification Client (runs inside Mininet hosts H2, H3, H4 or locally)."""

import argparse
import json
from pathlib import Path
import random
import signal
import socket
import sys
import threading
import time
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import CLIENT_EVIDENCE_DIR, CLIENT_UDP_PORTS, SERVER_UDP_PORT
from backend.protocol import (
    build_ack_packet,
    build_heartbeat_packet,
    build_register_packet,
    decode_packet,
    encode_packet,
    now_timestamps,
)


class EmergencyNotificationClient:
    """UDP Client that registers with H1, receives alerts, logs evidence, and sends ACKs."""

    def __init__(
        self,
        client_id: str,
        server_ip: str = "10.0.0.1",
        server_port: int = SERVER_UDP_PORT,
        bind_ip: str = "0.0.0.0",
        listen_port: Optional[int] = None,
        evidence_dir: Path = CLIENT_EVIDENCE_DIR,
        quiet: bool = False,
    ):
        self.client_id = client_id.upper()
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.bind_ip = bind_ip
        self.listen_port = int(listen_port or CLIENT_UDP_PORTS.get(self.client_id, 9100))
        self.evidence_dir = Path(evidence_dir)
        self.quiet = quiet

        self._sock: Optional[socket.socket] = None
        self._running = False
        self._registered = False
        self._lock = threading.RLock()
        self._seen_alerts: Dict[str, Dict[str, Any]] = {}
        self._threads: list = []

    @property
    def registered(self) -> bool:
        return self._registered

    @property
    def evidence_path(self) -> Path:
        return self.evidence_dir / f"{self.client_id}.jsonl"

    @property
    def control_path(self) -> Path:
        return self.evidence_dir / f"{self.client_id}_control.json"

    def _read_fault_control(self) -> Dict[str, Any]:
        """Optional fault-injection control file for controlled ACK-drop / forward-drop testing."""
        if not self.control_path.exists():
            return {}
        try:
            return json.loads(self.control_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        # Touch evidence log so server knows client is alive and recording reception evidence
        self.evidence_path.touch(exist_ok=True)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_ip, self.listen_port))
        self._sock.settimeout(0.2)

        t_rx = threading.Thread(target=self._recv_loop, name=f"{self.client_id}-rx", daemon=True)
        t_reg = threading.Thread(target=self._register_loop, name=f"{self.client_id}-reg", daemon=True)
        t_rx.start()
        t_reg.start()
        self._threads = [t_rx, t_reg]

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()

    def _send_to_server(self, payload: Dict[str, Any], dscp: int = 0) -> None:
        if not self._sock:
            return
        raw = encode_packet(payload)
        tos = (int(dscp) & 0x3F) << 2
        with self._lock:
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
            except OSError:
                pass
            self._sock.sendto(raw, (self.server_ip, self.server_port))

    def _register_loop(self) -> None:
        while self._running:
            try:
                if not self._registered:
                    pkt = build_register_packet(self.client_id, self.bind_ip, self.listen_port)
                    self._send_to_server(pkt, dscp=0)
                    time.sleep(0.5)
                else:
                    hb = build_heartbeat_packet(self.client_id, self.bind_ip, self.listen_port)
                    self._send_to_server(hb, dscp=0)
                    for _ in range(30):
                        if not self._running:
                            break
                        time.sleep(0.1)
            except OSError:
                time.sleep(0.5)

    def _record_evidence(self, alert_id: str, category: str, message: str, rx_wall_ms: float, rx_mono_ns: int, ack_tx_wall_ms: Optional[float]) -> None:
        entry = {
            "client_id": self.client_id,
            "alert_id": alert_id,
            "category": category,
            "message": message,
            "client_rx_wall_ms": rx_wall_ms,
            "client_rx_mono_ns": rx_mono_ns,
            "ack_tx_wall_ms": ack_tx_wall_ms,
        }
        try:
            with self.evidence_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def _recv_loop(self) -> None:
        while self._running and self._sock:
            try:
                raw, _addr = self._sock.recvfrom(65535)
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
            if pkt_type == "REGISTER_ACK":
                if not self._registered and not self.quiet:
                    print(f"[{self.client_id}] Registered successfully with H1 ({self.server_ip}:{self.server_port})", flush=True)
                self._registered = True
            elif pkt_type == "ALERT":
                self._handle_alert(pkt, rx_wall_ms, rx_mono_ns)

    def _handle_alert(self, pkt: Dict[str, Any], rx_wall_ms: float, rx_mono_ns: int) -> None:
        alert_id = str(pkt.get("alert_id", ""))
        category = str(pkt.get("category", "GENERAL"))
        dscp = int(pkt.get("dscp", 0))
        message = str(pkt.get("message", ""))
        attempt = int(pkt.get("attempt", 1))
        orig_tx_wall_ms = float(pkt.get("tx_wall_ms", rx_wall_ms))
        orig_tx_mono_ns = int(pkt.get("tx_mono_ns", rx_mono_ns))

        # Check optional controlled fault injection (Test 6 support)
        fault = self._read_fault_control()
        drop_fwd_prob = float(fault.get("drop_forward_prob", 0.0))
        if drop_fwd_prob > 0.0 and random.random() < drop_fwd_prob:
            if not self.quiet:
                print(f"[{self.client_id}] [FAULT-INJECT] Dropped incoming ALERT {alert_id} (attempt {attempt})", flush=True)
            return

        with self._lock:
            existing = self._seen_alerts.get(alert_id)
            if existing is None:
                # First time receiving this alert — store original reception time
                first_rx_wall_ms = rx_wall_ms
                first_rx_mono_ns = rx_mono_ns
                self._seen_alerts[alert_id] = {
                    "alert_id": alert_id,
                    "category": category,
                    "message": message,
                    "rx_wall_ms": first_rx_wall_ms,
                    "rx_mono_ns": first_rx_mono_ns,
                }
                is_first = True
            else:
                first_rx_wall_ms = existing["rx_wall_ms"]
                first_rx_mono_ns = existing["rx_mono_ns"]
                is_first = False

        if is_first and not self.quiet:
            print(
                f"[{self.client_id}] RECEIVED [{category} | DSCP {dscp}] {alert_id}: {message}",
                flush=True,
            )

        ack_tx_wall_ms, ack_tx_mono_ns = now_timestamps()
        if is_first:
            self._record_evidence(alert_id, category, message, first_rx_wall_ms, first_rx_mono_ns, ack_tx_wall_ms)

        # Optional ACK-drop fault injection (tests distinction between ACK failure vs delivery failure)
        drop_ack_prob = float(fault.get("drop_ack_prob", 0.0))
        if drop_ack_prob > 0.0 and random.random() < drop_ack_prob:
            if not self.quiet:
                print(f"[{self.client_id}] [FAULT-INJECT] Suppressed ACK for {alert_id} (delivery succeeded)", flush=True)
            return

        ack_pkt = build_ack_packet(
            alert_id=alert_id,
            client_id=self.client_id,
            category=category,
            dscp=dscp,
            attempt=attempt,
            orig_tx_wall_ms=orig_tx_wall_ms,
            orig_tx_mono_ns=orig_tx_mono_ns,
            client_rx_wall_ms=first_rx_wall_ms,
            client_rx_mono_ns=first_rx_mono_ns,
            ack_tx_wall_ms=ack_tx_wall_ms,
            ack_tx_mono_ns=ack_tx_mono_ns,
        )
        try:
            self._send_to_server(ack_pkt, dscp=dscp)
        except OSError:
            pass

    def get_received_alerts(self) -> list:
        with self._lock:
            return list(self._seen_alerts.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="UDP Emergency Notification Client")
    parser.add_argument("--id", required=True, help="Client ID (e.g., H2, H3, H4)")
    parser.add_argument("--server-ip", default="10.0.0.1", help="Emergency server IP (H1)")
    parser.add_argument("--server-port", type=int, default=SERVER_UDP_PORT, help="Emergency server UDP port")
    parser.add_argument("--bind", default="0.0.0.0", help="Local bind IP")
    parser.add_argument("--port", type=int, default=None, help="Local UDP listen port")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout alert banners")
    args = parser.parse_args()

    client = EmergencyNotificationClient(
        client_id=args.id,
        server_ip=args.server_ip,
        server_port=args.server_port,
        bind_ip=args.bind,
        listen_port=args.port,
        quiet=args.quiet,
    )

    stop_event = threading.Event()

    def _sig_handler(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    client.start()
    if not args.quiet:
        print(
            f"[{client.client_id}] Listening on {client.bind_ip}:{client.listen_port} -> H1 {client.server_ip}:{client.server_port}",
            flush=True,
        )
    try:
        while not stop_event.is_set():
            time.sleep(0.25)
    finally:
        client.stop()


if __name__ == "__main__":
    main()

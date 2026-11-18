"""Application-level acknowledgement and delivery-tracking engine."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend.config import CLIENT_EVIDENCE_DIR, FORWARDING_PATHS, REVERSE_PATHS


STATUS_PENDING = "Pending"
STATUS_DELIVERED = "Delivered"
STATUS_ACK_RECEIVED = "ACK Received"
STATUS_TIMEOUT = "Timeout"
STATUS_FAILED = "Failed"

FAILURE_NONE = "NONE"
FAILURE_DELIVERY = "DELIVERY_FAILED"
FAILURE_ACK_LOST = "ACK_LOST"
FAILURE_UNCONFIRMED = "UNCONFIRMED_TIMEOUT"


@dataclass
class ClientDeliveryRecord:
    alert_id: str
    client_id: str
    category: str
    priority: int
    dscp: int
    queue_id: int
    client_ip: str
    client_port: int
    orig_tx_wall_ms: float
    orig_tx_mono_ns: int
    last_tx_wall_ms: float
    last_tx_mono_ns: int
    status: str = STATUS_PENDING
    failure_reason: str = FAILURE_NONE
    attempts: int = 1
    client_rx_wall_ms: Optional[float] = None
    client_rx_mono_ns: Optional[int] = None
    ack_tx_wall_ms: Optional[float] = None
    ack_tx_mono_ns: Optional[int] = None
    server_ack_rx_wall_ms: Optional[float] = None
    server_ack_rx_mono_ns: Optional[int] = None
    delivery_latency_ms: Optional[float] = None
    rtt_ms: Optional[float] = None
    duplicate_acks_ignored: int = 0
    forwarding_path: List[str] = field(default_factory=list)
    reverse_path: List[str] = field(default_factory=list)
    link_hops: int = 3
    switch_hops: int = 2
    finalized: bool = False

    def to_dict(self) -> Dict:
        return {
            "alert_id": self.alert_id,
            "client_id": self.client_id,
            "category": self.category,
            "priority": self.priority,
            "dscp": self.dscp,
            "queue_id": self.queue_id,
            "client_ip": self.client_ip,
            "client_port": self.client_port,
            "orig_tx_wall_ms": round(self.orig_tx_wall_ms, 3),
            "client_rx_wall_ms": round(self.client_rx_wall_ms, 3) if self.client_rx_wall_ms is not None else None,
            "ack_tx_wall_ms": round(self.ack_tx_wall_ms, 3) if self.ack_tx_wall_ms is not None else None,
            "server_ack_rx_wall_ms": round(self.server_ack_rx_wall_ms, 3) if self.server_ack_rx_wall_ms is not None else None,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "attempts": self.attempts,
            "delivery_latency_ms": round(self.delivery_latency_ms, 3) if self.delivery_latency_ms is not None else None,
            "rtt_ms": round(self.rtt_ms, 3) if self.rtt_ms is not None else None,
            "duplicate_acks_ignored": self.duplicate_acks_ignored,
            "forwarding_path": self.forwarding_path,
            "forwarding_path_str": " → ".join(self.forwarding_path),
            "reverse_path": self.reverse_path,
            "link_hops": self.link_hops,
            "switch_hops": self.switch_hops,
            "finalized": self.finalized,
        }


@dataclass
class AlertRecord:
    alert_id: str
    category: str
    priority: int
    priority_label: str
    dscp: int
    dscp_name: str
    queue_id: int
    queue_label: str
    color: str
    message: str
    created_wall_ms: float
    tx_wall_ms: float
    tx_mono_ns: int
    ack_timeout_sec: float
    max_retries: int
    retry_interval_sec: float
    congestion_active: bool = False
    congestion_rate_mbps: float = 0.0
    network_mode: str = "MININET_SDN"
    clients: Dict[str, ClientDeliveryRecord] = field(default_factory=dict)
    completed: bool = False
    completed_wall_ms: Optional[float] = None
    sdn_stats_before: Optional[Dict] = None
    sdn_stats_after: Optional[Dict] = None
    sdn_flow_verified: bool = False
    timeline: List[Dict] = field(default_factory=list)

    def add_timeline(self, stage: str, detail: str, wall_ms: Optional[float] = None, client_id: Optional[str] = None, verified: bool = True) -> Dict:
        entry = {
            "alert_id": self.alert_id,
            "stage": stage,
            "detail": detail,
            "client_id": client_id,
            "verified": verified,
            "ts_wall_ms": round(wall_ms if wall_ms is not None else time.time() * 1000.0, 3),
        }
        self.timeline.append(entry)
        return entry

    def is_all_finalized(self) -> bool:
        return bool(self.clients) and all(c.finalized for c in self.clients.values())

    def to_dict(self) -> Dict:
        return {
            "alert_id": self.alert_id,
            "category": self.category,
            "priority": self.priority,
            "priority_label": self.priority_label,
            "dscp": self.dscp,
            "dscp_name": self.dscp_name,
            "queue_id": self.queue_id,
            "queue_label": self.queue_label,
            "color": self.color,
            "message": self.message,
            "created_wall_ms": round(self.created_wall_ms, 3),
            "tx_wall_ms": round(self.tx_wall_ms, 3),
            "ack_timeout_sec": self.ack_timeout_sec,
            "max_retries": self.max_retries,
            "retry_interval_sec": self.retry_interval_sec,
            "congestion_active": self.congestion_active,
            "congestion_rate_mbps": self.congestion_rate_mbps,
            "network_mode": self.network_mode,
            "completed": self.completed,
            "completed_wall_ms": round(self.completed_wall_ms, 3) if self.completed_wall_ms else None,
            "sdn_flow_verified": self.sdn_flow_verified,
            "clients": {cid: rec.to_dict() for cid, rec in self.clients.items()},
            "timeline": list(self.timeline),
        }


def lookup_client_evidence(client_id: str, alert_id: str, evidence_dir: Path = CLIENT_EVIDENCE_DIR) -> Optional[Dict]:
    """Check out-of-band client reception log to distinguish delivery failure vs ACK loss.

    The UDP protocol itself NEVER uses this file for delivery or ACKs. It is inspected ONLY
    after an ACK timeout to accurately report whether the client received the packet (ACK_LOST)
    or never received the packet (DELIVERY_FAILED).
    """
    log_path = evidence_dir / f"{client_id}.jsonl"
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("alert_id") == alert_id:
                    return item
    except OSError:
        return None
    return None


class AcknowledgementManager:
    """Thread-safe tracker for alert transmissions, ACKs, retries, and timeouts."""

    def __init__(self, evidence_dir: Path = CLIENT_EVIDENCE_DIR):
        self._lock = threading.RLock()
        self._alerts: Dict[str, AlertRecord] = {}
        self._alert_order: List[str] = []
        self._evidence_dir = evidence_dir

    def register_alert(self, alert: AlertRecord) -> None:
        with self._lock:
            self._alerts[alert.alert_id] = alert
            self._alert_order.append(alert.alert_id)

    def add_target_client(
        self,
        alert_id: str,
        client_id: str,
        client_ip: str,
        client_port: int,
        tx_wall_ms: float,
        tx_mono_ns: int,
    ) -> ClientDeliveryRecord:
        with self._lock:
            alert = self._alerts[alert_id]
            fwd_path = list(FORWARDING_PATHS.get(client_id, ["H1", "S1", "S2", client_id]))
            rev_path = list(REVERSE_PATHS.get(client_id, [client_id, "S2", "S1", "H1"]))
            rec = ClientDeliveryRecord(
                alert_id=alert_id,
                client_id=client_id,
                category=alert.category,
                priority=alert.priority,
                dscp=alert.dscp,
                queue_id=alert.queue_id,
                client_ip=client_ip,
                client_port=client_port,
                orig_tx_wall_ms=tx_wall_ms,
                orig_tx_mono_ns=tx_mono_ns,
                last_tx_wall_ms=tx_wall_ms,
                last_tx_mono_ns=tx_mono_ns,
                forwarding_path=fwd_path,
                reverse_path=rev_path,
                link_hops=max(1, len(fwd_path) - 1),
                switch_hops=sum(1 for node in fwd_path if node.startswith("S")),
            )
            alert.clients[client_id] = rec
            return rec

    def record_retransmission(self, alert_id: str, client_id: str, tx_wall_ms: float, tx_mono_ns: int) -> Optional[ClientDeliveryRecord]:
        with self._lock:
            alert = self._alerts.get(alert_id)
            if not alert or client_id not in alert.clients:
                return None
            rec = alert.clients[client_id]
            if rec.finalized:
                return None
            rec.attempts += 1
            rec.last_tx_wall_ms = tx_wall_ms
            rec.last_tx_mono_ns = tx_mono_ns
            return rec

    def process_ack(
        self,
        alert_id: str,
        client_id: str,
        client_rx_wall_ms: float,
        client_rx_mono_ns: int,
        ack_tx_wall_ms: float,
        ack_tx_mono_ns: int,
        server_ack_rx_wall_ms: float,
        server_ack_rx_mono_ns: int,
    ) -> Tuple[Optional[AlertRecord], Optional[ClientDeliveryRecord], bool]:
        """Process an incoming ACK packet.

        Returns (alert, record, is_duplicate).
        Prevents duplicate delivery records when retransmitted ACKs arrive.
        """
        with self._lock:
            alert = self._alerts.get(alert_id)
            if not alert or client_id not in alert.clients:
                return None, None, False

            rec = alert.clients[client_id]
            if rec.status == STATUS_ACK_RECEIVED:
                rec.duplicate_acks_ignored += 1
                return alert, rec, True

            # Compute precise delivery latency and RTT using monotonic kernel clock
            # (fallback to wall clock if monotonic delta is negative across distinct hosts)
            lat_ns = client_rx_mono_ns - rec.orig_tx_mono_ns
            if lat_ns >= 0:
                delivery_latency_ms = lat_ns / 1_000_000.0
            else:
                delivery_latency_ms = max(0.0, client_rx_wall_ms - rec.orig_tx_wall_ms)

            rtt_ns = server_ack_rx_mono_ns - rec.orig_tx_mono_ns
            if rtt_ns >= 0:
                rtt_ms = rtt_ns / 1_000_000.0
            else:
                rtt_ms = max(0.0, server_ack_rx_wall_ms - rec.orig_tx_wall_ms)

            rec.client_rx_wall_ms = client_rx_wall_ms
            rec.client_rx_mono_ns = client_rx_mono_ns
            rec.ack_tx_wall_ms = ack_tx_wall_ms
            rec.ack_tx_mono_ns = ack_tx_mono_ns
            rec.server_ack_rx_wall_ms = server_ack_rx_wall_ms
            rec.server_ack_rx_mono_ns = server_ack_rx_mono_ns
            rec.delivery_latency_ms = delivery_latency_ms
            rec.rtt_ms = rtt_ms
            rec.status = STATUS_ACK_RECEIVED
            rec.failure_reason = FAILURE_NONE
            rec.finalized = True

            return alert, rec, False

    def evaluate_timeouts_and_retries(self, now_mono_ns: int, now_wall_ms: float) -> Tuple[List[Tuple[AlertRecord, ClientDeliveryRecord]], List[Tuple[AlertRecord, ClientDeliveryRecord]], List[AlertRecord]]:
        """Scan active alerts for needed retransmissions, timeouts, and completions.

        Returns:
            retries_due: list of (alert, record) needing UDP retransmission
            timed_out: list of (alert, record) whose ACK timeout just expired
            newly_completed: list of alerts where all clients are now finalized
        """
        retries_due: List[Tuple[AlertRecord, ClientDeliveryRecord]] = []
        timed_out: List[Tuple[AlertRecord, ClientDeliveryRecord]] = []
        newly_completed: List[AlertRecord] = []

        with self._lock:
            for alert_id in list(self._alert_order):
                alert = self._alerts[alert_id]
                if alert.completed:
                    continue

                for rec in alert.clients.values():
                    if rec.finalized:
                        continue

                    elapsed_since_orig = (now_mono_ns - rec.orig_tx_mono_ns) / 1e9
                    elapsed_since_last = (now_mono_ns - rec.last_tx_mono_ns) / 1e9

                    # Check overall timeout first
                    if elapsed_since_orig >= alert.ack_timeout_sec:
                        self._finalize_timeout_record(rec)
                        timed_out.append((alert, rec))
                    elif (
                        rec.attempts <= alert.max_retries
                        and elapsed_since_last >= alert.retry_interval_sec
                    ):
                        retries_due.append((alert, rec))

                if alert.is_all_finalized():
                    alert.completed = True
                    alert.completed_wall_ms = now_wall_ms
                    newly_completed.append(alert)

        return retries_due, timed_out, newly_completed

    def _finalize_timeout_record(self, rec: ClientDeliveryRecord) -> None:
        """Distinguish delivery failure vs ACK loss using out-of-band client reception evidence."""
        evidence_file = self._evidence_dir / f"{rec.client_id}.jsonl"
        evidence = lookup_client_evidence(rec.client_id, rec.alert_id, self._evidence_dir)

        if evidence is not None:
            # The client DID receive the notification, so the forward delivery succeeded
            # and only the ACK failed to return!
            rec.status = STATUS_DELIVERED
            rec.failure_reason = FAILURE_ACK_LOST
            rx_wall = evidence.get("client_rx_wall_ms")
            rx_mono = evidence.get("client_rx_mono_ns")
            ack_tx = evidence.get("ack_tx_wall_ms")
            rec.client_rx_wall_ms = rx_wall
            rec.client_rx_mono_ns = rx_mono
            rec.ack_tx_wall_ms = ack_tx
            if rx_mono is not None and rx_mono >= rec.orig_tx_mono_ns:
                rec.delivery_latency_ms = (rx_mono - rec.orig_tx_mono_ns) / 1_000_000.0
            elif rx_wall is not None:
                rec.delivery_latency_ms = max(0.0, rx_wall - rec.orig_tx_wall_ms)
            rec.rtt_ms = None  # ACK never reached server
        elif evidence_file.exists():
            # Client is alive and logging receptions, yet never received this alert_id -> confirmed packet loss
            rec.status = STATUS_FAILED
            rec.failure_reason = FAILURE_DELIVERY
        else:
            # Evidence unavailable -> do not falsely claim confirmed packet loss
            rec.status = STATUS_TIMEOUT
            rec.failure_reason = FAILURE_UNCONFIRMED

        rec.finalized = True

    def get_alert(self, alert_id: str) -> Optional[AlertRecord]:
        with self._lock:
            return self._alerts.get(alert_id)

    def list_alerts(self) -> List[AlertRecord]:
        with self._lock:
            return [self._alerts[aid] for aid in self._alert_order]

    def latest_alert(self) -> Optional[AlertRecord]:
        with self._lock:
            if not self._alert_order:
                return None
            return self._alerts[self._alert_order[-1]]

    def clear(self) -> None:
        with self._lock:
            self._alerts.clear()
            self._alert_order.clear()

"""Network performance metrics calculation and CSV persistence."""

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.acknowledgement import (
    AlertRecord,
    ClientDeliveryRecord,
    FAILURE_ACK_LOST,
    FAILURE_DELIVERY,
    FAILURE_UNCONFIRMED,
    STATUS_ACK_RECEIVED,
    STATUS_DELIVERED,
)
from backend.config import ACKS_CSV, ALERTS_CSV, CLIENT_IDS, EXPERIMENTS_CSV


def _safe_avg(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _safe_min(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(min(values), 3)


def _safe_max(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(max(values), 3)


def compute_alert_metrics(alert: AlertRecord) -> Dict[str, Any]:
    """Compute exact per-alert metrics from actual recorded timestamps."""
    records: List[ClientDeliveryRecord] = list(alert.clients.values())
    intended = len(records)
    udp_transmissions = sum(r.attempts for r in records)

    delivered_records = [r for r in records if r.status in (STATUS_ACK_RECEIVED, STATUS_DELIVERED)]
    acked_records = [r for r in records if r.status == STATUS_ACK_RECEIVED]
    delivery_failed = [r for r in records if r.failure_reason == FAILURE_DELIVERY]
    ack_lost = [r for r in records if r.failure_reason == FAILURE_ACK_LOST]
    unconfirmed_timeouts = [r for r in records if r.failure_reason == FAILURE_UNCONFIRMED]
    pending = [r for r in records if not r.finalized]

    packets_delivered = len(delivered_records)
    packets_acked = len(acked_records)
    # Distinguish confirmed/unconfirmed delivery loss from ACK-only loss
    packets_lost = len(delivery_failed) + len(unconfirmed_timeouts)

    pdr = round((packets_delivered / intended) * 100.0, 2) if intended > 0 else None
    plr = round((packets_lost / intended) * 100.0, 2) if intended > 0 else None
    ack_ratio = round((packets_acked / intended) * 100.0, 2) if intended > 0 else None

    latencies = [r.delivery_latency_ms for r in delivered_records if r.delivery_latency_ms is not None]
    rtts = [r.rtt_ms for r in acked_records if r.rtt_ms is not None]

    per_client: Dict[str, Dict[str, Any]] = {}
    for cid in CLIENT_IDS:
        rec = alert.clients.get(cid)
        if rec:
            per_client[cid] = {
                "status": rec.status,
                "failure_reason": rec.failure_reason,
                "delivery_latency_ms": round(rec.delivery_latency_ms, 3) if rec.delivery_latency_ms is not None else None,
                "rtt_ms": round(rec.rtt_ms, 3) if rec.rtt_ms is not None else None,
                "attempts": rec.attempts,
                "forwarding_path": rec.forwarding_path,
                "forwarding_path_str": " → ".join(rec.forwarding_path),
                "link_hops": rec.link_hops,
                "switch_hops": rec.switch_hops,
                "orig_tx_wall_ms": round(rec.orig_tx_wall_ms, 3),
                "client_rx_wall_ms": round(rec.client_rx_wall_ms, 3) if rec.client_rx_wall_ms is not None else None,
                "ack_tx_wall_ms": round(rec.ack_tx_wall_ms, 3) if rec.ack_tx_wall_ms is not None else None,
                "server_ack_rx_wall_ms": round(rec.server_ack_rx_wall_ms, 3) if rec.server_ack_rx_wall_ms is not None else None,
            }
        else:
            per_client[cid] = {
                "status": "Unregistered",
                "failure_reason": "NONE",
                "delivery_latency_ms": None,
                "rtt_ms": None,
                "attempts": 0,
                "forwarding_path": ["H1", "S1", "S2", cid],
                "forwarding_path_str": f"H1 → S1 → S2 → {cid}",
                "link_hops": 3,
                "switch_hops": 2,
                "orig_tx_wall_ms": None,
                "client_rx_wall_ms": None,
                "ack_tx_wall_ms": None,
                "server_ack_rx_wall_ms": None,
            }

    sample_rec = records[0] if records else None
    link_hops = sample_rec.link_hops if sample_rec else 3
    switch_hops = sample_rec.switch_hops if sample_rec else 2

    return {
        "alert_id": alert.alert_id,
        "category": alert.category,
        "priority": alert.priority,
        "priority_label": alert.priority_label,
        "dscp": alert.dscp,
        "dscp_name": alert.dscp_name,
        "queue_id": alert.queue_id,
        "message": alert.message,
        "congestion_active": alert.congestion_active,
        "congestion_rate_mbps": alert.congestion_rate_mbps,
        "network_mode": alert.network_mode,
        "completed": alert.completed,
        "sdn_flow_verified": alert.sdn_flow_verified,
        "logical_notifications": 1,
        "intended_deliveries": intended,
        "udp_transmissions": udp_transmissions,
        "packets_delivered": packets_delivered,
        "packets_acked": packets_acked,
        "packets_lost": packets_lost,
        "confirmed_delivery_failures": len(delivery_failed),
        "ack_failures": len(ack_lost),
        "unconfirmed_timeouts": len(unconfirmed_timeouts),
        "pending_count": len(pending),
        "pdr_percent": pdr,
        "plr_percent": plr,
        "ack_ratio_percent": ack_ratio,
        "avg_delivery_latency_ms": _safe_avg(latencies),
        "min_delivery_latency_ms": _safe_min(latencies),
        "max_delivery_latency_ms": _safe_max(latencies),
        "avg_rtt_ms": _safe_avg(rtts),
        "min_rtt_ms": _safe_min(rtts),
        "max_rtt_ms": _safe_max(rtts),
        "forwarding_path": "H1 → S1 → S2 → H2/H3/H4",
        "forwarding_link_hops": link_hops,
        "forwarding_switch_hops": switch_hops,
        "per_client": per_client,
        "tx_wall_ms": round(alert.tx_wall_ms, 3),
        "completed_wall_ms": round(alert.completed_wall_ms, 3) if alert.completed_wall_ms else None,
    }


def compute_aggregate_metrics(alerts: List[AlertRecord]) -> Dict[str, Any]:
    """Compute overall and category-wise metrics across all recorded alerts."""
    total_logical = len(alerts)
    all_records: List[ClientDeliveryRecord] = []
    for a in alerts:
        all_records.extend(a.clients.values())

    intended = len(all_records)
    udp_tx = sum(r.attempts for r in all_records)
    delivered = [r for r in all_records if r.status in (STATUS_ACK_RECEIVED, STATUS_DELIVERED)]
    acked = [r for r in all_records if r.status == STATUS_ACK_RECEIVED]
    delivery_failed = [r for r in all_records if r.failure_reason == FAILURE_DELIVERY]
    ack_lost = [r for r in all_records if r.failure_reason == FAILURE_ACK_LOST]
    unconfirmed = [r for r in all_records if r.failure_reason == FAILURE_UNCONFIRMED]
    packets_lost = len(delivery_failed) + len(unconfirmed)

    latencies = [r.delivery_latency_ms for r in delivered if r.delivery_latency_ms is not None]
    rtts = [r.rtt_ms for r in acked if r.rtt_ms is not None]

    # Category-wise & condition-wise comparison
    by_category: Dict[str, Dict[str, Any]] = {}
    by_category_condition: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for cat in ("MEDICAL", "FIRE", "GENERAL"):
        cat_alerts = [a for a in alerts if a.category == cat]
        by_category[cat] = _summarize_subset(cat_alerts)
        by_category_condition[cat] = {
            "normal": _summarize_subset([a for a in cat_alerts if not a.congestion_active]),
            "congestion": _summarize_subset([a for a in cat_alerts if a.congestion_active]),
        }

    # Per-client overall averages
    per_client_avg: Dict[str, Dict[str, Optional[float]]] = {}
    for cid in CLIENT_IDS:
        c_recs = [r for r in all_records if r.client_id == cid]
        c_lats = [r.delivery_latency_ms for r in c_recs if r.delivery_latency_ms is not None]
        c_rtts = [r.rtt_ms for r in c_recs if r.rtt_ms is not None]
        per_client_avg[cid] = {
            "avg_delivery_latency_ms": _safe_avg(c_lats),
            "avg_rtt_ms": _safe_avg(c_rtts),
            "delivered": sum(1 for r in c_recs if r.status in (STATUS_ACK_RECEIVED, STATUS_DELIVERED)),
            "intended": len(c_recs),
        }

    return {
        "total_logical_alerts": total_logical,
        "total_intended_deliveries": intended,
        "total_udp_transmissions": udp_tx,
        "total_packets_delivered": len(delivered),
        "total_packets_acked": len(acked),
        "total_packets_lost": packets_lost,
        "confirmed_delivery_failures": len(delivery_failed),
        "ack_failures": len(ack_lost),
        "unconfirmed_timeouts": len(unconfirmed),
        "pdr_percent": round((len(delivered) / intended) * 100.0, 2) if intended > 0 else None,
        "plr_percent": round((packets_lost / intended) * 100.0, 2) if intended > 0 else None,
        "avg_delivery_latency_ms": _safe_avg(latencies),
        "min_delivery_latency_ms": _safe_min(latencies),
        "max_delivery_latency_ms": _safe_max(latencies),
        "avg_rtt_ms": _safe_avg(rtts),
        "forwarding_path": "H1 → S1 → S2 → H2/H3/H4",
        "forwarding_link_hops": 3,
        "forwarding_switch_hops": 2,
        "per_client": per_client_avg,
        "by_category": by_category,
        "by_category_condition": by_category_condition,
    }


def _summarize_subset(alerts: List[AlertRecord]) -> Dict[str, Any]:
    records = [r for a in alerts for r in a.clients.values()]
    intended = len(records)
    udp_tx = sum(r.attempts for r in records)
    delivered = [r for r in records if r.status in (STATUS_ACK_RECEIVED, STATUS_DELIVERED)]
    acked = [r for r in records if r.status == STATUS_ACK_RECEIVED]
    failed = [r for r in records if r.failure_reason in (FAILURE_DELIVERY, FAILURE_UNCONFIRMED)]
    ack_lost = [r for r in records if r.failure_reason == FAILURE_ACK_LOST]

    lats = [r.delivery_latency_ms for r in delivered if r.delivery_latency_ms is not None]
    rtts = [r.rtt_ms for r in acked if r.rtt_ms is not None]

    return {
        "alert_count": len(alerts),
        "intended_deliveries": intended,
        "udp_transmissions": udp_tx,
        "packets_delivered": len(delivered),
        "packets_acked": len(acked),
        "packets_lost": len(failed),
        "ack_failures": len(ack_lost),
        "pdr_percent": round((len(delivered) / intended) * 100.0, 2) if intended > 0 else None,
        "plr_percent": round((len(failed) / intended) * 100.0, 2) if intended > 0 else None,
        "avg_delivery_latency_ms": _safe_avg(lats),
        "min_delivery_latency_ms": _safe_min(lats),
        "max_delivery_latency_ms": _safe_max(lats),
        "avg_rtt_ms": _safe_avg(rtts),
    }


class MetricsCSVLogger:
    """Appends verified alert, ACK, and experiment records to CSV files in data/."""

    ALERTS_HEADER = [
        "alert_id", "timestamp_ms", "category", "priority", "dscp", "dscp_name",
        "queue_id", "message", "congestion_active", "congestion_rate_mbps",
        "network_mode", "intended_deliveries", "udp_transmissions",
        "packets_delivered", "packets_acked", "packets_lost", "ack_failures",
        "pdr_percent", "plr_percent", "avg_delivery_latency_ms",
        "min_delivery_latency_ms", "max_delivery_latency_ms", "avg_rtt_ms",
        "forwarding_path", "link_hops", "switch_hops",
    ]

    ACKS_HEADER = [
        "alert_id", "client_id", "category", "priority", "dscp", "queue_id",
        "status", "failure_reason", "attempts", "orig_tx_wall_ms",
        "client_rx_wall_ms", "ack_tx_wall_ms", "server_ack_rx_wall_ms",
        "delivery_latency_ms", "rtt_ms", "duplicate_acks_ignored",
        "forwarding_path", "link_hops",
    ]

    EXPERIMENTS_HEADER = [
        "run_id", "timestamp_ms", "scenario", "category", "condition",
        "congestion_rate_mbps", "alerts_sent", "intended_deliveries",
        "packets_delivered", "packets_lost", "ack_failures", "pdr_percent",
        "plr_percent", "avg_delivery_latency_ms", "min_delivery_latency_ms",
        "max_delivery_latency_ms", "avg_rtt_ms", "queue_tx_packets",
        "queue_tx_bytes", "network_mode",
    ]

    def __init__(
        self,
        alerts_csv: Path = ALERTS_CSV,
        acks_csv: Path = ACKS_CSV,
        experiments_csv: Path = EXPERIMENTS_CSV,
    ):
        self.alerts_csv = alerts_csv
        self.acks_csv = acks_csv
        self.experiments_csv = experiments_csv
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        for path, header in (
            (self.alerts_csv, self.ALERTS_HEADER),
            (self.acks_csv, self.ACKS_HEADER),
            (self.experiments_csv, self.EXPERIMENTS_HEADER),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.stat().st_size == 0:
                with path.open("w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(header)

    def log_completed_alert(self, alert: AlertRecord) -> None:
        m = compute_alert_metrics(alert)
        with self.alerts_csv.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([
                alert.alert_id,
                round(alert.tx_wall_ms, 3),
                alert.category,
                alert.priority,
                alert.dscp,
                alert.dscp_name,
                alert.queue_id,
                alert.message,
                alert.congestion_active,
                alert.congestion_rate_mbps,
                alert.network_mode,
                m["intended_deliveries"],
                m["udp_transmissions"],
                m["packets_delivered"],
                m["packets_acked"],
                m["packets_lost"],
                m["ack_failures"],
                m["pdr_percent"],
                m["plr_percent"],
                m["avg_delivery_latency_ms"],
                m["min_delivery_latency_ms"],
                m["max_delivery_latency_ms"],
                m["avg_rtt_ms"],
                m["forwarding_path"],
                m["forwarding_link_hops"],
                m["forwarding_switch_hops"],
            ])

        with self.acks_csv.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            for rec in alert.clients.values():
                writer.writerow([
                    rec.alert_id,
                    rec.client_id,
                    rec.category,
                    rec.priority,
                    rec.dscp,
                    rec.queue_id,
                    rec.status,
                    rec.failure_reason,
                    rec.attempts,
                    round(rec.orig_tx_wall_ms, 3),
                    round(rec.client_rx_wall_ms, 3) if rec.client_rx_wall_ms is not None else "",
                    round(rec.ack_tx_wall_ms, 3) if rec.ack_tx_wall_ms is not None else "",
                    round(rec.server_ack_rx_wall_ms, 3) if rec.server_ack_rx_wall_ms is not None else "",
                    round(rec.delivery_latency_ms, 3) if rec.delivery_latency_ms is not None else "",
                    round(rec.rtt_ms, 3) if rec.rtt_ms is not None else "",
                    rec.duplicate_acks_ignored,
                    " -> ".join(rec.forwarding_path),
                    rec.link_hops,
                ])

    def log_experiment_row(self, row: Dict[str, Any]) -> None:
        with self.experiments_csv.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.EXPERIMENTS_HEADER, extrasaction="ignore")
            writer.writerow(row)

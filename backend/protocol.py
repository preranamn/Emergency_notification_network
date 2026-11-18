"""UDP packet protocol definitions for Emergency Notifications and ACKs."""

import json
import time
from typing import Any, Dict, Tuple


def now_timestamps() -> Tuple[float, int]:
    """Return (wall_clock_ms, monotonic_ns) pair for precise latency/RTT measurement."""
    return time.time() * 1000.0, time.monotonic_ns()


def encode_packet(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode_packet(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


def build_register_packet(client_id: str, ip: str, port: int) -> Dict[str, Any]:
    wall_ms, mono_ns = now_timestamps()
    return {
        "type": "REGISTER",
        "client_id": client_id,
        "ip": ip,
        "port": port,
        "ts_wall_ms": wall_ms,
        "ts_mono_ns": mono_ns,
    }


def build_register_ack_packet(client_id: str) -> Dict[str, Any]:
    wall_ms, mono_ns = now_timestamps()
    return {
        "type": "REGISTER_ACK",
        "client_id": client_id,
        "status": "REGISTERED",
        "server_wall_ms": wall_ms,
        "server_mono_ns": mono_ns,
    }


def build_heartbeat_packet(client_id: str, ip: str, port: int) -> Dict[str, Any]:
    wall_ms, _ = now_timestamps()
    return {
        "type": "HEARTBEAT",
        "client_id": client_id,
        "ip": ip,
        "port": port,
        "ts_wall_ms": wall_ms,
    }


def build_alert_packet(
    alert_id: str,
    target_client: str,
    category: str,
    priority: int,
    dscp: int,
    dscp_name: str,
    queue_id: int,
    message: str,
    attempt: int,
    tx_wall_ms: float,
    tx_mono_ns: int,
    sender_id: str = "H1",
    payload_bytes: int = 0,
) -> Dict[str, Any]:
    pkt: Dict[str, Any] = {
        "type": "ALERT",
        "alert_id": alert_id,
        "sender_id": sender_id,
        "target_client": target_client,
        "category": category,
        "priority": priority,
        "dscp": dscp,
        "dscp_name": dscp_name,
        "queue_id": queue_id,
        "message": message,
        "attempt": attempt,
        "tx_wall_ms": tx_wall_ms,
        "tx_mono_ns": tx_mono_ns,
    }
    if payload_bytes > 0:
        pkt["pad"] = "X" * payload_bytes
    return pkt


def build_ack_packet(
    alert_id: str,
    client_id: str,
    category: str,
    dscp: int,
    attempt: int,
    orig_tx_wall_ms: float,
    orig_tx_mono_ns: int,
    client_rx_wall_ms: float,
    client_rx_mono_ns: int,
    ack_tx_wall_ms: float,
    ack_tx_mono_ns: int,
) -> Dict[str, Any]:
    return {
        "type": "ACK",
        "alert_id": alert_id,
        "client_id": client_id,
        "category": category,
        "dscp": dscp,
        "attempt": attempt,
        "orig_tx_wall_ms": orig_tx_wall_ms,
        "orig_tx_mono_ns": orig_tx_mono_ns,
        "client_rx_wall_ms": client_rx_wall_ms,
        "client_rx_mono_ns": client_rx_mono_ns,
        "ack_tx_wall_ms": ack_tx_wall_ms,
        "ack_tx_mono_ns": ack_tx_mono_ns,
    }

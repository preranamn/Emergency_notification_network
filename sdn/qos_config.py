"""Open vSwitch HTB QoS configuration, queue statistics, and tc netem loss control."""

import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from backend.config import BOTTLENECK_LINK_RATE_BPS, DEFAULT_HOST_DELAY_MS


TRUNK_PORTS = ["s1-eth3", "s2-eth1"]
CLIENT_EGRESS_PORTS = {
    "H2": "s2-eth2",
    "H3": "s2-eth3",
    "H4": "s2-eth4",
}

QUEUE_SPEC = {
    0: {"name": "DEFAULT", "priority": 2, "min_ratio": 0.05, "max_ratio": 1.00},
    1: {"name": "MEDICAL", "priority": 0, "min_ratio": 0.40, "max_ratio": 1.00},
    2: {"name": "FIRE", "priority": 1, "min_ratio": 0.30, "max_ratio": 1.00},
    3: {"name": "GENERAL", "priority": 3, "min_ratio": 0.10, "max_ratio": 1.00},
}


def _run(cmd: List[str], check: bool = False) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def clear_ovs_qos(ports: Optional[List[str]] = None) -> None:
    """Remove OVS QoS attachments on the specified ports and purge unused QoS/Queue records."""
    target_ports = ports or TRUNK_PORTS
    for port in target_ports:
        _run(["ovs-vsctl", "--", "--if-exists", "clear", "Port", port, "qos"])
    _run(["ovs-vsctl", "--", "--all", "destroy", "QoS"])
    _run(["ovs-vsctl", "--", "--all", "destroy", "Queue"])


def configure_ovs_qos(
    link_rate_bps: int = BOTTLENECK_LINK_RATE_BPS,
    ports: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Configure OVS linux-htb QoS queues on the S1 <-> S2 bottleneck trunk ports.

    Queue mapping:
      Queue 0: Default / Control (HTB priority 2)
      Queue 1: MEDICAL (DSCP EF  = 46, HTB priority 0 - Highest)
      Queue 2: FIRE    (DSCP AF41 = 34, HTB priority 1 - Second-Highest)
      Queue 3: GENERAL & Background (DSCP BE = 0, HTB priority 3 - Normal / Best-Effort)
    """
    target_ports = ports or TRUNK_PORTS
    clear_ovs_qos(target_ports)

    rate = max(1_000_000, int(link_rate_bps))
    configured: List[str] = []
    errors: List[str] = []

    q0_min = max(100_000, int(rate * QUEUE_SPEC[0]["min_ratio"]))
    q1_min = max(200_000, int(rate * QUEUE_SPEC[1]["min_ratio"]))
    q2_min = max(200_000, int(rate * QUEUE_SPEC[2]["min_ratio"]))
    q3_min = max(100_000, int(rate * QUEUE_SPEC[3]["min_ratio"]))

    for port in target_ports:
        cmd = [
            "ovs-vsctl",
            "--", "set", "Port", port, "qos=@newqos",
            "--", "--id=@newqos", "create", "QoS", "type=linux-htb",
            f"other-config:max-rate={rate}",
            "queues=0=@q0,1=@q1,2=@q2,3=@q3",
            "--", "--id=@q0", "create", "Queue",
            f"other-config:min-rate={q0_min}",
            f"other-config:max-rate={rate}",
            f"other-config:priority={QUEUE_SPEC[0]['priority']}",
            "--", "--id=@q1", "create", "Queue",
            f"other-config:min-rate={q1_min}",
            f"other-config:max-rate={rate}",
            f"other-config:priority={QUEUE_SPEC[1]['priority']}",
            "--", "--id=@q2", "create", "Queue",
            f"other-config:min-rate={q2_min}",
            f"other-config:max-rate={rate}",
            f"other-config:priority={QUEUE_SPEC[2]['priority']}",
            "--", "--id=@q3", "create", "Queue",
            f"other-config:min-rate={q3_min}",
            f"other-config:max-rate={rate}",
            f"other-config:priority={QUEUE_SPEC[3]['priority']}",
        ]
        rc, _out, err = _run(cmd)
        if rc == 0:
            configured.append(port)
        else:
            errors.append(f"{port}: {err}")

    return {
        "ok": len(errors) == 0 and len(configured) > 0,
        "link_rate_bps": rate,
        "link_rate_mbps": round(rate / 1_000_000.0, 2),
        "configured_ports": configured,
        "errors": errors,
        "queues": {
            "1": {"category": "MEDICAL", "dscp": 46, "htb_priority": 0, "min_rate_bps": q1_min, "max_rate_bps": rate},
            "2": {"category": "FIRE", "dscp": 34, "htb_priority": 1, "min_rate_bps": q2_min, "max_rate_bps": rate},
            "3": {"category": "GENERAL", "dscp": 0, "htb_priority": 3, "min_rate_bps": q3_min, "max_rate_bps": rate},
        },
    }


def apply_egress_netem(
    client_id: str,
    delay_ms: float = DEFAULT_HOST_DELAY_MS,
    loss_percent: float = 0.0,
) -> Dict[str, Any]:
    """Apply baseline propagation delay and optional controlled packet loss on S2 client egress port.

    Because OVS HTB QoS lives on trunk ports s1-eth3 / s2-eth1, applying tc netem on
    s2-eth2/3/4 does NOT interfere with OVS QoS prioritization on S1 -> S2!
    """
    cid = client_id.upper()
    iface = CLIENT_EGRESS_PORTS.get(cid)
    if not iface:
        return {"ok": False, "error": f"Unknown client interface for {client_id}"}

    cmd = ["tc", "qdisc", "replace", "dev", iface, "root", "netem", "delay", f"{max(0.0, float(delay_ms))}ms"]
    if loss_percent > 0.0:
        cmd.extend(["loss", f"{min(100.0, max(0.0, float(loss_percent)))}%"])

    rc, _out, err = _run(cmd)
    return {
        "ok": rc == 0,
        "client_id": cid,
        "interface": iface,
        "delay_ms": delay_ms,
        "loss_percent": loss_percent,
        "error": err if rc != 0 else None,
    }


def configure_baseline_client_delays(delay_ms: float = DEFAULT_HOST_DELAY_MS) -> List[Dict[str, Any]]:
    """Apply small propagation delay to S2 client ports so baseline latency is realistic (~2-5 ms)."""
    return [apply_egress_netem(cid, delay_ms=delay_ms, loss_percent=0.0) for cid in CLIENT_EGRESS_PORTS]


def get_ovs_queue_stats(switches: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Query live OVS OpenFlow 1.3 queue counters via ovs-ofctl (complements Ryu REST stats)."""
    sw_list = switches or ["s1", "s2"]
    results: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"port\s+(\S+)\s+queue\s+(\d+):\s+bytes=(\d+),\s+pkts=(\d+),\s+errors=(\d+)"
    )

    for sw in sw_list:
        rc, out, _err = _run(["ovs-ofctl", "-O", "OpenFlow13", "queue-stats", sw])
        if rc != 0:
            continue
        for match in pattern.finditer(out):
            port_str, qid_str, bytes_str, pkts_str, errs_str = match.groups()
            qid = int(qid_str)
            results.append(
                {
                    "switch": sw.upper(),
                    "port_no": port_str,
                    "queue_id": qid,
                    "label": QUEUE_SPEC.get(qid, {}).get("name", f"QUEUE_{qid}"),
                    "tx_bytes": int(bytes_str),
                    "tx_packets": int(pkts_str),
                    "tx_errors": int(errs_str),
                }
            )
    return results

#!/usr/bin/env python3
"""Automated 6-Scenario QoS Experiment Runner & Comparison Reporter.

Executes against a running Flask NOC backend (http://127.0.0.1:5000):
  1. MEDICAL  under NORMAL conditions
  2. FIRE     under NORMAL conditions
  3. GENERAL  under NORMAL conditions
  4. MEDICAL  under CONGESTION (H5 -> H6 background traffic)
  5. FIRE     under CONGESTION
  6. GENERAL  under CONGESTION
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict


def http_json(url: str, method: str = "GET", body: Dict[str, Any] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 6-Scenario QoS Priority Experiments")
    parser.add_argument("--api", default="http://127.0.0.1:5000", help="Flask API base URL")
    parser.add_argument("--count", type=int, default=5, help="Alerts per scenario")
    parser.add_argument("--rate", type=float, default=18.0, help="Congestion rate in Mbps (H5 -> H6)")
    args = parser.parse_args()

    base = args.api.rstrip("/")
    status = http_json(f"{base}/api/status")
    if not status.get("network_active"):
        print("[ERROR] Network is not active. Activate Mininet from the UI or POST /api/network/start first.")
        sys.exit(1)

    print(f"[EXP] Mode: {status['mode']} | Clients: {len(status['server']['clients'])}/3")
    print(f"[EXP] Launching experiment suite ({args.count} alerts/scenario, congestion={args.rate} Mbps)...")

    start_resp = http_json(
        f"{base}/api/experiments/run",
        method="POST",
        body={"alerts_per_scenario": args.count, "congestion_rate_mbps": args.rate},
    )
    if not start_resp.get("ok"):
        print(f"[ERROR] Failed to start experiment: {start_resp}")
        sys.exit(1)

    # Wait for experiment completion
    while True:
        time.sleep(1.0)
        st = http_json(f"{base}/api/status")
        if not st.get("experiment_running"):
            summary = st.get("last_experiment")
            break
        print(".", end="", flush=True)

    print("\n\n=== QoS EXPERIMENT COMPARISON RESULTS ===")
    if not summary or not summary.get("results"):
        print("No experiment results returned.")
        sys.exit(1)

    header = f"{'SCENARIO':<22} {'SENT':>5} {'DELIV':>5} {'LOST':>4} {'PDR%':>7} {'AVG LAT(ms)':>12} {'AVG RTT(ms)':>12} {'Q TX PKTS':>10}"
    print(header)
    print("-" * len(header))
    for r in summary["results"]:
        lat = f"{r['avg_delivery_latency_ms']:.3f}" if r["avg_delivery_latency_ms"] is not None else "N/A"
        rtt = f"{r['avg_rtt_ms']:.3f}" if r["avg_rtt_ms"] is not None else "N/A"
        pdr = f"{r['pdr_percent']:.1f}" if r["pdr_percent"] is not None else "N/A"
        print(
            f"{r['scenario']:<22} {r['intended_deliveries']:>5} {r['packets_delivered']:>5} "
            f"{r['packets_lost']:>4} {pdr:>7} {lat:>12} {rtt:>12} {r['queue_tx_packets']:>10}"
        )
    print("\nSaved to data/experiment_results.csv")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run_experiments.py - automated experiments for the Emergency Notification Network (Member D)

For every combination of  controller mode x background load x client-link loss it:
  starts Ryu -> builds the Mininet topology -> starts server.py and 3 clients
  -> sends a warm-up alert -> starts iperf background load -> sends N alerts
  -> collects latency / delivery / retransmission numbers -> tears everything down.

Results:
  results/results.csv          one row per run (appended)
  results/raw/<run>/           server.log, ryu.log, logs/client_c*.csv for every run
  results/link_failure.txt     output of the link-failure test

Modes:
  baseline   static learning switch, no prioritisation        (EMERGENCY_MODE=0)
  emergency  priority-100 flows for UDP 5001, no queue        (EMERGENCY_MODE=1)
  queue      priority flows + OVS priority queue (--qos)      (USE_QUEUE=1)

Run from the project folder (needs root for Mininet):
  sudo python3 run_experiments.py                              # 3 modes x 3 loads, no loss
  sudo python3 run_experiments.py --losses 5 20 --loads none --modes baseline queue
  sudo python3 run_experiments.py --link-failure
  python3 plot_results.py                                      # graphs + table (no sudo)
"""

import argparse
import contextlib
import csv
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from subprocess import DEVNULL, PIPE, STDOUT
from types import SimpleNamespace

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController

from topology import EmergencyTopo, setup_ovs_queues

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
CLIENT = os.path.join(HERE, "client.py")
CONTROLLER = os.path.join(HERE, "emergency_controller.py")

# Matches server.py: ACK_TIMEOUT 3 s, 1 + 3 retries -> an alert takes at most ~12 s.
ALERT_MAX_WAIT = 16

MODES = {
    "baseline":  {"emergency": "0", "queue": "0", "qos": False},
    "emergency": {"emergency": "1", "queue": "0", "qos": False},
    "queue":     {"emergency": "1", "queue": "1", "qos": True},
}

# Background load as a multiple of the bottleneck bandwidth.
LOADS = {"none": 0.0, "moderate": 1.0, "heavy": 5.0}

FIELDS = ["timestamp", "mode", "load", "load_mbps", "loss", "alerts", "expected", "delivered",
          "delivery_pct", "mean_ms", "median_ms", "p95_ms", "max_ms",
          "alerts_retransmitted", "datagrams_resent", "first_attempt_fail_pct", "unconfirmed"]


# ---------------------------------------------------------------- helpers

def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def wait_for(cond, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def finished_alerts(log):
    """Alerts the server has finished with (all ACKed, or given up)."""
    return (len(re.findall(r"Alert \d+: all clients ACKed", log))
            + len(re.findall(r"Alert \d+: delivery NOT confirmed", log)))


def send_alert(tb, text):
    """Send one alert through the server's console and wait until it is finished."""
    tb.sent += 1
    tb.server.stdin.write(f"alert {text}\n".encode())
    tb.server.stdin.flush()
    # An alert delivered only on the very last retry prints no summary line, so
    # stop waiting after ALERT_MAX_WAIT either way; delivery is judged from client logs.
    wait_for(lambda: finished_alerts(read(tb.server_log)) >= tb.sent, ALERT_MAX_WAIT)


def stop(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def clean_ovs_qos():
    subprocess.run("ovs-vsctl --all destroy qos; ovs-vsctl --all destroy queue",
                   shell=True, stdout=DEVNULL, stderr=DEVNULL)


@contextlib.contextmanager
def testbed(args, mode_name, loss, name):
    """Ryu + Mininet + server + 3 registered clients. Everything is stopped on exit."""
    cfg = MODES[mode_name]
    run_dir = os.path.join(args.out, "raw", name)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir)

    env = dict(os.environ, EMERGENCY_MODE=cfg["emergency"], USE_QUEUE=cfg["queue"])
    ryu_log = open(os.path.join(run_dir, "ryu.log"), "w")
    ryu = subprocess.Popen([args.ryu, CONTROLLER], env=env, stdout=ryu_log, stderr=STDOUT)
    time.sleep(4)

    net = None
    procs = []
    opened = [ryu_log]
    try:
        topo = EmergencyTopo(bw=args.bw, delay=None, loss=loss, qos=cfg["qos"])
        net = Mininet(topo=topo, link=TCLink, switch=OVSKernelSwitch,
                      controller=None, autoSetMacs=True)
        net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
        net.start()
        if hasattr(net, "waitConnected"):
            net.waitConnected(timeout=15)
        if cfg["qos"]:
            link = net.linksBetween(net.get("s1"), net.get("s2"))[0]
            setup_ovs_queues([link.intf1.name, link.intf2.name], args.bw)
        time.sleep(1)

        server_log = os.path.join(run_dir, "server.log")
        sf = open(server_log, "w")
        opened.append(sf)
        server = net.get("h1").popen(["python3", "-u", SERVER], stdin=PIPE, stdout=sf,
                                     stderr=STDOUT, cwd=run_dir)
        procs.append(server)
        time.sleep(1)

        for i, host in enumerate(("h2", "h3", "h4"), start=1):
            cf = open(os.path.join(run_dir, f"client_c{i}.out"), "w")
            opened.append(cf)
            procs.append(net.get(host).popen(
                ["python3", "-u", CLIENT, f"c{i}", "--server", "10.0.0.1"],
                stdout=cf, stderr=STDOUT, cwd=run_dir))

        if not wait_for(lambda: "total clients: 3" in read(server_log), 60):
            raise RuntimeError("3 clients did not register within 60 s (see " + run_dir + ")")

        yield SimpleNamespace(net=net, server=server, server_log=server_log,
                              run_dir=run_dir, sent=0, procs=procs, ryu_log=ryu_log.name)
    finally:
        for p in reversed(procs):
            stop(p)
        if net is not None:
            try:
                net.stop()
            except Exception:
                pass
        stop(ryu)
        for f in opened:
            f.close()
        clean_ovs_qos()


# ---------------------------------------------------------------- analysis

def read_client_logs(run_dir):
    rows = []  # (seq, latency_ms)
    for path in glob.glob(os.path.join(run_dir, "logs", "client_*.csv")):
        with open(path) as f:
            for row in csv.DictReader(f):
                rows.append((int(row["seq"]), float(row["latency_ms"])))
    return rows


def percentile(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def analyse(run_dir, first_seq, n_alerts, n_clients=3):
    measured = set(range(first_seq, first_seq + n_alerts))
    server_text = read(os.path.join(run_dir, "server.log"))

    lat = [ms for seq, ms in read_client_logs(run_dir) if seq in measured]
    expected = n_alerts * n_clients

    attempts = {}  # seq -> {attempt_no: clients still waiting}
    for seq, att, waiting in re.findall(r"Alert (\d+): attempt (\d+), sent to (\d+) waiting", server_text):
        attempts.setdefault(int(seq), {})[int(att)] = int(waiting)
    unconfirmed = sum(1 for s in re.findall(r"Alert (\d+): delivery NOT confirmed", server_text)
                      if int(s) in measured)

    retrans_alerts = sum(1 for s in measured if max(attempts.get(s, {1: 0})) >= 2)
    resent = sum(w for s in measured for a, w in attempts.get(s, {}).items() if a >= 2)
    first_fail = sum(attempts.get(s, {}).get(2, 0) for s in measured)

    def r(x):
        return round(x, 2)

    return {
        "alerts": n_alerts, "expected": expected, "delivered": len(lat),
        "delivery_pct": r(100.0 * len(lat) / expected),
        "mean_ms": r(sum(lat) / len(lat)) if lat else "",
        "median_ms": r(percentile(lat, 0.5)) if lat else "",
        "p95_ms": r(percentile(lat, 0.95)) if lat else "",
        "max_ms": r(max(lat)) if lat else "",
        "alerts_retransmitted": retrans_alerts,
        "datagrams_resent": resent,
        "first_attempt_fail_pct": r(100.0 * first_fail / expected),
        "unconfirmed": unconfirmed,
    }


# ---------------------------------------------------------------- scenarios

def run_one(args, mode, load, loss):
    name = f"{mode}_{load}_loss{loss:g}"
    load_mbps = LOADS[load] * args.bw
    print(f"\n=== {name}  (load {load_mbps:g} Mbps) ===", flush=True)

    with testbed(args, mode, loss, name) as tb:
        send_alert(tb, "warm-up")              # seq 1: installs flows, not measured
        gen = sink = None
        try:
            if load_mbps > 0:
                sink = tb.net.get("h6").popen(["iperf", "-s", "-u", "-p", "5002"],
                                              stdout=DEVNULL, stderr=DEVNULL)
                time.sleep(1)
                gen = tb.net.get("h5").popen(
                    ["iperf", "-c", "10.0.0.6", "-u", "-p", "5002",
                     "-b", f"{load_mbps:g}M", "-t", "900"],
                    stdout=DEVNULL, stderr=DEVNULL)
                time.sleep(args.settle)        # let the bottleneck queue fill up
            for i in range(args.alerts):
                send_alert(tb, f"test {i + 1}")
                time.sleep(args.gap)
            time.sleep(2)
        finally:
            stop(gen)
            stop(sink)
        result = analyse(tb.run_dir, first_seq=2, n_alerts=args.alerts)

    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "mode": mode,
           "load": load, "load_mbps": load_mbps, "loss": loss}
    row.update(result)
    print(f"    delivered {row['delivered']}/{row['expected']}  median {row['median_ms']} ms  "
          f"p95 {row['p95_ms']} ms  retransmitted alerts {row['alerts_retransmitted']}  "
          f"unconfirmed {row['unconfirmed']}", flush=True)
    return row


def append_row(args, row):
    path = os.path.join(args.out, "results.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def delivered_by_seq(run_dir):
    counts = {}
    for seq, _ in read_client_logs(run_dir):
        counts[seq] = counts.get(seq, 0) + 1
    return counts


def run_link_failure(args, mode):
    print(f"\n=== link failure test ({mode}) ===", flush=True)
    with testbed(args, mode, 0.0, f"linkfail_{mode}") as tb:
        send_alert(tb, "before failure")                    # seq 1
        tb.net.configLinkStatus("s1", "s2", "down")
        time.sleep(2)
        send_alert(tb, "during failure")                    # seq 2, cannot be delivered
        tb.net.configLinkStatus("s1", "s2", "up")
        time.sleep(3)
        send_alert(tb, "after recovery")                    # seq 3
        time.sleep(2)
        got = delivered_by_seq(tb.run_dir)
        flushes = read(tb.ryu_log).count("flushing flows")

    checks = [
        ("alert before failure delivered to all 3 clients", got.get(1, 0) == 3),
        ("alert during failure not delivered", got.get(2, 0) == 0),
        ("alert after recovery delivered to all 3 clients", got.get(3, 0) == 3),
        ("controller flushed flows on port-status events", flushes >= 2),
    ]
    lines = [f"Link failure test (mode={mode})", f"deliveries per alert: {dict(sorted(got.items()))}",
             f"controller flush events: {flushes}"]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {text}" for text, ok in checks]
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(args.out, "link_failure.txt"), "w") as f:
        f.write(out + "\n")
    return all(ok for _, ok in checks)


# ---------------------------------------------------------------- summary

def summarise(rows):
    print("\n" + "=" * 100)
    print(f"{'mode':<10}{'load':<10}{'loss%':<7}{'deliv%':<8}{'median':<10}{'p95':<10}"
          f"{'max':<10}{'retx alerts':<13}{'unconfirmed'}")
    for r in rows:
        print(f"{r['mode']:<10}{r['load']:<10}{r['loss']:<7g}{r['delivery_pct']:<8}"
              f"{str(r['median_ms']):<10}{str(r['p95_ms']):<10}{str(r['max_ms']):<10}"
              f"{r['alerts_retransmitted']:<13}{r['unconfirmed']}")

    ok = True
    print("\nChecks:")
    for r in rows:
        if r["loss"] == 0 and r["delivery_pct"] < 100:
            ok = False
            print(f"[FAIL] {r['mode']}/{r['load']}: delivery {r['delivery_pct']}% with no link loss")
    for loss in sorted({r["loss"] for r in rows}):
        pick = {r["mode"]: r for r in rows if r["load"] == "heavy" and r["loss"] == loss}
        if "baseline" in pick and "queue" in pick and pick["baseline"]["p95_ms"] != "" \
                and pick["queue"]["p95_ms"] != "":
            b, q = pick["baseline"]["p95_ms"], pick["queue"]["p95_ms"]
            good = q < b
            ok = ok and good
            print(f"[{'PASS' if good else 'FAIL'}] heavy load, loss {loss:g}%: "
                  f"p95 latency queue {q} ms vs baseline {b} ms")
    if ok:
        print("[PASS] no failed checks")
    return ok


# ---------------------------------------------------------------- main

def default_ryu():
    user = os.environ.get("SUDO_USER")
    home = f"/home/{user}" if user else os.path.expanduser("~")
    return os.path.join(home, "ryu-env", "bin", "ryu-manager")


def parse_args():
    p = argparse.ArgumentParser(description="Emergency network experiment runner")
    p.add_argument("--modes", nargs="+", default=["baseline", "emergency", "queue"],
                   choices=list(MODES))
    p.add_argument("--loads", nargs="+", default=["none", "moderate", "heavy"],
                   choices=list(LOADS))
    p.add_argument("--losses", nargs="+", type=float, default=[0.0],
                   help="loss %% on client links, e.g. --losses 0 5 20")
    p.add_argument("--alerts", type=int, default=8, help="measured alerts per run")
    p.add_argument("--bw", type=float, default=2.0, help="bottleneck Mbps")
    p.add_argument("--gap", type=float, default=1.0, help="pause between alerts (s)")
    p.add_argument("--settle", type=float, default=5.0, help="seconds to let the load build up")
    p.add_argument("--ryu", default=default_ryu(), help="path to ryu-manager")
    p.add_argument("--out", default="results")
    p.add_argument("--link-failure", action="store_true",
                   help="run only the link-failure test (uses the first --modes entry)")
    return p.parse_args()


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 run_experiments.py")
    args = parse_args()
    args.out = os.path.abspath(args.out)
    if not os.path.exists(args.ryu):
        sys.exit(f"ryu-manager not found at {args.ryu} (use --ryu /path/to/ryu-manager)")
    if not shutil.which("iperf"):
        sys.exit("iperf not found: sudo apt install -y iperf")

    os.makedirs(args.out, exist_ok=True)
    setLogLevel("warning")
    subprocess.run(["mn", "-c"], stdout=DEVNULL, stderr=DEVNULL)

    rows = []
    ok = True
    try:
        if args.link_failure:
            ok = run_link_failure(args, args.modes[0])
        else:
            for loss in args.losses:
                for load in args.loads:
                    for mode in args.modes:
                        row = run_one(args, mode, load, loss)
                        append_row(args, row)
                        rows.append(row)
            ok = summarise(rows)
    except KeyboardInterrupt:
        print("\nInterrupted; partial results are in results.csv")
        ok = False
    finally:
        user = os.environ.get("SUDO_USER")
        if user:
            subprocess.run(["chown", "-R", f"{user}:", args.out],
                           stdout=DEVNULL, stderr=DEVNULL)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# ==============================================================================
# SDN-Based Priority Emergency Notification Network — Unified Launcher
# Usage:
#   sudo ./run.sh          # Full SDN mode (Ryu + Mininet + OVS QoS + NOC UI)
#   ./run.sh               # Non-root mode (Ryu + NOC UI + Loopback Test Mode)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Resolve real user's home directory even when invoked under sudo
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(eval echo "~${REAL_USER}")"

# Locate ryu-manager
RYU_BIN="${RYU_BIN:-}"
if [[ -z "$RYU_BIN" ]]; then
  for candidate in \
    "${REAL_HOME}/ryu38/bin/ryu-manager" \
    "${REAL_HOME}/ryu-env/bin/ryu-manager" \
    "$(command -v ryu-manager 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      RYU_BIN="$candidate"
      break
    fi
  done
fi

mkdir -p /tmp/emergency_notify_logs data

RYU_PID=""
cleanup() {
  echo ""
  echo "[RUN] Shutting down services..."
  if [[ -n "$RYU_PID" ]] && kill -0 "$RYU_PID" 2>/dev/null; then
    kill "$RYU_PID" 2>/dev/null || true
    wait "$RYU_PID" 2>/dev/null || true
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    ovs-vsctl -- --if-exists clear Port s1-eth3 qos 2>/dev/null || true
    ovs-vsctl -- --if-exists clear Port s2-eth1 qos 2>/dev/null || true
    ovs-vsctl -- --all destroy QoS 2>/dev/null || true
    ovs-vsctl -- --all destroy Queue 2>/dev/null || true
  fi
  echo "[RUN] Cleanup complete."
}
trap cleanup EXIT INT TERM

# Ensure Open vSwitch is running when root
if [[ "$(id -u)" -eq 0 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl start openvswitch-switch 2>/dev/null || true
  fi
  echo "[RUN] Root privileges detected — Full Mininet + OVS HTB QoS mode enabled."
else
  echo "[RUN] Running without root (sudo). Mininet activation requires 'sudo ./run.sh'."
  echo "[RUN] You can still test UDP server/clients in Loopback Test Mode from the UI."
fi

# Start Ryu SDN Controller
if [[ -n "$RYU_BIN" ]]; then
  echo "[RUN] Starting Ryu OpenFlow 1.3 Controller via: $RYU_BIN"
  "$RYU_BIN" --ofp-tcp-listen-port 6653 --wsapi-port 8088 sdn/controller.py \
    > /tmp/emergency_notify_logs/ryu_controller.log 2>&1 &
  RYU_PID=$!

  echo -n "[RUN] Waiting for Ryu REST API (http://127.0.0.1:8088/sdn/status)"
  for _ in $(seq 1 30); do
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/sdn/status', timeout=1)" >/dev/null 2>&1; then
      echo " — READY!"
      break
    fi
    echo -n "."
    sleep 0.3
  done
  echo ""
else
  echo "[WARN] ryu-manager not found! Set RYU_BIN=/path/to/ryu-manager or install Ryu."
fi

# Start Flask NOC Backend + Frontend
echo "[RUN] Starting NOC Web Server at http://127.0.0.1:5000"
echo "[RUN] Open http://localhost:5000 in your browser and click 'Activate Mininet'."
python3 backend/app.py

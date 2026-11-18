"""Central configuration for the SDN Emergency Notification Network."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALERTS_CSV = DATA_DIR / "alerts.csv"
ACKS_CSV = DATA_DIR / "acknowledgements.csv"
EXPERIMENTS_CSV = DATA_DIR / "experiment_results.csv"

# Mininet host IP & MAC layout
HOSTS = {
    "H1": {"ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "role": "Emergency Notification Server", "switch": "S1", "port": 1},
    "H2": {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "role": "Emergency Client 1", "switch": "S2", "port": 2},
    "H3": {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "role": "Emergency Client 2", "switch": "S2", "port": 3},
    "H4": {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "role": "Emergency Client 3", "switch": "S2", "port": 4},
    "H5": {"ip": "10.0.0.5", "mac": "00:00:00:00:00:05", "role": "Background Traffic Generator", "switch": "S1", "port": 2},
    "H6": {"ip": "10.0.0.6", "mac": "00:00:00:00:00:06", "role": "Background Traffic Sink", "switch": "S2", "port": 5},
}

CLIENT_IDS = ["H2", "H3", "H4"]

# UDP ports
SERVER_UDP_PORT = int(os.environ.get("EN_SERVER_UDP_PORT", 9000))
CLIENT_UDP_PORTS = {
    "H2": int(os.environ.get("EN_H2_UDP_PORT", 9102)),
    "H3": int(os.environ.get("EN_H3_UDP_PORT", 9103)),
    "H4": int(os.environ.get("EN_H4_UDP_PORT", 9104)),
}
TRAFFIC_UDP_PORT = int(os.environ.get("EN_TRAFFIC_UDP_PORT", 5001))

# SDN Controller & Web ports
RYU_OF_HOST = os.environ.get("EN_RYU_HOST", "127.0.0.1")
RYU_OF_PORT = int(os.environ.get("EN_RYU_OF_PORT", 6653))
RYU_REST_URL = os.environ.get("EN_RYU_REST_URL", "http://127.0.0.1:8088")
FLASK_HOST = os.environ.get("EN_FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("EN_FLASK_PORT", 5000))

# Cross-namespace IPC (UNIX domain socket works seamlessly across Mininet namespaces)
SERVER_IPC_SOCK = os.environ.get("EN_SERVER_IPC_SOCK", "/tmp/emergency_notify_h1.sock")
CLIENT_EVIDENCE_DIR = Path(os.environ.get("EN_EVIDENCE_DIR", "/tmp/emergency_notify_evidence"))

# Default ACK & Retransmission policy (configurable at runtime)
DEFAULT_ACK_TIMEOUT_SEC = float(os.environ.get("EN_ACK_TIMEOUT", 2.0))
DEFAULT_MAX_RETRIES = int(os.environ.get("EN_MAX_RETRIES", 2))
DEFAULT_RETRY_INTERVAL_SEC = float(os.environ.get("EN_RETRY_INTERVAL", 0.7))

# OVS QoS link capacity (bits per second) for S1 <-> S2 bottleneck trunk
BOTTLENECK_LINK_RATE_BPS = int(os.environ.get("EN_BOTTLENECK_BPS", 10_000_000))  # 10 Mbps
DEFAULT_HOST_DELAY_MS = float(os.environ.get("EN_HOST_DELAY_MS", 2.0))

# Exact topology forwarding paths from H1 to each client
FORWARDING_PATHS = {
    "H2": ["H1", "S1", "S2", "H2"],
    "H3": ["H1", "S1", "S2", "H3"],
    "H4": ["H1", "S1", "S2", "H4"],
}

REVERSE_PATHS = {
    "H2": ["H2", "S2", "S1", "H1"],
    "H3": ["H3", "S2", "S1", "H1"],
    "H4": ["H4", "S2", "S1", "H1"],
}

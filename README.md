# SDN-Based Priority Emergency Notification Network Using UDP

A complete, functional Software-Defined Networking (SDN) Emergency Notification Network built with **Python**, **UDP sockets**, **Mininet**, **Ryu SDN Controller (OpenFlow 1.3)**, **Open vSwitch (OVS) HTB QoS**, **Flask + Server-Sent Events (SSE)**, and a dark-charcoal **Network Operations Center (NOC)** web frontend with **Chart.js** visualizations.

---

## 1. Network Architecture

```text
               [ Ryu SDN Controller ]
          (OpenFlow 1.3 :6653 | REST :8088)
                  /              \
  [H1 Server] ---+                +--- [H2 Client 1] (10.0.0.2:9102)
  (10.0.0.1:9000) \              /---- [H3 Client 2] (10.0.0.3:9103)
                   [S1] ===== [S2]
  [H5 BG Gen] ----/  Ingress  Egress \-- [H4 Client 3] (10.0.0.4:9104)
  (10.0.0.5)         (OVS HTB QoS)    \-- [H6 BG Sink] (10.0.0.6:5001)
```

| Node | Role | IP / Port | Switch Attachment |
| :--- | :--- | :--- | :--- |
| **Ryu** | OpenFlow 1.3 SDN Controller | `127.0.0.1:6653` (OF) / `:8088` (REST) | Controls S1, S2 |
| **H1** | Emergency Notification Server | `10.0.0.1:9000` (UDP) | S1 Port 1 |
| **H5** | Background Traffic Generator | `10.0.0.5` (DSCP 0) | S1 Port 2 |
| **S1** | Ingress SDN Switch (`dpid=1`) | OVS OpenFlow 1.3 | Trunk `s1-eth3` ↔ `s2-eth1` |
| **S2** | Egress SDN Switch (`dpid=2`) | OVS OpenFlow 1.3 | Client fanout ports 2–5 |
| **H2** | Emergency Client 1 | `10.0.0.2:9102` (UDP) | S2 Port 2 (`s2-eth2`) |
| **H3** | Emergency Client 2 | `10.0.0.3:9103` (UDP) | S2 Port 3 (`s2-eth3`) |
| **H4** | Emergency Client 3 | `10.0.0.4:9104` (UDP) | S2 Port 4 (`s2-eth4`) |
| **H6** | Background Traffic Sink | `10.0.0.6:5001` (UDP) | S2 Port 5 (`s2-eth5`) |

---

## 2. Emergency Classification & QoS Enforcement

Outgoing UDP packets from **H1** set the real IPv4 Type-of-Service byte (`IP_TOS = DSCP << 2`) before calling `sendto()`. **Ryu** installs OpenFlow 1.3 rules matching `ip_dscp` and executing `OFPActionSetQueue(queue_id)` on **S1** and **S2**, backed by **Open vSwitch `linux-htb` QoS queues** on the `S1 ↔ S2` bottleneck trunk (`10 Mbps`):

| Category | App Priority | DSCP | IPv4 ToS | OpenFlow Priority | OVS Queue | HTB Priority | Guaranteed Min Rate |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **MEDICAL** | 1 (HIGHEST) | **EF (46)** | `0xB8` (184) | `300` | **Queue 1** | `0` (Highest) | 4.0 Mbps |
| **FIRE** | 2 (MEDIUM) | **AF41 (34)** | `0x88` (136) | `200` | **Queue 2** | `1` (Second) | 3.0 Mbps |
| **GENERAL** | 3 (NORMAL) | **BE (0)** | `0x00` (0) | `100` | **Queue 3** | `3` (Normal/BE) | 1.0 Mbps (shared w/ H5→H6) |

Because competing background UDP traffic (`H5 → H6`) uses **DSCP 0 (Best Effort)**, it shares **Queue 3** with `GENERAL` notifications. During trunk congestion (>10 Mbps), Queue 3 saturates while **Queue 1 (MEDICAL)** and **Queue 2 (FIRE)** receive strict HTB priority scheduling and guaranteed bandwidth.

---

## 3. Project Structure

```text
EMERGENCY_NOTIFY/
├── backend/
│   ├── config.py             # Network IPs, UDP ports, DSCP/QoS defaults, CSV paths
│   ├── classifier.py         # MEDICAL / FIRE / GENERAL classification & DSCP mapping
│   ├── protocol.py           # UDP JSON packet framing (REGISTER, ALERT, ACK, HEARTBEAT)
│   ├── acknowledgement.py    # Per-client ACK tracker, retries, dedup, loss-vs-ACK-loss distinction
│   ├── metrics.py            # Real latency, RTT, PDR, PLR, min/max/avg, hops, CSV persistence
│   ├── server.py             # Central UDP Server (H1) with IP_TOS DSCP tagging & cross-ns IPC
│   ├── client.py             # UDP Client (H2/H3/H4) with registration, ACK & evidence logging
│   ├── traffic_gen.py        # Background UDP congestion generator (H5) & sink (H6)
│   ├── network_manager.py    # Mininet, OVS QoS, Ryu REST stats, congestion, SSE broker
│   └── app.py                # Flask REST API + SSE live stream + frontend static server
├── sdn/
│   ├── controller.py         # Ryu OpenFlow 1.3 QoS Controller + REST stats API (:8080)
│   ├── topology.py           # Mininet topology (H1..H6, S1, S2) & host process launcher
│   └── qos_config.py         # OVS HTB QoS queue setup, queue-stats & tc netem loss control
├── frontend/
│   ├── index.html            # 4-Section NOC Interface + Live Transmission Timeline
│   ├── style.css             # Dark charcoal / black canvas NOC styling
│   └── script.js             # Live SVG packet journey animation, SSE handler, Chart.js
├── scripts/
│   └── run_experiments.py    # Automated 6-scenario QoS comparison runner
├── tests/
│   └── test_system.py        # Automated end-to-end validation suite (Tests 1–7)
├── data/
│   ├── alerts.csv
│   ├── acknowledgements.csv
│   └── experiment_results.csv
├── requirements.txt
├── run.sh
└── README.md
```

---

## 4. Installation (Ubuntu / Linux)

```bash
# 1. Install system networking dependencies
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 iperf python3-flask python3-requests curl

# 2. Ensure Open vSwitch is running
sudo systemctl enable --now openvswitch-switch

# 3. Ensure Ryu SDN Controller is installed (Ryu 4.34 requires Python 3.8/3.9 virtualenv on newer Ubuntu)
# Example if not already present at ~/ryu38:
# python3.8 -m venv ~/ryu38
# ~/ryu38/bin/pip install ryu==4.34 eventlet==0.30.2
```

---

## 5. Execution & Startup Order

### Required Startup Order
1. **Open vSwitch service** (`openvswitch-switch`)
2. **Ryu SDN Controller** (`sdn/controller.py` on OpenFlow port `6653` & REST port `8088`)
3. **Flask NOC Web Server** (`backend/app.py` on port `5000`)
4. **Mininet Topology + OVS HTB QoS** (`sdn/topology.py` — started via **"Activate Mininet"** button in the UI or manually)
5. **UDP Server on H1** (`backend/server.py`) & **Background Sink on H6**
6. **UDP Clients on H2, H3, H4** (`backend/client.py` — automatically register with H1)

### Option A: One-Command Launch (Recommended)
```bash
cd "/run/media/preranamn/New Volume/EMERGENCY_NOTIFY"
chmod +x run.sh
sudo ./run.sh
```
Then open **`http://localhost:5000`** in your browser and click **`▶ Activate Mininet`**.

### Option B: Manual Terminal-by-Terminal Launch
**Terminal 1 — Start Ryu SDN Controller:**
```bash
~/ryu38/bin/ryu-manager --ofp-tcp-listen-port 6653 --wsapi-port 8088 sdn/controller.py
```

**Terminal 2 — Start Mininet Topology, OVS QoS, H1 Server, and H2–H4 Clients:**
```bash
sudo python3 sdn/topology.py
# (Add --cli if you want the interactive Mininet CLI)
```

**Terminal 3 — Start Flask Backend & NOC Frontend:**
```bash
sudo python3 backend/app.py
```
Open **`http://localhost:5000`** in your browser.

> **Note on Non-Root Development:** Running `./run.sh` without `sudo` starts Ryu + Flask and allows testing real UDP packets, classification, ACKs, packet loss distinction, and dashboard metrics via **Loopback Test Mode** (clearly labeled as `LOOPBACK_TEST` in the UI). Full Mininet + OVS HTB QoS enforcement requires `sudo`.

---

## 6. Testing & Validation Guide

### Automated Unit & End-to-End Verification (Tests 1–7)
```bash
python3 -m unittest discover -s tests -v
```

### Automated 6-Scenario QoS Congestion Experiments (Test 5)
Once Mininet is active in the NOC UI:
- Click **`⚙ Run 6-Scenario QoS Experiment`** in the Alert Console, **or** run:
```bash
python3 scripts/run_experiments.py --count 5 --rate 18
```
Results are printed to the console and appended to `data/experiment_results.csv`, while every individual alert and ACK record is logged to `data/alerts.csv` and `data/acknowledgements.csv`.

### Controlled Packet Loss vs. ACK Loss Validation (Test 6)
In the NOC Topology footer (**Loss Test**):
1. Select **`H2`**, set **`100% Fwd`**, **`0% ACK`**, click **Apply** → packet is dropped before reaching H2 (`DELIVERY_FAILED`, increments `Packets Lost` and `PLR`).
2. Select **`H3`**, set **`0% Fwd`**, **`100% ACK`**, click **Apply** → packet is delivered to H3 (confirmed via client evidence), return ACK is dropped (`ACK_LOST`, increments `Packets Delivered` and `ACK Lost` separately from confirmed packet loss!).
3. Send an alert and inspect the **Per-Client Table** and **KPI Cards**.

### Manual OVS & OpenFlow Verification Commands (While Mininet is Running)
```bash
# Inspect OpenFlow 1.3 DSCP QoS rules & packet counters on S1 and S2
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo ovs-ofctl -O OpenFlow13 dump-flows s2

# Inspect OVS HTB Queue statistics (Queue 1 Medical, Queue 2 Fire, Queue 3 General/BE)
sudo ovs-ofctl -O OpenFlow13 queue-stats s1
sudo ovs-vsctl list QoS
sudo ovs-vsctl list Queue

# Inspect Ryu REST API live telemetry
curl http://127.0.0.1:8088/sdn/status
curl http://127.0.0.1:8088/sdn/flows
curl http://127.0.0.1:8088/sdn/queues
```

"""Mininet Topology and Lifecycle Manager for the SDN Emergency Notification Network.

Topology:
    [Ryu Controller (127.0.0.1:6653, OpenFlow 1.3)]
              /                      \\
    [H1 Server] ---+                  +--- [H2 Client 1]
    [H5 BG Gen] ---+--- [S1] ==== [S2] +--- [H3 Client 2]
                   (Ingress)  QoS  (Egress) +--- [H4 Client 3]
                                           +--- [H6 BG Sink]
"""

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import (
    BOTTLENECK_LINK_RATE_BPS,
    CLIENT_EVIDENCE_DIR,
    CLIENT_IDS,
    CLIENT_UDP_PORTS,
    DEFAULT_HOST_DELAY_MS,
    HOSTS,
    RYU_OF_HOST,
    RYU_OF_PORT,
    SERVER_IPC_SOCK,
    SERVER_UDP_PORT,
    TRAFFIC_UDP_PORT,
)
from sdn.qos_config import (
    apply_egress_netem,
    clear_ovs_qos,
    configure_baseline_client_delays,
    configure_ovs_qos,
)

try:
    from mininet.cli import CLI
    from mininet.link import Link
    from mininet.log import setLogLevel
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController
    from mininet.topo import Topo
    MININET_AVAILABLE = True
except ImportError:
    MININET_AVAILABLE = False
    Topo = object  # type: ignore


class EmergencyTopology(Topo):  # type: ignore[misc]
    """Custom Mininet topology with deterministic port assignments matching Ryu controller."""

    def build(self) -> None:
        s1 = self.addSwitch("s1", dpid="0000000000000001", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", dpid="0000000000000002", protocols="OpenFlow13")

        h1 = self.addHost("h1", ip=f"{HOSTS['H1']['ip']}/24", mac=HOSTS["H1"]["mac"])
        h2 = self.addHost("h2", ip=f"{HOSTS['H2']['ip']}/24", mac=HOSTS["H2"]["mac"])
        h3 = self.addHost("h3", ip=f"{HOSTS['H3']['ip']}/24", mac=HOSTS["H3"]["mac"])
        h4 = self.addHost("h4", ip=f"{HOSTS['H4']['ip']}/24", mac=HOSTS["H4"]["mac"])
        h5 = self.addHost("h5", ip=f"{HOSTS['H5']['ip']}/24", mac=HOSTS["H5"]["mac"])
        h6 = self.addHost("h6", ip=f"{HOSTS['H6']['ip']}/24", mac=HOSTS["H6"]["mac"])

        # Ingress switch S1 links (ports 1, 2, 3)
        self.addLink(h1, s1, port1=0, port2=1)
        self.addLink(h5, s1, port1=0, port2=2)

        # Bottleneck trunk link S1 (port 3) <-> S2 (port 1)
        self.addLink(s1, s2, port1=3, port2=1)

        # Egress switch S2 links (ports 2, 3, 4, 5)
        self.addLink(h2, s2, port1=0, port2=2)
        self.addLink(h3, s2, port1=0, port2=3)
        self.addLink(h4, s2, port1=0, port2=4)
        self.addLink(h6, s2, port1=0, port2=5)


class MininetEmergencyNetwork:
    """Manages Mininet topology, OVS QoS queues, H1 server, H2-H4 clients, and H5-H6 traffic."""

    def __init__(
        self,
        controller_ip: str = RYU_OF_HOST,
        controller_port: int = RYU_OF_PORT,
        bottleneck_bps: int = BOTTLENECK_LINK_RATE_BPS,
    ):
        self.controller_ip = controller_ip
        self.controller_port = controller_port
        self.bottleneck_bps = bottleneck_bps

        self.net: Optional[Any] = None
        self.running = False
        self.qos_info: Dict[str, Any] = {}
        self.congestion_active = False
        self.congestion_rate_mbps = 0.0
        self.packet_loss_config: Dict[str, Dict[str, float]] = {
            cid: {"forward_loss_pct": 0.0, "ack_loss_pct": 0.0} for cid in CLIENT_IDS
        }

        self._procs: Dict[str, subprocess.Popen] = {}
        self._congestion_proc: Optional[subprocess.Popen] = None
        self._log_dir = Path("/tmp/emergency_notify_logs")

    @staticmethod
    def _cleanup_stale_switches() -> None:
        """Clean up leftover s1/s2 OVS bridges and veth pairs without killing ryu-manager."""
        clear_ovs_qos()
        for sw in ("s1", "s2"):
            subprocess.run(
                ["ovs-vsctl", "--if-exists", "del-br", sw],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # Remove any stale s1-eth* / s2-eth* veth interfaces
        for sw in ("s1", "s2"):
            for port_idx in range(1, 7):
                subprocess.run(
                    ["ip", "link", "del", f"{sw}-eth{port_idx}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def start(self, launch_apps: bool = True) -> Dict[str, Any]:
        if not MININET_AVAILABLE:
            raise RuntimeError("Mininet Python library is not installed.")
        if os.geteuid() != 0:
            raise PermissionError("Mininet requires root privileges. Run with sudo.")
        if self.running:
            return {"ok": True, "already_running": True, "qos": self.qos_info}

        setLogLevel("warning")
        # Safely clean up only stale OVS bridges/veths (NEVER run `mn -c`, which kills ryu-manager!)
        self._cleanup_stale_switches()

        # Reset evidence & log directories
        if CLIENT_EVIDENCE_DIR.exists():
            shutil.rmtree(CLIENT_EVIDENCE_DIR, ignore_errors=True)
        CLIENT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CLIENT_EVIDENCE_DIR, 0o777)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        topo = EmergencyTopology()
        self.net = Mininet(
            topo=topo,
            switch=OVSSwitch,
            controller=lambda name: RemoteController(name, ip=self.controller_ip, port=self.controller_port),
            link=Link,
            autoSetMacs=False,
            autoStaticArp=True,
        )
        self.net.start()

        # Ensure OpenFlow 1.3 protocol is explicitly set on OVS bridges
        for sw in ("s1", "s2"):
            subprocess.run(
                ["ovs-vsctl", "set", "bridge", sw, "protocols=OpenFlow13"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # Wait briefly for switches to register with Ryu
        time.sleep(1.2)

        # Configure real OVS HTB QoS queues on S1 <-> S2 trunk
        self.qos_info = configure_ovs_qos(link_rate_bps=self.bottleneck_bps)
        configure_baseline_client_delays(delay_ms=DEFAULT_HOST_DELAY_MS)

        self.running = True

        if launch_apps:
            self._start_host_services()

        return {
            "ok": True,
            "mode": "MININET_SDN",
            "qos": self.qos_info,
            "hosts": list(HOSTS.keys()),
        }

    def _start_host_services(self) -> None:
        if not self.net:
            return
        py = sys.executable

        # 1. Start UDP Emergency Notification Server on H1
        h1 = self.net.get("h1")
        h1_log = open(self._log_dir / "h1_server.log", "w", encoding="utf-8")
        self._procs["H1"] = h1.popen(
            [
                py,
                str(PROJECT_ROOT / "backend" / "server.py"),
                "--bind", HOSTS["H1"]["ip"],
                "--port", str(SERVER_UDP_PORT),
                "--ipc", SERVER_IPC_SOCK,
                "--mode", "MININET_SDN",
            ],
            stdout=h1_log,
            stderr=subprocess.STDOUT,
        )

        # Wait for H1 IPC socket to appear
        for _ in range(40):
            if os.path.exists(SERVER_IPC_SOCK):
                break
            time.sleep(0.1)

        # 2. Start Background Traffic Sink on H6
        h6 = self.net.get("h6")
        h6_log = open(self._log_dir / "h6_sink.log", "w", encoding="utf-8")
        self._procs["H6"] = h6.popen(
            [
                py,
                str(PROJECT_ROOT / "backend" / "traffic_gen.py"),
                "sink",
                "--ip", HOSTS["H6"]["ip"],
                "--port", str(TRAFFIC_UDP_PORT),
            ],
            stdout=h6_log,
            stderr=subprocess.STDOUT,
        )

        # 3. Start UDP Emergency Clients on H2, H3, H4
        for cid in CLIENT_IDS:
            host = self.net.get(cid.lower())
            clog = open(self._log_dir / f"{cid.lower()}_client.log", "w", encoding="utf-8")
            self._procs[cid] = host.popen(
                [
                    py,
                    str(PROJECT_ROOT / "backend" / "client.py"),
                    "--id", cid,
                    "--server-ip", HOSTS["H1"]["ip"],
                    "--server-port", str(SERVER_UDP_PORT),
                    "--bind", HOSTS[cid]["ip"],
                    "--port", str(CLIENT_UDP_PORTS[cid]),
                ],
                stdout=clog,
                stderr=subprocess.STDOUT,
            )

        # Wait up to 4 seconds for all 3 clients (H2, H3, H4) to complete UDP REGISTER handshake
        from backend.server import ipc_call
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                resp = ipc_call({"cmd": "get_state"}, sock_path=SERVER_IPC_SOCK, timeout=0.5)
                if resp.get("ok") and len(resp["state"].get("clients", [])) >= len(CLIENT_IDS):
                    break
            except Exception:
                pass
            time.sleep(0.2)

    def start_congestion(self, rate_mbps: float = 15.0) -> Dict[str, Any]:
        if not self.running or not self.net:
            raise RuntimeError("Mininet network is not running.")
        self.stop_congestion()

        rate = max(1.0, min(100.0, float(rate_mbps)))
        h5 = self.net.get("h5")
        h5_log = open(self._log_dir / "h5_gen.log", "w", encoding="utf-8")
        self._congestion_proc = h5.popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "backend" / "traffic_gen.py"),
                "gen",
                "--ip", HOSTS["H6"]["ip"],
                "--port", str(TRAFFIC_UDP_PORT),
                "--rate", str(rate),
                "--size", "1400",
                "--dscp", "0",
            ],
            stdout=h5_log,
            stderr=subprocess.STDOUT,
        )
        self.congestion_active = True
        self.congestion_rate_mbps = rate
        return {
            "ok": True,
            "congestion_active": True,
            "rate_mbps": rate,
            "source": "H5 (10.0.0.5)",
            "destination": "H6 (10.0.0.6)",
            "dscp": 0,
        }

    def stop_congestion(self) -> Dict[str, Any]:
        if self._congestion_proc is not None:
            try:
                self._congestion_proc.terminate()
                self._congestion_proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._congestion_proc.kill()
                except Exception:
                    pass
            self._congestion_proc = None
        self.congestion_active = False
        self.congestion_rate_mbps = 0.0
        return {"ok": True, "congestion_active": False, "rate_mbps": 0.0}

    def set_packet_loss(
        self,
        client_id: str,
        forward_loss_pct: float = 0.0,
        ack_loss_pct: float = 0.0,
    ) -> Dict[str, Any]:
        """Inject controlled kernel-level tc netem packet loss on forward and/or return ACK links."""
        if not self.running or not self.net:
            raise RuntimeError("Mininet network is not running.")

        cid = client_id.upper()
        if cid not in CLIENT_IDS:
            raise ValueError(f"Unsupported client_id: {client_id}")

        fwd_loss = max(0.0, min(100.0, float(forward_loss_pct)))
        ack_loss = max(0.0, min(100.0, float(ack_loss_pct)))

        # Forward path: S2 egress interface toward client (s2-eth2/3/4)
        fwd_res = apply_egress_netem(cid, delay_ms=DEFAULT_HOST_DELAY_MS, loss_percent=fwd_loss)

        # Return (ACK) path: inside client namespace (h2-eth0 / h3-eth0 / h4-eth0)
        host = self.net.get(cid.lower())
        h_iface = f"{cid.lower()}-eth0"
        if ack_loss > 0.0:
            host.cmd(f"tc qdisc replace dev {h_iface} root netem loss {ack_loss}%")
        else:
            host.cmd(f"tc qdisc del dev {h_iface} root 2>/dev/null || true")

        self.packet_loss_config[cid] = {
            "forward_loss_pct": fwd_loss,
            "ack_loss_pct": ack_loss,
        }
        return {
            "ok": fwd_res["ok"],
            "client_id": cid,
            "forward_loss_pct": fwd_loss,
            "ack_loss_pct": ack_loss,
            "forward_interface": fwd_res.get("interface"),
            "ack_interface": h_iface,
        }

    def stop(self) -> Dict[str, Any]:
        self.stop_congestion()
        for name, proc in list(self._procs.items()):
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._procs.pop(name, None)

        if os.path.exists(SERVER_IPC_SOCK):
            try:
                os.unlink(SERVER_IPC_SOCK)
            except OSError:
                pass

        clear_ovs_qos()

        if self.net is not None:
            try:
                self.net.stop()
            except Exception:
                pass
            self.net = None

        self.running = False
        self.qos_info = {}
        for cid in CLIENT_IDS:
            self.packet_loss_config[cid] = {"forward_loss_pct": 0.0, "ack_loss_pct": 0.0}
        return {"ok": True, "running": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mininet Topology for SDN Emergency Notification Network")
    parser.add_argument("--controller-ip", default=RYU_OF_HOST, help="Ryu controller IP")
    parser.add_argument("--controller-port", type=int, default=RYU_OF_PORT, help="Ryu OpenFlow port")
    parser.add_argument("--rate-mbps", type=float, default=10.0, help="S1-S2 bottleneck link capacity in Mbps")
    parser.add_argument("--cli", action="store_true", help="Drop into Mininet interactive CLI after startup")
    parser.add_argument("--no-apps", action="store_true", help="Do not auto-launch H1 server and H2-H4 clients")
    args = parser.parse_args()

    mgr = MininetEmergencyNetwork(
        controller_ip=args.controller_ip,
        controller_port=args.controller_port,
        bottleneck_bps=int(args.rate_mbps * 1_000_000),
    )

    stop_ev = False

    def _sig(_signum, _frame):
        nonlocal stop_ev
        stop_ev = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    info = mgr.start(launch_apps=not args.no_apps)
    print(f"[TOPOLOGY] Mininet started | QoS: {info['qos']}", flush=True)
    print("[TOPOLOGY] Hosts: H1(10.0.0.1), H2(10.0.0.2), H3(10.0.0.3), H4(10.0.0.4), H5(10.0.0.5), H6(10.0.0.6)", flush=True)

    try:
        if args.cli and mgr.net is not None:
            CLI(mgr.net)
        else:
            print("[TOPOLOGY] Running in daemon mode. Press Ctrl+C to stop.", flush=True)
            while not stop_ev:
                time.sleep(0.5)
    finally:
        print("[TOPOLOGY] Stopping Mininet and cleaning OVS QoS...", flush=True)
        mgr.stop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
topology.py - Mininet topology for the Emergency Notification Network (Member C)

    h1 (10.0.0.1, server) --+                      +-- h2 (10.0.0.2, client)
                            |                      |
    h5 (10.0.0.5, bg src) --+-- s1 ==bottleneck== s2 -- h3 (10.0.0.3, client)
                                                   |
                                                   +-- h4 (10.0.0.4, client)
                                                   +-- h6 (10.0.0.6, bg sink)

Everything the server and the background source send to the clients/sink has
to cross the s1-s2 link, so that link is the congestion point.

Usage (start the Ryu controller first, in another terminal):
    ryu-manager emergency_controller.py
    sudo python3 topology.py                 # TCLink bottleneck (baseline)
    sudo python3 topology.py --qos           # OVS priority queues on the bottleneck
    sudo python3 topology.py --bw 1 --loss 5 # tighter bottleneck, 5% loss on client links

With --qos start the controller with:  USE_QUEUE=1 ryu-manager emergency_controller.py
"""

import argparse
import subprocess

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.topo import Topo


class EmergencyTopo(Topo):
    def build(self, bw=2.0, delay="5ms", loss=0.0, qos=False):
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")

        # Server and background source on the s1 side.
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h5 = self.addHost("h5", ip="10.0.0.5/24")
        self.addLink(h1, s1)
        self.addLink(h5, s1)

        # Bottleneck. In --qos mode OVS queues do the rate limiting instead
        # (tc shaping and OVS queues would fight over the same interface).
        if qos:
            self.addLink(s1, s2)
        else:
            self.addLink(s1, s2, bw=bw, delay=delay, max_queue_size=100)

        # Clients and background sink on the s2 side.
        client_opts = {"loss": loss} if loss > 0 else {}
        for name, ip in (("h2", "10.0.0.2"), ("h3", "10.0.0.3"), ("h4", "10.0.0.4")):
            host = self.addHost(name, ip=f"{ip}/24")
            self.addLink(host, s2, **client_opts)

        h6 = self.addHost("h6", ip="10.0.0.6/24")
        self.addLink(h6, s2)


def setup_ovs_queues(ifaces, bw_mbps):
    """Two HTB queues on each bottleneck port.
    Queue 1 = emergency (high priority, most of the bandwidth reserved).
    Queue 0 = everything else.
    """
    rate = int(bw_mbps * 1_000_000)
    emergency_min = int(rate * 0.8)
    other_min = int(rate * 0.1)
    for iface in ifaces:
        cmd = [
            "ovs-vsctl", "set", "port", iface, "qos=@newqos",
            "--", "--id=@newqos", "create", "qos", "type=linux-htb",
            f"other-config:max-rate={rate}",
            "queues:0=@q0", "queues:1=@q1",
            "--", "--id=@q0", "create", "queue",
            f"other-config:min-rate={other_min}", f"other-config:max-rate={rate}",
            "other-config:priority=1",
            "--", "--id=@q1", "create", "queue",
            f"other-config:min-rate={emergency_min}", f"other-config:max-rate={rate}",
            "other-config:priority=0",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            info(f"*** WARNING: could not configure queues on {iface}: {result.stderr}\n")
        else:
            info(f"*** QoS queues configured on {iface} ({bw_mbps} Mbps)\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Emergency network Mininet topology")
    parser.add_argument("--bw", type=float, default=2.0,
                        help="Bottleneck bandwidth in Mbps (default 2)")
    parser.add_argument("--delay", default="5ms", help="Bottleneck delay (TCLink mode only)")
    parser.add_argument("--loss", type=float, default=0.0,
                        help="Packet loss %% on client links (h2-h4)")
    parser.add_argument("--qos", action="store_true",
                        help="Use OVS priority queues on the bottleneck instead of a TCLink")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    return parser.parse_args()


def main():
    args = parse_args()
    setLogLevel("info")

    topo = EmergencyTopo(bw=args.bw, delay=args.delay, loss=args.loss, qos=args.qos)
    net = Mininet(
        topo=topo,
        link=TCLink,
        switch=OVSKernelSwitch,
        controller=None,
        autoSetMacs=True,
    )
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    net.start()

    if args.qos:
        link = net.linksBetween(net.get("s1"), net.get("s2"))[0]
        setup_ovs_queues([link.intf1.name, link.intf2.name], args.bw)

    info("*** Server: h1 (10.0.0.1). Clients: h2, h3, h4. Background: h5 -> h6.\n")
    info("*** Try:  pingall   |   xterm h1 h2 h3 h4 h5 h6\n")
    info("*** Flows: sudo ovs-ofctl -O OpenFlow13 dump-flows s1\n")
    info("*** Link failure test:  link s1 s2 down   /   link s1 s2 up\n")
    CLI(net)

    net.stop()


if __name__ == "__main__":
    main()

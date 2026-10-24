#!/usr/bin/env python3
"""
emergency_topo.py - Mininet star topology for the Emergency Notification Network.

    h1            (10.0.0.1)            = server
    h2 .. h(N+1)  (10.0.0.2 ...)        = clients

Run with root privileges, from the folder that contains the Python files:
    sudo python3 emergency_topo.py --clients 3
    sudo python3 emergency_topo.py --clients 3 --loss 10   # 10% loss on client links
"""

import argparse

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.topo import Topo


class EmergencyTopo(Topo):
    def build(self, clients=3, loss=0.0):
        switch = self.addSwitch("s1")

        server = self.addHost("h1", ip="10.0.0.1/24")
        self.addLink(server, switch)

        link_opts = {"loss": loss} if loss > 0 else {}
        for i in range(2, clients + 2):
            host = self.addHost(f"h{i}", ip=f"10.0.0.{i}/24")
            self.addLink(host, switch, **link_opts)


def parse_args():
    parser = argparse.ArgumentParser(description="Emergency network Mininet topology")
    parser.add_argument("--clients", type=int, default=3, help="Number of client hosts")
    parser.add_argument("--loss", type=float, default=0.0,
                        help="Packet loss percentage on client links (for retransmission tests)")
    return parser.parse_args()


def main():
    args = parse_args()
    setLogLevel("info")

    topo = EmergencyTopo(clients=args.clients, loss=args.loss)
    net = Mininet(topo=topo, link=TCLink)
    net.start()

    info(f"*** Server: h1 (10.0.0.1). Clients: h2..h{args.clients + 1}.\n")
    info("*** Open terminals with:  xterm h1 h2 h3 h4\n")
    CLI(net)

    net.stop()


if __name__ == "__main__":
    main()

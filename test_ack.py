"""
test_ack.py - single-client ACK test.

Run on one client host (e.g. h2) while the server runs on h1:
    python3 test_ack.py --server 10.0.0.1

Then type "alert <text>" on the server. This client should print the alert
and send an ACK. Do not run it on the same host as client.py (both use port 5001).
"""

import argparse
import socket
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Single-client ACK test")
    parser.add_argument("--server", default="10.0.0.1", help="Server IP")
    parser.add_argument("--port", type=int, default=5001, help="Local port to bind")
    parser.add_argument("--duration", type=float, default=60, help="Seconds to run")
    return parser.parse_args()


def main():
    args = parse_args()
    server_addr = (args.server, 5000)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)

    sock.sendto(b"REGISTER", server_addr)
    print(f"Registration sent to {args.server}:5000 from port {args.port}.")

    end_time = time.time() + args.duration
    while time.time() < end_time:
        try:
            data, address = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except ConnectionResetError:
            continue

        message = data.decode(errors="replace")
        if message.startswith("BG|"):
            continue  # background traffic, not for this test

        print("Received:", message)

        parts = message.split("|", 3)
        if len(parts) == 4 and parts[0] == "ALERT":
            ack = f"ACK|{parts[1]}|test_client"
            sock.sendto(ack.encode(), address)
            print("ACK sent:", ack)

    print("Test finished.")
    sock.close()


if __name__ == "__main__":
    main()

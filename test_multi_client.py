"""
test_multi_client.py - several simulated clients on one host.

Run on a spare client host (e.g. h5) while the server runs on h1:
    python3 test_multi_client.py --server 10.0.0.1 --clients 3

By default, the last client (client_3) receives alerts but does NOT ACK,
so the server should retransmit and then report "delivery NOT confirmed".
Use --silent-client 0 to disable that behavior.
"""

import argparse
import socket
import threading
import time


def run_client(client_id, server_ip, send_ack, duration):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("0.0.0.0", 0))  # ephemeral port, so each simulated client is unique
    client.settimeout(1.0)

    server_addr = (server_ip, 5000)
    client.sendto(b"REGISTER", server_addr)
    print(f"{client_id}: registered from port {client.getsockname()[1]}")

    end_time = time.time() + duration
    try:
        while time.time() < end_time:
            try:
                data, _ = client.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue

            message = data.decode(errors="replace")
            if not message.startswith("ALERT|"):
                continue  # ignore BG| and anything else

            print(f"{client_id} received: {message}")

            if send_ack:
                sequence = message.split("|", 2)[1]
                client.sendto(f"ACK|{sequence}|{client_id}".encode(), server_addr)
                print(f"{client_id}: ACK sent")
            else:
                print(f"{client_id}: deliberately not ACKing")
    finally:
        client.close()
        print(f"{client_id}: test finished")


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-client alert test")
    parser.add_argument("--server", default="10.0.0.1", help="Server IP")
    parser.add_argument("--clients", type=int, default=3, help="Number of simulated clients")
    parser.add_argument("--duration", type=float, default=60, help="Seconds to run")
    parser.add_argument(
        "--silent-client", type=int, default=None,
        help="Number (1-based) of a client that should NOT ACK. "
             "Default: the last client. Use 0 to make every client ACK.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    silent = args.clients if args.silent_client is None else args.silent_client

    threads = []
    for i in range(1, args.clients + 1):
        t = threading.Thread(
            target=run_client,
            args=(f"client_{i}", args.server, i != silent, args.duration),
        )
        t.start()
        threads.append(t)

    time.sleep(1)
    print(f"{args.clients} test clients are ready.")

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()

"""
client.py - Emergency Notification Network, UDP client (Member B)

Usage (inside Mininet, on a client host such as h2):
    python3 client.py <client_id> [--server 10.0.0.1] [--port 5001]

What it does:
  1. Binds a UDP port (default 5001) where alerts arrive.
  2. Sends REGISTER to the server on port 5000.
  3. Re-registers if no alert arrives for a while.
  4. For each ALERT, sends ACK back and logs latency once per sequence number.
  5. Ignores any other traffic (e.g. BG| background messages).

REGISTER and ACK are sent from the same socket that receives alerts, because
the server matches ACKs by the sender's address (IP and port).
"""

import argparse
import csv
import os
import socket
import time

SERVER_PORT = 5000     # server listens here for REGISTER and ACK
REREGISTER_AFTER = 5.0 # seconds of silence before sending REGISTER again
RECV_TIMEOUT = 1.0     # short timeout so the loop can check re-register timing


def parse_args():
    parser = argparse.ArgumentParser(description="Emergency alert client")
    parser.add_argument("client_id", help="Name for this client, e.g. c1")
    parser.add_argument(
        "--server",
        default="10.0.0.1",
        help="Server IP. 10.0.0.1 is h1 in the Mininet topology. "
             "Use 127.0.0.1 for local tests.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Port this client listens on. Only needed if several clients share one host.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    client_id = args.client_id
    server_addr = (args.server, SERVER_PORT)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(RECV_TIMEOUT)

    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", f"client_{client_id}.csv")
    log_file = open(log_path, "w", newline="")
    writer = csv.writer(log_file)
    writer.writerow(["seq", "send_ts", "recv_ts", "latency_ms"])

    seen = set()  # sequence numbers already logged
    last_alert_time = 0.0

    def register():
        sock.sendto(b"REGISTER", server_addr)
        print(f"[{client_id}] Registered with {args.server}:{SERVER_PORT}")

    print(f"[{client_id}] Listening on port {args.port}. Logging to {log_path}")
    register()
    last_register_time = time.time()

    try:
        while True:
            # Re-register if no alert has arrived for a while (first REGISTER may be lost).
            now = time.time()
            if now - last_alert_time > REREGISTER_AFTER and now - last_register_time > REREGISTER_AFTER:
                register()
                last_register_time = now

            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # ICMP "port unreachable" from an earlier send; safe to ignore.
                continue

            recv_ts = time.time()

            try:
                message = data.decode()
            except UnicodeDecodeError:
                continue

            # Only ALERT messages are for us. Ignore BG| and anything else.
            if not message.startswith("ALERT|"):
                continue

            parts = message.split("|", 3)  # ALERT, seq, send_ts, payload
            if len(parts) != 4:
                print(f"[{client_id}] Malformed alert ignored: {message!r}")
                continue

            try:
                seq = int(parts[1])
                send_ts = float(parts[2])
            except ValueError:
                print(f"[{client_id}] Malformed alert ignored: {message!r}")
                continue
            payload = parts[3]

            last_alert_time = recv_ts

            # Always ACK, even duplicates, so the server stops retransmitting.
            sock.sendto(f"ACK|{seq}|{client_id}".encode(), server_addr)

            if seq in seen:
                print(f"[{client_id}] Duplicate alert {seq} (ACKed again, not logged)")
                continue

            seen.add(seq)
            latency_ms = (recv_ts - send_ts) * 1000
            writer.writerow([seq, f"{send_ts:.6f}", f"{recv_ts:.6f}", f"{latency_ms:.3f}"])
            log_file.flush()
            print(f"[{client_id}] ALERT {seq}: {payload} (latency {latency_ms:.2f} ms)")

    except KeyboardInterrupt:
        print(f"\n[{client_id}] Stopping. {len(seen)} unique alert(s) received.")
    finally:
        log_file.close()
        sock.close()


if __name__ == "__main__":
    main()

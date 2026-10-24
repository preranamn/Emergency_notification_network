"""
server.py - Emergency Notification Network, UDP server

Run on Mininet host h1 (10.0.0.1):
    python3 server.py

Console commands:
    alert <text>   send an alert to all registered clients; retransmit until ACKed
    bg start       start background "BG|" traffic to clients
    bg stop        stop background traffic
    quit           shut down
"""

import argparse
import socket
import threading
import time

SERVER_PORT = 5000   # clients REGISTER and ACK on this port
ACK_TIMEOUT = 3      # seconds to wait for ACKs before retransmitting
MAX_RETRIES = 3      # retransmissions after the first send

lock = threading.Lock()
subscribers = set()        # (ip, port) of registered clients
pending_acks = {}          # seq -> set of client addresses that still owe an ACK
next_sequence = 1
background_stop = threading.Event()
background_thread = None


def parse_args():
    parser = argparse.ArgumentParser(description="Emergency alert server")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="IP to bind. 0.0.0.0 listens on all interfaces (needed in Mininet).",
    )
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    return parser.parse_args()


def receive_messages(server):
    while True:
        try:
            data, address = server.recvfrom(1024)
        except ConnectionResetError:
            continue
        except OSError:
            break  # socket was closed during shutdown

        try:
            message = data.decode().strip()
        except UnicodeDecodeError:
            continue

        if message == "REGISTER":
            with lock:
                subscribers.add(address)
                total = len(subscribers)
            print(f"\nClient registered: {address} (total clients: {total})")

        elif message.startswith("ACK|"):
            parts = message.split("|", 2)
            if len(parts) != 3:
                continue
            try:
                sequence = int(parts[1])
            except ValueError:
                continue
            client_id = parts[2]

            with lock:
                waiting = pending_acks.get(sequence)
                accepted = waiting is not None and address in waiting
                if accepted:
                    waiting.discard(address)

            if accepted:
                print(f"\nACK received for alert {sequence} from {client_id}")
            # Otherwise it is a duplicate or unexpected ACK; ignore it.

        else:
            print(f"\nMessage received from {address}: {message}")


def send_alert(server, payload):
    global next_sequence

    with lock:
        if not subscribers:
            print("No clients registered!")
            return
        sequence = next_sequence
        next_sequence += 1
        pending_acks[sequence] = set(subscribers)

    # The timestamp is taken once, so latency is measured from the first send.
    message = f"ALERT|{sequence}|{time.time():.6f}|{payload}".encode()
    failed = set()

    try:
        for attempt in range(MAX_RETRIES + 1):
            with lock:
                waiting = set(pending_acks.get(sequence, set()))

            if not waiting:
                print(f"Alert {sequence}: all clients ACKed.")
                break

            for client in waiting:
                server.sendto(message, client)

            print(
                f"Alert {sequence}: attempt {attempt + 1}, "
                f"sent to {len(waiting)} waiting client(s)."
            )

            # Wait for ACKs, or until the timeout expires.
            deadline = time.time() + ACK_TIMEOUT
            while time.time() < deadline:
                with lock:
                    if not pending_acks.get(sequence):
                        break
                time.sleep(0.05)
    finally:
        with lock:
            failed = pending_acks.pop(sequence, set())

    if failed:
        print(f"Alert {sequence}: delivery NOT confirmed for {sorted(failed)}")


def background_sender(server):
    counter = 0
    while not background_stop.is_set():
        with lock:
            clients = set(subscribers)

        for client in clients:
            try:
                server.sendto(f"BG|Normal traffic {counter}".encode(), client)
            except OSError:
                pass

        counter += 1
        background_stop.wait(0.5)


def command_loop(server):
    global background_thread

    while True:
        try:
            command = input("> ").strip()
        except EOFError:
            # No console attached (e.g. started with '&' in Mininet).
            # Keep serving until Ctrl+C.
            print("No console input available. Serving until Ctrl+C.")
            while True:
                time.sleep(1)

        if command.startswith("alert "):
            payload = command[len("alert "):].strip()
            if payload:
                send_alert(server, payload)
            else:
                print("Please enter alert text.")

        elif command == "bg start":
            if background_thread and background_thread.is_alive():
                print("Background traffic is already running.")
            else:
                background_stop.clear()
                background_thread = threading.Thread(
                    target=background_sender, args=(server,), daemon=True
                )
                background_thread.start()
                print("Background traffic started.")

        elif command == "bg stop":
            background_stop.set()
            print("Background traffic stopped.")

        elif command == "quit":
            break

        elif command:
            print("Unknown command. Use: alert <text> | bg start | bg stop | quit")


def main():
    args = parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((args.host, args.port))

    threading.Thread(target=receive_messages, args=(server,), daemon=True).start()
    print(f"Emergency server started! Listening on {args.host}:{args.port}")

    try:
        command_loop(server)
    except KeyboardInterrupt:
        print()
    finally:
        background_stop.set()
        server.close()
        print("Server stopped.")


if __name__ == "__main__":
    main()

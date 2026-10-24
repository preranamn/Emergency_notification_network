
import socket
import threading
import time
background_stop = threading.Event()
background_thread = None

ACK_TIMEOUT = 3
MAX_RETRIES = 3

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind(("127.0.0.1", 5000))

subscribers = set()
lock = threading.Lock()
next_sequence = 1
pending_acks = {}

print("Emergency server started!")


def receive_messages():
    while True:
        try:
            data, address = server.recvfrom(1024)
            message = data.decode().strip()

        except ConnectionResetError:
            continue

        except OSError:
            break

        if message == "REGISTER":
            with lock:
                subscribers.add(address)
                total = len(subscribers)

            print(f"\nClient registered: {address}")
            print(f"Total clients: {total}")

        elif message.startswith("ACK|"):
            parts = message.split("|", 2)
            if len(parts) == 3:
                try:
                    sequence = int(parts[1])
                except ValueError:
                    continue

                client_id = parts[2]

                if sequence in pending_acks:
                    with lock:
                        if address in pending_acks[sequence]:
                            pending_acks[sequence].remove(address)
                            print(
                                f"ACK received for alert {sequence} "
                                f"from {client_id}"
                            
                            )
                        else:
                            print("ACK already received or client not waiting.")
        else:
            print(f"\nMessage received: {message}")



def send_alert(payload):
    global next_sequence

    with lock:
        clients = set(subscribers)
        sequence = next_sequence
        next_sequence += 1

    if not clients:
        print("No clients registered!")
        return

    with lock:
        pending_acks[sequence] = set(clients)

    timestamp = time.time()
    message = f"ALERT|{sequence}|{timestamp}|{payload}".encode()

    for attempt in range(MAX_RETRIES + 1):
        with lock:
            waiting = set(pending_acks.get(sequence, set()))

        if not waiting:
            print(f"Alert {sequence}: all clients ACKed!")
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

            time.sleep(0.1)

    with lock:
        failed = set(pending_acks.pop(sequence, set()))

    if failed:
        print(f"Delivery not confirmed for: {failed}")
    


def background_sender():
    counter = 0

    while not background_stop.is_set():
        with lock:
            clients = set(subscribers)

        for client in clients:
            try:
                server.sendto(
                    f"BG|Normal traffic {counter}".encode(),
                    client
                )
            except OSError:
                return

        counter += 1
        background_stop.wait(0.5)


threading.Thread(
    target=receive_messages,
    daemon=True
).start()

while True:
    command = input("> ").strip()

    if command.startswith("alert "):
        payload = command[len("alert "):].strip()

        if payload:
            send_alert(payload)
        else:
            print("Please enter alert text.")

        
    elif command == "bg start":
        if background_thread and background_thread.is_alive():
            print("Background traffic is already running.")
        else:
            background_stop.clear()
            background_thread = threading.Thread(
                target=background_sender,
                daemon=True
            )
            background_thread.start()
            print("Background traffic started.")

    elif command == "bg stop":
        background_stop.set()
        print("Background traffic stopped.")


    elif command == "quit":
        break

    else:
        print("Unknown command.")

server.close()


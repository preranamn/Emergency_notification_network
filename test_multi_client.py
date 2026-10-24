
import socket
import threading
import time

def run_client(client_id, send_ack=True):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(20)

    server_address = ("127.0.0.1", 5000)
    client.sendto(b"REGISTER", server_address)

    print(f"{client_id}: registered on {client.getsockname()[1]}")

    try:
        while True:
            data, address = client.recvfrom(1024)
            message = data.decode()

            if message.startswith("ALERT|"):
                print(f"{client_id} received: {message}")

                if send_ack:
                    sequence = message.split("|", 2)[1]
                    ack = f"ACK|{sequence}|{client_id}"
                    client.sendto(ack.encode(), server_address)
                    print(f"{client_id}: ACK sent")

                else:
                    print(f"{client_id}: deliberately not ACKing")

    except socket.timeout:
        print(f"{client_id}: test finished")

    finally:
        client.close()


for i in range(3):
    threading.Thread(
        target=run_client,
        args=(f"client_{i + 1}", i != 2),
        daemon=True
    ).start()

time.sleep(1)
print("Three test clients are ready.")
time.sleep(60)

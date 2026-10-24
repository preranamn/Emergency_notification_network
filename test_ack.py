
import socket
import threading
import time

client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
client.bind(("127.0.0.1", 5001))
client.settimeout(60)

def receive_alert():
    try:
        data, address = client.recvfrom(1024)
        message = data.decode()
        print("Received:", message)

        parts = message.split("|", 3)

        if len(parts) == 4 and parts[0] == "ALERT":
            sequence = parts[1]
            ack = f"ACK|{sequence}|test_client"
            client.sendto(ack.encode(), address)
            print("ACK sent:", ack)

    except socket.timeout:
        print("No alert received.")

threading.Thread(target=receive_alert, daemon=True).start()

time.sleep(0.5)

client.sendto(b"REGISTER", ("127.0.0.1", 5000))
print("Registration sent.")

time.sleep(60)
client.close()

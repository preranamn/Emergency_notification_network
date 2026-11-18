"""Background UDP Congestion Generator (H5) and Sink (H6) with DSCP BE (0)."""

import argparse
import signal
import socket
import time


def run_sink(bind_ip: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    sock.settimeout(0.5)
    running = True

    def _stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    total_pkts = 0
    total_bytes = 0
    last_report = time.monotonic()
    print(f"[H6-SINK] Listening for background UDP traffic on {bind_ip}:{port}", flush=True)
    try:
        while running:
            try:
                data, _ = sock.recvfrom(65535)
                total_pkts += 1
                total_bytes += len(data)
            except socket.timeout:
                pass
            now = time.monotonic()
            if now - last_report >= 5.0 and total_pkts > 0:
                mbps = (total_bytes * 8.0) / ((now - last_report) * 1_000_000.0)
                print(f"[H6-SINK] Received {total_pkts} pkts ({mbps:.2f} Mbps)", flush=True)
                total_pkts = 0
                total_bytes = 0
                last_report = now
    finally:
        sock.close()


def run_generator(target_ip: str, port: int, rate_mbps: float, packet_size: int = 1400, dscp: int = 0) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (int(dscp) & 0x3F) << 2)
    except OSError:
        pass

    running = True

    def _stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    payload = b"B" * max(64, min(packet_size, 1450))
    bits_per_pkt = (len(payload) + 42) * 8.0  # Ethernet + IP + UDP overhead
    target_pps = max(1.0, (rate_mbps * 1_000_000.0) / bits_per_pkt)
    batch_size = max(1, min(20, int(target_pps / 200)))
    batch_interval = batch_size / target_pps

    print(
        f"[H5-GEN] Generating {rate_mbps:.1f} Mbps UDP background traffic -> {target_ip}:{port} "
        f"(pkt={len(payload)}B, DSCP={dscp}, ~{target_pps:.0f} pps)",
        flush=True,
    )

    next_send = time.monotonic()
    try:
        while running:
            for _ in range(batch_size):
                try:
                    sock.sendto(payload, (target_ip, port))
                except OSError:
                    pass
            next_send += batch_interval
            sleep_time = next_send - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -0.5:
                next_send = time.monotonic()
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Background UDP Congestion Traffic Generator / Sink")
    parser.add_argument("role", choices=["sink", "gen"], help="Run as traffic sink (H6) or generator (H5)")
    parser.add_argument("--ip", default="10.0.0.6", help="Target IP (for gen) or Bind IP (for sink)")
    parser.add_argument("--port", type=int, default=5001, help="UDP port")
    parser.add_argument("--rate", type=float, default=15.0, help="Target bandwidth in Mbps (for gen)")
    parser.add_argument("--size", type=int, default=1400, help="UDP payload size in bytes")
    parser.add_argument("--dscp", type=int, default=0, help="DSCP value (default 0 = Best Effort)")
    args = parser.parse_args()

    if args.role == "sink":
        run_sink("0.0.0.0" if args.ip == "10.0.0.6" else args.ip, args.port)
    else:
        run_generator(args.ip, args.port, args.rate, args.size, args.dscp)


if __name__ == "__main__":
    main()

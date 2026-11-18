"""End-to-End Test Suite for the SDN Priority Emergency Notification Network.

Validates:
  TEST 1: Client Registration (H2, H3, H4 via real UDP)
  TEST 2: Medical Alert (Priority 1, DSCP 46 EF, Queue 1, Latency & RTT)
  TEST 3: Fire Alert (Priority 2, DSCP 34 AF41, Queue 2)
  TEST 4: General Alert (Priority 3, DSCP 0 BE, Queue 3)
  TEST 5: Duplicate ACK Deduplication & Keyword Classification
  TEST 6: Controlled Packet Loss vs ACK Loss Distinction
  TEST 7: Multi-Client Concurrent Delivery & Aggregate Metrics Comparison
"""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.acknowledgement import (
    FAILURE_ACK_LOST,
    FAILURE_DELIVERY,
    STATUS_ACK_RECEIVED,
    STATUS_DELIVERED,
    STATUS_FAILED,
)
from backend.classifier import EmergencyClassifier
from backend.client import EmergencyNotificationClient
from backend.metrics import compute_aggregate_metrics, compute_alert_metrics
from backend.server import EmergencyNotificationServer


class EmergencyNetworkEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix="en_test_"))
        cls.evidence_dir = cls.tmp_dir / "evidence"
        cls.ipc_sock = str(cls.tmp_dir / "h1.sock")

        # Use test ports so running live services are never disturbed
        cls.server_port = 19000
        cls.client_ports = {"H2": 19102, "H3": 19103, "H4": 19104}

        cls.server = EmergencyNotificationServer(
            bind_ip="127.0.0.1",
            udp_port=cls.server_port,
            ipc_sock_path=cls.ipc_sock,
            evidence_dir=cls.evidence_dir,
            ack_timeout_sec=0.8,
            max_retries=1,
            retry_interval_sec=0.3,
            network_mode="LOOPBACK_TEST",
        )
        cls.server.start()

        cls.clients = {}
        for cid, port in cls.client_ports.items():
            c = EmergencyNotificationClient(
                client_id=cid,
                server_ip="127.0.0.1",
                server_port=cls.server_port,
                bind_ip="127.0.0.1",
                listen_port=port,
                evidence_dir=cls.evidence_dir,
                quiet=True,
            )
            c.start()
            cls.clients[cid] = c

        # Wait up to 3 seconds for all 3 clients to register
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if len(cls.server.get_clients()) == 3 and all(c.registered for c in cls.clients.values()):
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        for c in cls.clients.values():
            c.stop()
        cls.server.stop()
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def _wait_for_alert_completion(self, alert_id: str, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            alert = self.server.ack_manager.get_alert(alert_id)
            if alert and alert.completed:
                return alert
            time.sleep(0.05)
        return self.server.ack_manager.get_alert(alert_id)

    def _set_fault(self, client_id: str, drop_fwd: float = 0.0, drop_ack: float = 0.0) -> None:
        ctrl = self.evidence_dir / f"{client_id}_control.json"
        if drop_fwd == 0.0 and drop_ack == 0.0:
            if ctrl.exists():
                ctrl.unlink()
        else:
            ctrl.write_text(json.dumps({"drop_forward_prob": drop_fwd, "drop_ack_prob": drop_ack}), encoding="utf-8")

    # ------------------------------------------------------------------
    # TEST 1: Client Registration
    # ------------------------------------------------------------------
    def test_1_client_registration(self) -> None:
        registered = self.server.get_clients()
        ids = sorted(c["client_id"] for c in registered)
        self.assertEqual(ids, ["H2", "H3", "H4"])
        for cid, client in self.clients.items():
            self.assertTrue(client.registered, f"{cid} did not receive REGISTER_ACK")

    # ------------------------------------------------------------------
    # TEST 2: Medical Alert
    # ------------------------------------------------------------------
    def test_2_medical_alert(self) -> None:
        res = self.server.send_alert("MEDICAL", "Urgent medical assistance required in Coach B2.")
        alert = self._wait_for_alert_completion(res["alert"]["alert_id"])
        self.assertIsNotNone(alert)
        self.assertTrue(alert.completed)
        self.assertEqual(alert.category, "MEDICAL")
        self.assertEqual(alert.priority, 1)
        self.assertEqual(alert.dscp, 46)
        self.assertEqual(alert.queue_id, 1)

        m = compute_alert_metrics(alert)
        self.assertEqual(m["intended_deliveries"], 3)
        self.assertEqual(m["packets_delivered"], 3)
        self.assertEqual(m["packets_acked"], 3)
        self.assertEqual(m["packets_lost"], 0)
        self.assertEqual(m["pdr_percent"], 100.0)
        self.assertIsNotNone(m["avg_delivery_latency_ms"])
        self.assertIsNotNone(m["avg_rtt_ms"])
        self.assertGreaterEqual(m["avg_rtt_ms"], m["avg_delivery_latency_ms"])

        for cid in ("H2", "H3", "H4"):
            rec = alert.clients[cid]
            self.assertEqual(rec.status, STATUS_ACK_RECEIVED)
            self.assertEqual(rec.forwarding_path, ["H1", "S1", "S2", cid])
            self.assertEqual(rec.link_hops, 3)
            self.assertEqual(rec.switch_hops, 2)

    # ------------------------------------------------------------------
    # TEST 3: Fire Alert
    # ------------------------------------------------------------------
    def test_3_fire_alert(self) -> None:
        res = self.server.send_alert("FIRE", "Fire detected in Coach A3. Immediate attention required.")
        alert = self._wait_for_alert_completion(res["alert"]["alert_id"])
        self.assertTrue(alert.completed)
        self.assertEqual(alert.category, "FIRE")
        self.assertEqual(alert.priority, 2)
        self.assertEqual(alert.dscp, 34)
        self.assertEqual(alert.queue_id, 2)

        m = compute_alert_metrics(alert)
        self.assertEqual(m["packets_delivered"], 3)
        self.assertEqual(m["pdr_percent"], 100.0)

    # ------------------------------------------------------------------
    # TEST 4: General Alert
    # ------------------------------------------------------------------
    def test_4_general_alert(self) -> None:
        res = self.server.send_alert("GENERAL", "General emergency announcement. Contact railway security.")
        alert = self._wait_for_alert_completion(res["alert"]["alert_id"])
        self.assertTrue(alert.completed)
        self.assertEqual(alert.category, "GENERAL")
        self.assertEqual(alert.priority, 3)
        self.assertEqual(alert.dscp, 0)
        self.assertEqual(alert.queue_id, 3)

        m = compute_alert_metrics(alert)
        self.assertEqual(m["packets_delivered"], 3)
        self.assertEqual(m["pdr_percent"], 100.0)

    # ------------------------------------------------------------------
    # TEST 5: Auto-Classification & Duplicate ACK Deduplication
    # ------------------------------------------------------------------
    def test_5_classifier_and_deduplication(self) -> None:
        self.assertEqual(EmergencyClassifier.classify("Patient needs ambulance immediately").category, "MEDICAL")
        self.assertEqual(EmergencyClassifier.classify("Heavy smoke and flames in Coach C1").category, "FIRE")
        self.assertEqual(EmergencyClassifier.classify("Platform change announcement").category, "GENERAL")

        # Send alert and verify duplicate ACK does not corrupt delivery count
        res = self.server.send_alert("MEDICAL", "Duplicate ACK verification test")
        aid = res["alert"]["alert_id"]
        alert = self._wait_for_alert_completion(aid)
        orig_rtt = alert.clients["H2"].rtt_ms

        # Replay a duplicate ACK directly to ack_manager
        _, rec, is_dup = self.server.ack_manager.process_ack(
            alert_id=aid,
            client_id="H2",
            client_rx_wall_ms=alert.clients["H2"].client_rx_wall_ms + 50,
            client_rx_mono_ns=alert.clients["H2"].client_rx_mono_ns + 50_000_000,
            ack_tx_wall_ms=alert.clients["H2"].ack_tx_wall_ms + 50,
            ack_tx_mono_ns=alert.clients["H2"].ack_tx_mono_ns + 50_000_000,
            server_ack_rx_wall_ms=alert.clients["H2"].server_ack_rx_wall_ms + 50,
            server_ack_rx_mono_ns=alert.clients["H2"].server_ack_rx_mono_ns + 50_000_000,
        )
        self.assertTrue(is_dup)
        self.assertEqual(rec.duplicate_acks_ignored, 1)
        self.assertEqual(rec.rtt_ms, orig_rtt)

    # ------------------------------------------------------------------
    # TEST 6: Controlled Packet Loss vs ACK Loss Distinction
    # ------------------------------------------------------------------
    def test_6_packet_loss_vs_ack_loss(self) -> None:
        try:
            # H2 drops 100% of forward packets -> Confirmed DELIVERY_FAILED (Packet Loss)
            # H3 drops 100% of return ACKs -> Confirmed DELIVERED + ACK_LOST (Acknowledgement Failure, NOT packet loss!)
            # H4 operates normally -> ACK Received
            self._set_fault("H2", drop_fwd=1.0, drop_ack=0.0)
            self._set_fault("H3", drop_fwd=0.0, drop_ack=1.0)
            self._set_fault("H4", drop_fwd=0.0, drop_ack=0.0)

            res = self.server.send_alert(
                "FIRE",
                "Controlled loss & ACK failure test",
                ack_timeout_sec=0.6,
                max_retries=1,
                retry_interval_sec=0.25,
            )
            alert = self._wait_for_alert_completion(res["alert"]["alert_id"], timeout=3.0)
            self.assertTrue(alert.completed)

            # H2: Forward packet never arrived -> Failed / DELIVERY_FAILED
            self.assertEqual(alert.clients["H2"].status, STATUS_FAILED)
            self.assertEqual(alert.clients["H2"].failure_reason, FAILURE_DELIVERY)

            # H3: Forward packet DID arrive (evidence log), ACK dropped -> Delivered / ACK_LOST
            self.assertEqual(alert.clients["H3"].status, STATUS_DELIVERED)
            self.assertEqual(alert.clients["H3"].failure_reason, FAILURE_ACK_LOST)
            self.assertIsNotNone(alert.clients["H3"].delivery_latency_ms)
            self.assertIsNone(alert.clients["H3"].rtt_ms)

            # H4: Normal ACK Received
            self.assertEqual(alert.clients["H4"].status, STATUS_ACK_RECEIVED)
            self.assertIsNotNone(alert.clients["H4"].rtt_ms)

            m = compute_alert_metrics(alert)
            self.assertEqual(m["intended_deliveries"], 3)
            self.assertEqual(m["packets_delivered"], 2)  # H3 + H4 delivered!
            self.assertEqual(m["packets_acked"], 1)      # Only H4 ACKed!
            self.assertEqual(m["packets_lost"], 1)       # Only H2 lost!
            self.assertEqual(m["ack_failures"], 1)       # H3 ACK lost!
            self.assertAlmostEqual(m["pdr_percent"], 66.67, places=1)
            self.assertAlmostEqual(m["plr_percent"], 33.33, places=1)
        finally:
            for cid in ("H2", "H3", "H4"):
                self._set_fault(cid, 0.0, 0.0)

    # ------------------------------------------------------------------
    # TEST 7: Multiple Clients & Category Aggregate Comparison
    # ------------------------------------------------------------------
    def test_7_aggregate_category_comparison(self) -> None:
        agg = compute_aggregate_metrics(self.server.ack_manager.list_alerts())
        self.assertGreaterEqual(agg["total_logical_alerts"], 4)
        for cat in ("MEDICAL", "FIRE", "GENERAL"):
            self.assertGreaterEqual(agg["by_category"][cat]["alert_count"], 1)
            self.assertIsNotNone(agg["by_category"][cat]["avg_delivery_latency_ms"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

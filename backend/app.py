"""Flask API and Server-Sent Events (SSE) server for the SDN Emergency Notification Network."""

import atexit
import json
from pathlib import Path
import queue
import sys
import threading
from typing import Any, Dict

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.classifier import EmergencyClassifier
from backend.config import FLASK_HOST, FLASK_PORT
from backend.network_manager import NetworkManager

FRONTEND_DIR = PROJECT_ROOT / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
manager = NetworkManager()


def _cleanup() -> None:
    try:
        if manager._mode in ("MININET_SDN", "LOOPBACK_TEST"):
            manager.stop_network()
    except Exception:
        pass


atexit.register(_cleanup)


@app.after_request
def _add_headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ----------------------------------------------------------------------
# Frontend Static Routes
# ----------------------------------------------------------------------
@app.route("/")
def index() -> Response:
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename: str) -> Response:
    return send_from_directory(FRONTEND_DIR, filename)


# ----------------------------------------------------------------------
# Live SSE Stream
# ----------------------------------------------------------------------
@app.route("/api/events")
def sse_events() -> Response:
    sub_queue = manager.broker.subscribe()

    def event_stream():
        try:
            # Send initial state snapshot on connect
            init_payload = json.dumps({"event": "connected", "data": manager.get_full_status()})
            yield f"event: connected\ndata: {init_payload}\n\n"
            while True:
                try:
                    item = sub_queue.get(timeout=15.0)
                    ev_name = item.get("event", "message")
                    yield f"event: {ev_name}\ndata: {json.dumps(item)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            manager.broker.unsubscribe(sub_queue)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------------------------------------------------
# System & Network Lifecycle Endpoints
# ----------------------------------------------------------------------
@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(manager.get_full_status())


@app.route("/api/profiles", methods=["GET"])
def api_profiles():
    return jsonify(EmergencyClassifier.all_profiles())


@app.route("/api/sdn/stats", methods=["GET"])
def api_sdn_stats():
    return jsonify(manager.fetch_sdn_stats())


@app.route("/api/network/start", methods=["POST"])
def api_network_start():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    mode = body.get("mode", "mininet")
    bottleneck_mbps = float(body.get("bottleneck_mbps", 10.0))
    try:
        res = manager.activate_network(mode=mode, bottleneck_mbps=bottleneck_mbps)
        code = 200 if res.get("ok") else 400
        return jsonify(res), code
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/network/stop", methods=["POST"])
def api_network_stop():
    try:
        return jsonify(manager.stop_network())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ----------------------------------------------------------------------
# Emergency Alert & Policy Endpoints
# ----------------------------------------------------------------------
@app.route("/api/alert/send", methods=["POST"])
def api_alert_send():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        result = manager.send_alert(
            category=body.get("category"),
            message=body.get("message"),
            ack_timeout_sec=body.get("ack_timeout_sec"),
            max_retries=body.get("max_retries"),
            retry_interval_sec=body.get("retry_interval_sec"),
            payload_bytes=int(body.get("payload_bytes", 0)),
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/alerts/<alert_id>", methods=["GET"])
def api_get_alert(alert_id: str):
    try:
        resp = manager.call_h1({"cmd": "get_alert", "alert_id": alert_id})
        if not resp.get("ok"):
            return jsonify(resp), 404
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/policy", methods=["POST"])
def api_update_policy():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        resp = manager.call_h1(
            {
                "cmd": "update_policy",
                "ack_timeout_sec": body.get("ack_timeout_sec"),
                "max_retries": body.get("max_retries"),
                "retry_interval_sec": body.get("retry_interval_sec"),
            }
        )
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/metrics/reset", methods=["POST"])
def api_metrics_reset():
    try:
        resp = manager.call_h1({"cmd": "reset_metrics"})
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ----------------------------------------------------------------------
# Congestion & Controlled Packet Loss Endpoints
# ----------------------------------------------------------------------
@app.route("/api/congestion/start", methods=["POST"])
def api_congestion_start():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    rate_mbps = float(body.get("rate_mbps", 15.0))
    try:
        return jsonify(manager.start_congestion(rate_mbps=rate_mbps))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/congestion/stop", methods=["POST"])
def api_congestion_stop():
    try:
        return jsonify(manager.stop_congestion())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/fault/loss", methods=["POST"])
def api_fault_loss():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        res = manager.set_packet_loss(
            client_id=str(body.get("client_id", "H2")),
            forward_loss_pct=float(body.get("forward_loss_pct", 0.0)),
            ack_loss_pct=float(body.get("ack_loss_pct", 0.0)),
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ----------------------------------------------------------------------
# Automated 6-Scenario QoS Experiment Runner
# ----------------------------------------------------------------------
@app.route("/api/experiments/run", methods=["POST"])
def api_run_experiments():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    alerts_per = max(1, min(20, int(body.get("alerts_per_scenario", 5))))
    rate_mbps = float(body.get("congestion_rate_mbps", 18.0))

    if manager._experiment_running:
        return jsonify({"ok": False, "error": "Experiment suite is already running."}), 409

    def _worker():
        try:
            manager.run_qos_experiments(
                alerts_per_scenario=alerts_per,
                congestion_rate_mbps=rate_mbps,
            )
        except Exception as exc:
            manager.broker.publish("experiment_error", {"error": str(exc)})

    threading.Thread(target=_worker, name="qos-experiments", daemon=True).start()
    return jsonify({"ok": True, "started": True, "alerts_per_scenario": alerts_per, "congestion_rate_mbps": rate_mbps})


if __name__ == "__main__":
    print(f"[FLASK-API] Starting NOC Web Server on http://{FLASK_HOST}:{FLASK_PORT}", flush=True)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)

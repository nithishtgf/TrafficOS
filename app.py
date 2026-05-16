"""
app.py
------
Flask backend for the Adaptive Traffic Signal Control System.
Now supports 4 independent video sources — one per lane direction.
"""

import time
import threading
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit

from detector import MultiLaneDetector
from signal_controller import SignalController, SignalConfig
from database import DatabaseManager

# ── App Setup ──────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = "traffic_secret_key_change_in_prod"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Lane IDs ───────────────────────────────────────────────────────

LANE_IDS = ["lane_north", "lane_south", "lane_east", "lane_west"]

# ══════════════════════════════════════════════════════════════════
#  VIDEO SOURCES — set your 4 inputs here
#  Each value can be:
#    int         → webcam index (0, 1, 2, 3)
#    str (file)  → "videos/north.mp4"
#    str (rtsp)  → "rtsp://192.168.1.10/stream"
# ══════════════════════════════════════════════════════════════════

VIDEO_SOURCES = {
    "lane_north": "lanes/north.mp4",   # ← North lane camera
    "lane_south": "lanes/south.mp4",   # ← South lane camera
    "lane_east":  "lanes/east.mp4",    # ← East lane camera
    "lane_west":  "lanes/west.mp4",    # ← West lane camera
}

# TIP: For quick testing with one webcam, use:
# VIDEO_SOURCES = {
#     "lane_north": 0,
#     "lane_south": 0,
#     "lane_east":  0,
#     "lane_west":  0,
# }

# ── System Components ──────────────────────────────────────────────

detector = MultiLaneDetector(model_path="yolov8n.pt")
detector.set_sources(VIDEO_SOURCES)

controller = SignalController(lane_ids=LANE_IDS, config=SignalConfig(
    min_green=8,
    max_green=60,
    base_green=15,
    density_factor=2.0,
))
db = DatabaseManager()

# ── DB write interval ──────────────────────────────────────────────

DB_WRITE_INTERVAL = 60   # write to MySQL once per minute
_last_db_write    = 0
_latest_counts    = {}
_counts_lock      = threading.Lock()


# ── Background counts pusher ───────────────────────────────────────

def counts_push_loop():
    """
    Runs in a background thread.
    Reads counts from all 4 streams every second,
    pushes to dashboard via SocketIO, and writes to DB every minute.
    """
    global _last_db_write, _latest_counts

    while True:
        counts = detector.get_all_counts()

        controller.update_counts(counts)

        with _counts_lock:
            _latest_counts = counts

        # Push live update to browser
        # socketio.emit("counts_update", {
        #     "counts": {
        #         lid: data.get("total", 0)
        #         for lid, data in counts.items()
        #     },
        #     "signals": controller.get_all_states(),
        #     "timestamp": time.time(),
        # })
        states = controller.get_all_states()

        socketio.emit("counts_update", {
            "counts": {
                lid: states[lid]["vehicle_count"]
                for lid in states
            },
            "signals": states,
            "timestamp": time.time(),
        })

        # DB write once per minute
        now = time.time()
        if now - _last_db_write >= DB_WRITE_INTERVAL:
            db.record_counts(counts)
            _last_db_write = now

        time.sleep(1)   # push update every second

# def counts_push_loop():
#     global _last_db_write, _latest_counts

#     SAMPLE_WINDOW = 30  # seconds

#     while True:

#         start_time = time.time()
#         collected_counts = {lid: [] for lid in LANE_IDS}

#         # Collect counts for 30 seconds
#         while time.time() - start_time < SAMPLE_WINDOW:

#             counts = detector.get_all_counts()

#             for lid, data in counts.items():
#                 collected_counts[lid].append(data.get("total", 0))

#             time.sleep(1)

#         # Calculate average vehicle count
#         averaged_counts = {}

#         for lid in LANE_IDS:

#             samples = collected_counts[lid]

#             avg_count = max(samples) if samples else 0

#             averaged_counts[lid] = {
#                 "total": avg_count
#             }

#         # Update controller only once every 30 sec
#         controller.update_counts(averaged_counts)

#         with _counts_lock:
#             _latest_counts = averaged_counts

#         # Send update to frontend
#         socketio.emit("counts_update", {
#             "counts": {
#                 lid: data.get("total", 0)
#                 for lid, data in averaged_counts.items()
#             },
#             "signals": controller.get_all_states(),
#             "timestamp": time.time(),
#         })

#         # Database write
#         now = time.time()

#         if now - _last_db_write >= DB_WRITE_INTERVAL:
#             db.record_counts(averaged_counts)
#             _last_db_write = now


# Signal state change → SocketIO
def on_signal_change(states: dict):
    socketio.emit("signal_update", states)

controller.on_state_change = on_signal_change


# ── Routes: Pages ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", lanes=LANE_IDS)


# ── Routes: Video Feeds ────────────────────────────────────────────

def _mjpeg_generator(lane_id: str):
    """MJPEG generator for a single lane's video feed."""
    while True:
        jpg = detector.get_jpeg(lane_id)
        if jpg:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
        time.sleep(0.04)   # ~25 FPS cap for browser


@app.route("/video_feed/<lane_id>")
def video_feed(lane_id: str):
    """Individual lane MJPEG stream. e.g. /video_feed/lane_north"""
    if lane_id not in LANE_IDS:
        return "Invalid lane", 404
    return Response(
        _mjpeg_generator(lane_id),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/video_feed/combined")
def video_feed_combined():
    """2x2 combined overview of all 4 lanes."""
    def gen():
        while True:
            jpg = detector.get_combined_jpeg(width=640)
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
            time.sleep(0.04)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ── Routes: REST API ───────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with _counts_lock:
        counts_snapshot = dict(_latest_counts)
    return jsonify({
        "signals":     controller.get_all_states(),
        "counts":      {lid: data.get("total", 0) for lid, data in counts_snapshot.items()},
        "active_lane": controller.get_active_lane(),
        "sources":     {lid: str(src) for lid, src in VIDEO_SOURCES.items()},
        "timestamp":   time.time(),
    })


@app.route("/api/history")
def api_history():
    lane  = request.args.get("lane", None)
    limit = int(request.args.get("limit", 100))
    return jsonify(db.get_recent_counts(lane_id=lane, limit=limit))


@app.route("/api/history/hourly")
def api_hourly():
    hours = int(request.args.get("hours", 24))
    return jsonify(db.get_hourly_summary(hours=hours))


@app.route("/api/signal_cycles")
def api_signal_cycles():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_signal_cycles(limit=limit))


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats_summary())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = controller.config
        return jsonify({
            "min_green":      cfg.min_green,
            "max_green":      cfg.max_green,
            "base_green":     cfg.base_green,
            "yellow_time":    cfg.yellow_time,
            "density_factor": cfg.density_factor,
            "max_vehicles":   cfg.max_vehicles,
        })
    data = request.get_json()
    cfg  = controller.config
    cfg.min_green      = float(data.get("min_green",      cfg.min_green))
    cfg.max_green      = float(data.get("max_green",      cfg.max_green))
    cfg.base_green     = float(data.get("base_green",     cfg.base_green))
    cfg.yellow_time    = float(data.get("yellow_time",    cfg.yellow_time))
    cfg.density_factor = float(data.get("density_factor", cfg.density_factor))
    cfg.max_vehicles   = int(  data.get("max_vehicles",   cfg.max_vehicles))
    db.log_event("config_update", str(data))
    return jsonify({"status": "updated"})


@app.route("/api/sources", methods=["GET", "POST"])
def api_sources():
    """Get or update video sources per lane."""
    global VIDEO_SOURCES
    if request.method == "GET":
        return jsonify({lid: str(src) for lid, src in VIDEO_SOURCES.items()})

    data = request.get_json()
    for lane_id in LANE_IDS:
        if lane_id in data:
            src = data[lane_id]
            VIDEO_SOURCES[lane_id] = int(src) if str(src).isdigit() else src
            # Restart that lane's stream with new source
            if lane_id in detector.streams:
                detector.streams[lane_id].stop()
                from detector import LaneStream
                detector.streams[lane_id] = LaneStream(
                    lane_id=lane_id,
                    source=VIDEO_SOURCES[lane_id],
                    model=detector._model,
                    config=detector.config,
                )
                detector.streams[lane_id].start()
    db.log_event("sources_update", str(VIDEO_SOURCES))
    return jsonify({"status": "updated", "sources": {lid: str(s) for lid, s in VIDEO_SOURCES.items()}})


# ── SocketIO Events ────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print(f"[WS] Client connected: {request.sid}")
    emit("init", {
        "lanes":   LANE_IDS,
        "signals": controller.get_all_states(),
    })


@socketio.on("disconnect")
def on_disconnect():
    print(f"[WS] Client disconnected: {request.sid}")


# ── Startup / Shutdown ─────────────────────────────────────────────

def start_system():
    print("[App] Starting TrafficOS...")
    if db.connect():
        db.log_event("system_start", "TrafficOS started with 4 lane streams")
    else:
        print("[App] Warning: DB unavailable — running without persistence.")

    # Start all 4 video streams
    detector.start()

    # Start signal controller
    controller.start()

    # Start counts push loop
    t = threading.Thread(target=counts_push_loop, daemon=True, name="counts-pusher")
    t.start()

    print("[App] All systems running.")
    print(f"[App] Dashboard → http://localhost:5000")


def stop_system():
    print("[App] Shutting down...")
    detector.stop()
    controller.stop()
    db.disconnect()


# ── Entry Point ────────────────────────────────────────────────────

if __name__ == "__main__":
    start_system()
    try:
        socketio.run(app, host="0.0.0.0", port=5000, debug=False)
    finally:
        stop_system()
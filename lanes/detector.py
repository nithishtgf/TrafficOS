"""
detector.py
-----------
YOLOv8 vehicle detection — supports 4 independent lane video sources.
Each lane (North/South/East/West) has its own camera/video feed.
All 4 streams run in parallel background threads sharing one YOLO model.
"""

import cv2
import numpy as np
import time
import threading
from ultralytics import YOLO
from collections import defaultdict

# ── Vehicle classes (COCO) ─────────────────────────────────────────
VEHICLE_CLASSES = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# ── Lane colours for bounding boxes (BGR) ─────────────────────────
LANE_COLORS = {
    "lane_north": (255, 120,  50),
    "lane_south": ( 50, 230, 120),
    "lane_east":  ( 50, 130, 255),
    "lane_west":  (200,  50, 230),
}

# ── Default config ─────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "model_path":    "yolov8n.pt",
    "confidence":    0.4,
    "iou_threshold": 0.45,
    "frame_skip":    2,
}


class LaneStream:
    """
    One camera feed for one lane. Runs in its own background thread.
    Exposes latest annotated frame and vehicle count.
    """

    def __init__(self, lane_id: str, source, model: YOLO, config: dict):
        self.lane_id      = lane_id
        self.source       = source
        self.model        = model
        self.config       = config
        self.zone_polygon = None        # None = whole frame counts
        self._lock        = threading.Lock()
        self._running     = False
        self._thread      = None
        self._frame_count = 0
        self.fps_tracker  = FPSTracker()
        self.latest_frame = None
        self.latest_count = {"total": 0, "by_type": defaultdict(int)}

    def set_zone(self, polygon: list):
        self.zone_polygon = np.array(polygon, dtype=np.int32)

    def _in_zone(self, cx, cy):
        if self.zone_polygon is None:
            return True
        return cv2.pointPolygonTest(self.zone_polygon, (float(cx), float(cy)), False) >= 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"stream-{self.lane_id}")
        self._thread.start()
        print(f"[{self.lane_id}] Stream started → {self.source}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            print(f"[{self.lane_id}] ERROR: Cannot open source: {self.source}")
            self._running = False
            return

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[{self.lane_id}] {w}x{h} stream open")

        while self._running:
            ret, frame = cap.read()
            if not ret:
                if isinstance(self.source, str):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                else:
                    break

            self._frame_count += 1
            self.fps_tracker.update()

            if self._frame_count % self.config["frame_skip"] != 0:
                continue

            annotated, count = self._process_frame(frame)
            with self._lock:
                self.latest_frame = annotated
                self.latest_count = count

        cap.release()

    def _process_frame(self, frame):
        results = self.model(
            frame,
            conf=self.config["confidence"],
            iou=self.config["iou_threshold"],
            verbose=False,
            classes=list(VEHICLE_CLASSES.keys()),
        )[0]

        count = {"total": 0, "by_type": defaultdict(int)}
        annotated = frame.copy()
        color = LANE_COLORS.get(self.lane_id, (0, 255, 100))

        if self.zone_polygon is not None:
            overlay = annotated.copy()
            cv2.fillPoly(overlay, [self.zone_polygon], color)
            cv2.addWeighted(overlay, 0.15, annotated, 0.85, 0, annotated)
            cv2.polylines(annotated, [self.zone_polygon], True, color, 2)

        for box in results.boxes:
            cls_id = int(box.cls[0])
            if cls_id not in VEHICLE_CLASSES:
                continue
            class_name = VEHICLE_CLASSES[cls_id]
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if not self._in_zone(cx, cy):
                continue
            count["total"] += 1
            count["by_type"][class_name] += 1
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, f"{class_name} {conf:.2f}", (x1, y1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.circle(annotated, (cx, cy), 4, (0, 0, 255), -1)

        fps = self.fps_tracker.get_fps()
        label = f"{self.lane_id.upper()} | {count['total']} vehicles | {fps:.1f} FPS"
        cv2.rectangle(annotated, (0, 0), (len(label) * 9 + 10, 28), (0, 0, 0), -1)
        cv2.putText(annotated, label, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1)

        return annotated, count

    def get_frame(self):
        with self._lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

    def get_count(self):
        with self._lock:
            return dict(self.latest_count)

    def get_jpeg(self):
        frame = self.get_frame()
        if frame is None:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes()


class MultiLaneDetector:
    """
    Manages 4 independent LaneStream instances (one per lane direction).
    All streams share one YOLO model instance.
    """

    LANE_IDS = ["lane_north", "lane_south", "lane_east", "lane_west"]

    def __init__(self, model_path: str = "yolov8n.pt", config: dict = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        print(f"[Detector] Loading YOLO: {model_path}")
        self._model = YOLO(model_path)
        self.streams = {}

    def set_sources(self, sources: dict):
        """
        Args:
            sources: {lane_id: source}
            source = int (webcam index) or str (file path / RTSP URL)

        Example:
            {
                "lane_north": 0,
                "lane_south": "videos/south.mp4",
                "lane_east":  1,
                "lane_west":  "videos/west.mp4",
            }
        """
        for lane_id in self.LANE_IDS:
            src = sources.get(lane_id, 0)
            self.streams[lane_id] = LaneStream(
                lane_id=lane_id,
                source=src,
                model=self._model,
                config=self.config,
            )
        print(f"[Detector] Sources set: {sources}")

    def set_zone(self, lane_id: str, polygon: list):
        if lane_id in self.streams:
            self.streams[lane_id].set_zone(polygon)

    def start(self):
        if not self.streams:
            raise RuntimeError("Call set_sources() before start()")
        for stream in self.streams.values():
            stream.start()
        print("[Detector] All 4 streams running.")

    def stop(self):
        for stream in self.streams.values():
            stream.stop()

    def get_all_counts(self) -> dict:
        return {lid: s.get_count() for lid, s in self.streams.items()}

    def get_frame(self, lane_id: str):
        s = self.streams.get(lane_id)
        return s.get_frame() if s else None

    def get_jpeg(self, lane_id: str):
        s = self.streams.get(lane_id)
        return s.get_jpeg() if s else None

    def get_combined_jpeg(self, width: int = 640) -> bytes:
        """2x2 grid of all 4 lane feeds as a single JPEG."""
        h = width // 2
        blank = np.zeros((h, width // 2, 3), dtype=np.uint8)
        frames = []
        for lane_id in self.LANE_IDS:
            f = self.get_frame(lane_id)
            if f is not None:
                f = cv2.resize(f, (width // 2, h))
            else:
                f = blank.copy()
                cv2.putText(f, f"{lane_id} - no feed", (10, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)
            frames.append(f)
        top    = np.hstack([frames[0], frames[1]])
        bottom = np.hstack([frames[2], frames[3]])
        combined = np.vstack([top, bottom])
        _, buf = cv2.imencode(".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes()


class FPSTracker:
    def __init__(self, window: int = 30):
        self.window = window
        self.timestamps = []

    def update(self):
        self.timestamps.append(time.time())
        if len(self.timestamps) > self.window:
            self.timestamps.pop(0)

    def get_fps(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        elapsed = self.timestamps[-1] - self.timestamps[0]
        return (len(self.timestamps) - 1) / elapsed if elapsed > 0 else 0.0


if __name__ == "__main__":
    detector = MultiLaneDetector()
    detector.set_sources({
        "lane_north": 0,
        "lane_south": 0,
        "lane_east":  0,
        "lane_west":  0,
    })
    detector.start()
    print("Combined view — press Q to quit")
    try:
        while True:
            _, buf = cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))
            combined_bytes = detector.get_combined_jpeg(width=800)
            arr = np.frombuffer(combined_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            cv2.imshow("TrafficOS", frame)
            if cv2.waitKey(30) & 0xFF == ord('q'):
                break
            time.sleep(0.03)
    finally:
        detector.stop()
        cv2.destroyAllWindows()
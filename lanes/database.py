"""
database.py
-----------
MySQL database layer for the Adaptive Traffic Signal System.
Handles all storage and retrieval of vehicle counts, signal cycles, and alerts.
"""

import time
import threading
from datetime import datetime
from typing import Optional
import mysql.connector
from mysql.connector import pooling, Error


# ── DB Config ──────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "user":     "root",         # ← change 
    "password": "123",          # ←  MySQL password
    "database": "traffic_db",
    "pool_name": "traffic_pool",
    "pool_size": 5,
}

# ── Schema ─────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE DATABASE IF NOT EXISTS traffic_db;
USE traffic_db;

-- Stores per-lane vehicle counts snapshot (written every N seconds)
CREATE TABLE IF NOT EXISTS vehicle_counts (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    lane_id       VARCHAR(50)   NOT NULL,
    total         INT           NOT NULL DEFAULT 0,
    cars          INT           NOT NULL DEFAULT 0,
    motorcycles   INT           NOT NULL DEFAULT 0,
    buses         INT           NOT NULL DEFAULT 0,
    trucks        INT           NOT NULL DEFAULT 0,
    recorded_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_lane_time (lane_id, recorded_at)
);

-- Stores each completed signal cycle
CREATE TABLE IF NOT EXISTS signal_cycles (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    lane_id       VARCHAR(50)   NOT NULL,
    green_time    FLOAT         NOT NULL,
    vehicle_count INT           NOT NULL,
    density       FLOAT         NOT NULL DEFAULT 0,
    cycled_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_lane (lane_id),
    INDEX idx_time (cycled_at)
);

-- System events / alerts log
CREATE TABLE IF NOT EXISTS system_events (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    event_type  VARCHAR(50)   NOT NULL,
    message     TEXT,
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# ── Database Manager ───────────────────────────────────────────────

class DatabaseManager:
    """
    Thread-safe MySQL manager using a connection pool.
    Handles all reads/writes for the traffic system.
    """

    def __init__(self, config: dict = None):
        self.config  = {**DB_CONFIG, **(config or {})}
        self._pool   = None
        self._lock   = threading.Lock()
        self._buffer: list[dict] = []    # write buffer for batch inserts
        self._buffer_limit = 4           # flush when buffer hits 4 (1 snapshot × 4 lanes)
        self._flush_interval = 60        # flush every 60 seconds (1 per minute)
        self._last_flush = time.time()

    # ── Connection ─────────────────────────────────────────────────

    def connect(self) -> bool:
        """Initialize the connection pool and ensure schema exists."""
        try:
            # First connect without database to create it if needed
            bootstrap = mysql.connector.connect(
                host=self.config["host"],
                port=self.config["port"],
                user=self.config["user"],
                password=self.config["password"],
            )
            cursor = bootstrap.cursor()
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {self.config['database']}")
            bootstrap.commit()
            cursor.close()
            bootstrap.close()

            # Now create the pool with the database selected
            pool_cfg = {k: v for k, v in self.config.items()
                        if k not in ("pool_name", "pool_size")}
            self._pool = pooling.MySQLConnectionPool(
                pool_name=self.config["pool_name"],
                pool_size=self.config["pool_size"],
                **pool_cfg,
            )
            self._init_schema()
            print("[DB] Connected — pool ready.")
            return True

        except Error as e:
            print(f"[DB] Connection failed: {e}")
            return False

    def disconnect(self):
        self.flush_buffer()
        print("[DB] Disconnected.")

    def _get_conn(self):
        return self._pool.get_connection()

    def _init_schema(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            for statement in SCHEMA_SQL.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    cursor.execute(stmt)
            conn.commit()
            print("[DB] Schema ready.")
        except Error as e:
            print(f"[DB] Schema init error: {e}")
        finally:
            cursor.close()
            conn.close()

    # ── Write Operations ───────────────────────────────────────────

    def record_counts(self, counts: dict, flush: bool = False):
        """
        Buffer vehicle count snapshots for batch writing.

        Args:
            counts: {lane_id: {"total": int, "by_type": {"car": n, ...}}}
            flush:  if True, write immediately
        """
        ts = datetime.now()
        for lane_id, data in counts.items():
            by_type = data.get("by_type", {})
            self._buffer.append({
                "lane_id":     lane_id,
                "total":       data.get("total", 0),
                "cars":        by_type.get("car", 0),
                "motorcycles": by_type.get("motorcycle", 0),
                "buses":       by_type.get("bus", 0),
                "trucks":      by_type.get("truck", 0),
                "recorded_at": ts,
            })

        should_flush = (
            flush
            or len(self._buffer) >= self._buffer_limit
            or (time.time() - self._last_flush) >= self._flush_interval
        )
        if should_flush:
            self.flush_buffer()

    def flush_buffer(self):
        """Write buffered records to MySQL in a single batch."""
        with self._lock:
            if not self._buffer or not self._pool:
                return
            rows = self._buffer.copy()
            self._buffer.clear()
            self._last_flush = time.time()

        sql = """
            INSERT INTO vehicle_counts
              (lane_id, total, cars, motorcycles, buses, trucks, recorded_at)
            VALUES
              (%(lane_id)s, %(total)s, %(cars)s, %(motorcycles)s,
               %(buses)s, %(trucks)s, %(recorded_at)s)
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.executemany(sql, rows)
            conn.commit()
        except Error as e:
            print(f"[DB] Flush error: {e}")
        finally:
            cursor.close()
            conn.close()

    def record_signal_cycle(self, lane_id: str, green_time: float,
                             vehicle_count: int, density: float = 0.0):
        """Log a completed signal cycle."""
        if not self._pool:
            return
        sql = """
            INSERT INTO signal_cycles (lane_id, green_time, vehicle_count, density)
            VALUES (%s, %s, %s, %s)
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, (lane_id, round(green_time, 2),
                                  vehicle_count, round(density, 3)))
            conn.commit()
        except Error as e:
            print(f"[DB] signal_cycles insert error: {e}")
        finally:
            cursor.close()
            conn.close()

    def log_event(self, event_type: str, message: str = ""):
        """Write a system event/alert."""
        if not self._pool:
            return
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO system_events (event_type, message) VALUES (%s, %s)",
                (event_type, message)
            )
            conn.commit()
        except Error as e:
            print(f"[DB] event log error: {e}")
        finally:
            cursor.close()
            conn.close()

    # ── Read Operations ────────────────────────────────────────────

    def get_recent_counts(self, lane_id: Optional[str] = None,
                           limit: int = 100) -> list[dict]:
        """
        Fetch recent vehicle count records.

        Args:
            lane_id: filter by lane (None = all lanes)
            limit:   max rows to return
        """
        if not self._pool:
            return []
        conn = self._get_conn()
        try:
            cursor = conn.cursor(dictionary=True)
            if lane_id:
                cursor.execute(
                    "SELECT * FROM vehicle_counts WHERE lane_id = %s "
                    "ORDER BY recorded_at DESC LIMIT %s",
                    (lane_id, limit)
                )
            else:
                cursor.execute(
                    "SELECT * FROM vehicle_counts "
                    "ORDER BY recorded_at DESC LIMIT %s", (limit,)
                )
            return cursor.fetchall()
        except Error as e:
            print(f"[DB] get_recent_counts error: {e}")
            return []
        finally:
            cursor.close()
            conn.close()

    def get_hourly_summary(self, hours: int = 24) -> list[dict]:
        """Aggregate vehicle counts grouped by lane and hour."""
        if not self._pool:
            return []
        conn = self._get_conn()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT
                    lane_id,
                    DATE_FORMAT(recorded_at, '%%Y-%%m-%%d %%H:00') AS hour,
                    AVG(total)  AS avg_vehicles,
                    MAX(total)  AS peak_vehicles,
                    COUNT(*)    AS snapshots
                FROM vehicle_counts
                WHERE recorded_at >= NOW() - INTERVAL %s HOUR
                GROUP BY lane_id, hour
                ORDER BY hour DESC
            """, (hours,))
            return cursor.fetchall()
        except Error as e:
            print(f"[DB] get_hourly_summary error: {e}")
            return []
        finally:
            cursor.close()
            conn.close()

    def get_signal_cycles(self, limit: int = 50) -> list[dict]:
        """Fetch recent signal cycle history."""
        if not self._pool:
            return []
        conn = self._get_conn()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT * FROM signal_cycles ORDER BY cycled_at DESC LIMIT %s",
                (limit,)
            )
            return cursor.fetchall()
        except Error as e:
            print(f"[DB] get_signal_cycles error: {e}")
            return []
        finally:
            cursor.close()
            conn.close()

    def get_stats_summary(self) -> dict:
        """High-level stats for the dashboard summary cards."""
        if not self._pool:
            return {}
        conn = self._get_conn()
        try:
            cursor = conn.cursor(dictionary=True)

            cursor.execute("""
                SELECT
                    SUM(total)  AS total_vehicles_today,
                    AVG(total)  AS avg_per_snapshot,
                    MAX(total)  AS peak_vehicles
                FROM vehicle_counts
                WHERE recorded_at >= CURDATE()
            """)
            row = cursor.fetchone() or {}

            cursor.execute("""
                SELECT AVG(green_time) AS avg_green_time
                FROM signal_cycles
                WHERE cycled_at >= CURDATE()
            """)
            green = cursor.fetchone() or {}

            return {
                "total_vehicles_today": int(row.get("total_vehicles_today") or 0),
                "avg_per_snapshot":     round(float(row.get("avg_per_snapshot") or 0), 1),
                "peak_vehicles":        int(row.get("peak_vehicles") or 0),
                "avg_green_time":       round(float(green.get("avg_green_time") or 0), 1),
            }
        except Error as e:
            print(f"[DB] get_stats_summary error: {e}")
            return {}
        finally:
            cursor.close()
            conn.close()
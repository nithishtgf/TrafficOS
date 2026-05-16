"""
signal_controller.py
--------------------
Adaptive traffic signal controller.
Dynamically allocates green time to lanes based on real-time vehicle density.
"""

import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


# ── Signal States ──────────────────────────────────────────────────

class SignalState(str, Enum):
    GREEN  = "green"
    YELLOW = "yellow"
    RED    = "red"


# ── Configuration ──────────────────────────────────────────────────

@dataclass
class SignalConfig:
    min_green:      float = 10.0   # minimum green time (seconds)
    max_green:      float = 60.0   # maximum green time (seconds)
    yellow_time:    float = 4.0    # fixed yellow phase duration
    base_green:     float = 10.0   # default green when no vehicles detected
    density_factor: float = 1.0    # seconds of green per vehicle
    max_vehicles:   int   = 30     # vehicle count considered "fully congested"


# ── Lane Signal ────────────────────────────────────────────────────

@dataclass
class LaneSignal:
    lane_id:       str
    state:         SignalState = SignalState.RED
    remaining:     float       = 0.0          # seconds left in current phase
    vehicle_count: int         = 0
    locked_count: int = 0
    green_time:    float       = 0.0          # allocated green time
    total_cycles:  int         = 0
    density:       float       = 0.0          # 0.0 → 1.0

    def to_dict(self) -> dict:
        return {
            "lane_id":       self.lane_id,
            "state":         self.state.value,
            "remaining":     round(self.remaining, 1),
            # "vehicle_count": self.vehicle_count,
            "vehicle_count": self.locked_count,
            "green_time":    round(self.green_time, 1),
            "total_cycles":  self.total_cycles,
            "density":       round(self.density, 2),
        }


# ── Adaptive Signal Controller ─────────────────────────────────────

class SignalController:
    """
    Round-robin adaptive signal controller.

    Algorithm:
      1. Collect vehicle counts from detector for all lanes.
      2. Calculate each lane's density (count / max_vehicles, capped at 1.0).
      3. Allocate green time = base_green + density * density_factor * max_green.
      4. Serve lanes in round-robin order, one green at a time.
      5. Yellow phase separates each green→red transition.
    """

    def __init__(self, lane_ids: list[str], config: SignalConfig = None):
        self.config   = config or SignalConfig()
        self.lanes    = {lid: LaneSignal(lane_id=lid) for lid in lane_ids}
        self.order    = list(lane_ids)          # rotation order
        self.current_index = 0                  # which lane is currently active
        self._lock    = threading.Lock()
        self._running = False
        self._thread  = None
        self.on_state_change: Callable | None = None  # hook for Flask/SocketIO
        self.cycle_log: list[dict] = []         # recent cycle history

        print(f"[Signal] Controller initialized for lanes: {lane_ids}")

    # ── Public API ─────────────────────────────────────────────────

    def update_counts(self, counts: dict):
        """
        Called by detector callback with latest vehicle counts.

        Args:
            counts: {lane_id: {"total": int, "by_type": {...}}}
        """
        with self._lock:
            for lane_id, data in counts.items():
                if lane_id in self.lanes:
                    n = data.get("total", 0)
                    # self.lanes[lane_id].vehicle_count = n
                    # self.lanes[lane_id].density = min(n / self.config.max_vehicles, 1.0)
                    # self.lanes[lane_id].green_time = self._calc_green_time(n)
                    lane = self.lanes[lane_id]

                    # ONLY update if lane is NOT currently active
                    if lane.state != SignalState.GREEN:
                        lane.vehicle_count = n
                        lane.density = min(n / self.config.max_vehicles, 1.0)
                        lane.green_time = self._calc_green_time(n)

    def get_all_states(self) -> dict:
        """Return current state of all lane signals."""
        with self._lock:
            return {lid: lane.to_dict() for lid, lane in self.lanes.items()}

    def get_active_lane(self) -> str:
        """Return the lane_id currently holding GREEN."""
        return self.order[self.current_index]

    def start(self):
        """Start the signal cycle loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_cycle, daemon=True)
        self._thread.start()
        print("[Signal] Controller started.")

    def stop(self):
        """Stop the signal cycle loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[Signal] Controller stopped.")

    # ── Core Cycle Logic ───────────────────────────────────────────

    def _calc_green_time(self, vehicle_count: int) -> float:
        """Calculate green time based on vehicle count."""
        cfg = self.config
        if vehicle_count == 0:
            return cfg.base_green
        density = min(vehicle_count / cfg.max_vehicles, 1.0)
        raw = cfg.base_green + (vehicle_count * cfg.density_factor)
        return round(max(cfg.min_green, min(raw, cfg.max_green)), 1)

    def _set_state(self, lane_id: str, state: SignalState, remaining: float):
        """Update a lane's signal state (thread-safe)."""
        with self._lock:
            self.lanes[lane_id].state     = state
            self.lanes[lane_id].remaining = remaining
            if state == SignalState.GREEN:
                self.lanes[lane_id].total_cycles += 1
                self.lanes[lane_id].locked_count = self.lanes[lane_id].vehicle_count

        if self.on_state_change:
            self.on_state_change(self.get_all_states())

    def _set_all_red(self):
        for lid in self.lanes:
            self._set_state(lid, SignalState.RED, 0.0)

    def _run_cycle(self):
        """
        Main signal loop. Runs indefinitely until stop() is called.
        Each iteration: GREEN → countdown → YELLOW → RED → next lane.
        """
        self._set_all_red()

        while self._running:
            lane_id = self.order[self.current_index]

            with self._lock:
                green_duration = self.lanes[lane_id].green_time or self.config.base_green
                vehicle_count  = self.lanes[lane_id].vehicle_count
                self.lanes[lane_id].locked_count = vehicle_count

            # ── GREEN phase ──────────────────────────────────────
            self._set_state(lane_id, SignalState.GREEN, green_duration)
            print(f"[Signal] 🟢 GREEN → {lane_id} "
                  f"({green_duration:.1f}s | {vehicle_count} vehicles)")

            start = time.time()
            while self._running:
                elapsed   = time.time() - start
                remaining = green_duration - elapsed
                if remaining <= 0:
                    break
                with self._lock:
                    self.lanes[lane_id].remaining = remaining
                    # # Re-check if count changed during green
                    # new_green = self._calc_green_time(self.lanes[lane_id].vehicle_count)
                    # # Only extend if significantly more congested (avoid thrashing)
                    # if new_green > green_duration + 5:
                    #     green_duration = min(new_green, self.config.max_green)
                time.sleep(0.5)

            # ── YELLOW phase ─────────────────────────────────────
            self._set_state(lane_id, SignalState.YELLOW, self.config.yellow_time)
            print(f"[Signal] 🟡 YELLOW → {lane_id}")

            yellow_start = time.time()
            while self._running:
                remaining = self.config.yellow_time - (time.time() - yellow_start)
                if remaining <= 0:
                    break
                with self._lock:
                    self.lanes[lane_id].remaining = max(remaining, 0)
                time.sleep(0.2)

            # ── RED + log ─────────────────────────────────────────
            self._set_state(lane_id, SignalState.RED, 0.0)
            self._log_cycle(lane_id, green_duration, vehicle_count)

            # Advance to next lane
            self.current_index = (self.current_index + 1) % len(self.order)
    
    

    def _log_cycle(self, lane_id: str, green_time: float, vehicle_count: int):
        """Keep a rolling log of the last 100 cycles."""
        entry = {
            "timestamp":     time.time(),
            "lane_id":       lane_id,
            "green_time":    green_time,
            "vehicle_count": vehicle_count,
        }
        self.cycle_log.append(entry)
        if len(self.cycle_log) > 100:
            self.cycle_log.pop(0)


# ── Quick standalone test ──────────────────────────────────────────

if __name__ == "__main__":
    controller = SignalController(
        lane_ids=["lane_north", "lane_south", "lane_east", "lane_west"]
    )

    # Simulate some vehicle counts
    controller.update_counts({
        "lane_north": {"total": 12},
        "lane_south": {"total": 3},
        "lane_east":  {"total": 25},
        "lane_west":  {"total": 0},
    })

    controller.start()

    try:
        while True:
            states = controller.get_all_states()
            for lid, s in states.items():
                print(f"  {lid}: {s['state']:6s} | {s['remaining']:5.1f}s "
                      f"| vehicles: {s['vehicle_count']}")
            print("─" * 50)
            time.sleep(2)
    except KeyboardInterrupt:
        controller.stop()
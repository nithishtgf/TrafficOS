# TrafficOS — AI-Based Adaptive Traffic Signal System

## What This Project Does

This system watches live CCTV/webcam footage, detects vehicles using YOLOv8,
counts them per lane (North/South/East/West), and automatically adjusts
traffic light green times based on how congested each lane is.
A live web dashboard shows everything in real time.

---

## How It Actually Works (Step by Step)

```
Webcam/Video
     │
     ▼
OpenCV grabs raw frame (30 FPS)
     │
     ▼  (every 2nd frame only — frame_skip=2)
YOLOv8 runs detection
     │  ~15 detections/second
     ▼
Each vehicle's center point is checked
against 4 zone polygons (N/S/E/W)
     │
     ▼
Vehicle counts updated in memory instantly
     │
     ├──► Every frame    → SocketIO pushes counts to browser (live)
     ├──► Every 5 sec    → MySQL write (12 rows/min per lane)
     └──► Continuously   → SignalController reads counts,
                           calculates green time per lane,
                           runs round-robin cycle in background thread
```

### Green Time Formula

```
density      = lane_vehicle_count / max_vehicles   (capped at 1.0)
green_time   = base_green + density × density_factor × max_green

Example:
  lane_east has 21 vehicles, max_vehicles=30
  density      = 21/30 = 0.70
  green_time   = 15 + 0.70 × 2.0 × 60 = 15 + 84 = 99 → capped at 60s

  lane_west has 0 vehicles
  green_time   = base_green = 15s (minimum)
```

### Database Write Rate

| What            | How often     | Rows per minute |
|-----------------|---------------|-----------------|
| YOLOv8 detects  | ~15/sec       | in-memory only  |
| MySQL write      | every 5 sec   | ~12 rows/min    |
| Signal cycle log | per cycle     | ~4–8 rows/min   |

---

## Project Structure

```
traffic_system/
├── app.py                  ← Flask app, routes, SocketIO, startup
├── detector.py             ← YOLOv8 + OpenCV detection engine
├── signal_controller.py    ← Adaptive green time logic (background thread)
├── database.py             ← MySQL connection pool + queries
├── schema.sql              ← Run once to create the database
├── requirements.txt        ← Python dependencies
│
├── templates/
│   └── index.html          ← Dashboard HTML (served by Flask)
│
├── static/
│   ├── css/style.css       ← Dark industrial theme
│   └── js/dashboard.js     ← Live updates via SocketIO + Chart.js
│
├── videos/                 ← Drop .mp4 test videos here
│   └── sample.mp4
│
└── models/                 ← YOLOv8 weights (auto-downloaded)
    └── yolov8n.pt
```

---

## Environment Setup (Windows — Do This First)

### Step 1 — Install Python 3.10+

Download from https://www.python.org/downloads/
During install: CHECK "Add Python to PATH"

Verify in Command Prompt:
```
python --version
```

### Step 2 — Install MySQL

Download MySQL Community Server from https://dev.mysql.com/downloads/mysql/
During setup: note your root password — you'll need it later.

### Step 3 — Create the Project Folder

```
mkdir traffic_system
cd traffic_system
```

Copy all project files into this folder.

### Step 4 — Create a Virtual Environment

```
python -m venv venv
```

This creates a `venv/` folder inside your project.
A virtual environment keeps your project's packages separate
from your system Python — very important.

### Step 5 — Activate the Virtual Environment

```
venv\Scripts\activate
```

Your terminal prompt will change to:
```
(venv) C:\...\traffic_system>
```

You must activate it every time you open a new terminal.

### Step 6 — Install Dependencies

```
pip install -r requirements.txt
```

This installs Flask, YOLOv8, OpenCV, etc.
First time takes 2–5 minutes. YOLOv8 is a large package.

### Step 7 — Set Up the Database

Open MySQL Workbench or MySQL Command Line Client, then run:

```
mysql -u root -p < schema.sql
```

Enter your MySQL root password when prompted.
This creates the `traffic_db` database and all tables.

### Step 8 — Update Your MySQL Password in database.py

Open `database.py` and find these lines near the top:

```python
DB_CONFIG = {
    "host":     "localhost",
    "user":     "root",         # ← your MySQL username
    "password": "yourpassword", # ← your MySQL password here
    "database": "traffic_db",
    ...
}
```

### Step 9 — Create the videos/ and models/ folders

```
mkdir videos
mkdir models
```

Drop any .mp4 test video into `videos/`.
The YOLOv8 model (`yolov8n.pt`) downloads automatically on first run.

---

## Running the System

Make sure your virtual environment is activated first (`venv\Scripts\activate`).

```
python app.py
```

Then open your browser and go to:
```
http://localhost:5000
```

### Switching Video Source

In the dashboard, type into the source box:
- `0` — default webcam
- `1` — second webcam
- `videos/sample.mp4` — a video file

Or in `app.py`, change this line directly:
```python
VIDEO_SOURCE = 0        # webcam
VIDEO_SOURCE = "videos/sample.mp4"   # video file
```

---

## Stopping the System

Press `Ctrl+C` in the terminal. The system will cleanly:
- Stop the signal controller thread
- Flush any remaining DB writes
- Release the video capture

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` with venv active |
| Webcam not opening | Change `VIDEO_SOURCE = 0` to `VIDEO_SOURCE = 1` |
| MySQL connection error | Check username/password in `database.py` |
| `yolov8n.pt` not found | Delete `models/` folder and let it auto-download |
| Dashboard not updating | Check browser console for SocketIO errors |
| Slow detection | Increase `frame_skip` in `detector.py` from 2 to 4 |

---

## Adjusting Detection Zones

If your camera angle is different, edit the zone coordinates in `app.py`:

```python
detector.add_zone("lane_north", [(252, 30),  (428, 30),  (428, 225), (252, 225)])
detector.add_zone("lane_south", [(252, 395), (428, 395), (428, 590), (252, 590)])
detector.add_zone("lane_west",  [(30,  252), (225, 252), (225, 368), (30,  368)])
detector.add_zone("lane_east",  [(455, 252), (650, 252), (650, 368), (455, 368)])
```

Each tuple is an `(x, y)` pixel coordinate on the video frame.
Run `detector.py` standalone first to see what the zones look like:

```
python detector.py
```

Press `q` to quit the preview window.
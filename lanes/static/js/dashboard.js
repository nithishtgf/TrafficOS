/* =================================================================
   dashboard.js
   TrafficOS — Live dashboard controller
   Handles: SocketIO events, signal rendering, Chart.js, history
   ================================================================= */

'use strict';

// ── State ──────────────────────────────────────────────────────────
const state = {
  lanes:        ['lane_north', 'lane_south', 'lane_east', 'lane_west'],
  signals:      {},
  counts:       {},
  maxVehicles:  30,
  chartHours:   6,
  chart:        null,
  connected:    false,
};

const LANE_LABELS = {
  lane_north: 'North',
  lane_south: 'South',
  lane_east:  'East',
  lane_west:  'West',
};

const LANE_SHORT = {
  lane_north: 'N',
  lane_south: 'S',
  lane_east:  'E',
  lane_west:  'W',
};

// ── SocketIO ───────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  state.connected = true;
  setStatus('online', 'System Online');
  document.getElementById('video-overlay').classList.add('hidden');
  loadStats();
  loadHistory();
  loadConfig();
});

socket.on('disconnect', () => {
  state.connected = false;
  setStatus('offline', 'Disconnected');
});

socket.on('connect_error', () => {
  setStatus('offline', 'Connection Error');
});

socket.on('init', (data) => {
  if (data.lanes) state.lanes = data.lanes;
  if (data.signals) renderSignals(data.signals);
  buildSignalGrid();
  loadChartData(state.chartHours);
});

socket.on('counts_update', (data) => {
  if (data.counts)  updateCounts(data.counts);
  if (data.signals) renderSignals(data.signals);
});

socket.on('signal_update', (signals) => {
  renderSignals(signals);
});

// ── Status bar ─────────────────────────────────────────────────────
function setStatus(cls, text) {
  const dot  = document.getElementById('status-dot');
  const span = document.getElementById('status-text');
  dot.className  = `status-dot ${cls}`;
  span.textContent = text;
}

// ── Clock ──────────────────────────────────────────────────────────
function updateClock() {
  document.getElementById('nav-time').textContent =
    new Date().toLocaleTimeString('en-US', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// ── Lane count cards ───────────────────────────────────────────────
function updateCounts(counts) {
  state.counts = counts;
  const maxSeen = Math.max(state.maxVehicles, ...Object.values(counts));

  for (const [lane, total] of Object.entries(counts)) {
    const countEl = document.getElementById(`count-${lane}`);
    const barEl   = document.getElementById(`bar-${lane}`);
    if (countEl) countEl.textContent = total;
    if (barEl) {
      const pct = Math.min((total / maxSeen) * 100, 100);
      barEl.style.width = `${pct}%`;
    }
  }
}

// ── Signal Grid ────────────────────────────────────────────────────
function buildSignalGrid() {
  const grid = document.getElementById('signals-grid');
  grid.innerHTML = '';
  for (const lane of state.lanes) {
    const card = document.createElement('div');
    card.className = 'signal-card state-red';
    card.id = `sig-${lane}`;
    card.innerHTML = `
      <div class="sig-top">
        <span class="sig-name">${LANE_SHORT[lane] || lane}</span>
        <div class="sig-lights">
          <span class="sig-light r" id="light-r-${lane}"></span>
          <span class="sig-light y" id="light-y-${lane}"></span>
          <span class="sig-light g" id="light-g-${lane}"></span>
        </div>
      </div>
      <div class="sig-progress">
        <div class="sig-progress-fill" id="prog-${lane}"></div>
      </div>
      <div class="sig-meta">
        <span class="sig-state" id="state-${lane}">RED</span>
        <span class="sig-remaining" id="rem-${lane}">—</span>
        <span class="sig-vehicles" id="veh-${lane}">0 v</span>
      </div>
    `;
    grid.appendChild(card);
  }
}

function renderSignals(signals) {
  state.signals = signals;
  let activeLane = '—';

  for (const [lane, data] of Object.entries(signals)) {
    const card    = document.getElementById(`sig-${lane}`);
    const laneCard = document.getElementById(`card-${lane}`);
    if (!card) continue;

    const s   = data.state;         // 'green' | 'yellow' | 'red'
    const rem = data.remaining;
    const gt  = data.green_time;
    const vc  = data.vehicle_count;

    // Card state class
    card.className = `signal-card state-${s}`;

    // Traffic lights
    setLight(lane, 'r', s === 'red');
    setLight(lane, 'y', s === 'yellow');
    setLight(lane, 'g', s === 'green');

    // Progress bar (% remaining of green_time)
    const prog = document.getElementById(`prog-${lane}`);
    if (prog && gt > 0 && s === 'green') {
      prog.style.width = `${Math.min((rem / gt) * 100, 100)}%`;
    } else if (prog) {
      prog.style.width = s === 'yellow' ? '100%' : '0%';
    }

    // Text fields
    const stateEl = document.getElementById(`state-${lane}`);
    const remEl   = document.getElementById(`rem-${lane}`);
    const vehEl   = document.getElementById(`veh-${lane}`);
    if (stateEl) stateEl.textContent = s.toUpperCase();
    if (remEl)   remEl.textContent   = s !== 'red' ? `${rem.toFixed(1)}s` : '—';
    if (vehEl)   vehEl.textContent   = `${vc} v`;

    // Active lane highlight
    if (s === 'green') {
      activeLane = LANE_LABELS[lane] || lane;
      laneCard && laneCard.classList.add('active-lane');
    } else {
      laneCard && laneCard.classList.remove('active-lane');
    }
  }

  document.getElementById('active-lane').textContent = activeLane;
}

function setLight(lane, color, on) {
  const el = document.getElementById(`light-${color}-${lane}`);
  if (!el) return;
  if (on) el.classList.add('active');
  else    el.classList.remove('active');
}

// ── Chart ──────────────────────────────────────────────────────────
function initChart() {
  const ctx = document.getElementById('traffic-chart').getContext('2d');

  state.chart = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400 },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: {
            color: '#7a8599',
            font: { family: "'JetBrains Mono', monospace", size: 10 },
            boxWidth: 10,
            padding: 10,
          }
        },
        tooltip: {
          backgroundColor: '#141c2e',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          titleColor: '#e8edf5',
          bodyColor: '#7a8599',
          titleFont: { family: "'JetBrains Mono', monospace", size: 11 },
          bodyFont:  { family: "'JetBrains Mono', monospace", size: 10 },
        }
      },
      scales: {
        x: {
          stacked: false,
          ticks: {
            color: '#3d4a60',
            font: { family: "'JetBrains Mono', monospace", size: 9 },
            maxRotation: 0,
          },
          grid: { color: 'rgba(255,255,255,0.04)' },
        },
        y: {
          beginAtZero: true,
          ticks: {
            color: '#3d4a60',
            font: { family: "'JetBrains Mono', monospace", size: 9 },
          },
          grid: { color: 'rgba(255,255,255,0.04)' },
        }
      }
    }
  });
}

const LANE_COLORS = {
  lane_north: '#448aff',
  lane_east:  '#ff6d00',
  lane_south: '#00e676',
  lane_west:  '#aa00ff',
};

async function loadChartData(hours) {
  try {
    const res  = await fetch(`/api/history/hourly?hours=${hours}`);
    const data = await res.json();

    // Group by hour label
    const hoursMap = {};
    const lanesSet = new Set();

    for (const row of data) {
      const h = row.hour;
      lanesSet.add(row.lane_id);
      if (!hoursMap[h]) hoursMap[h] = {};
      hoursMap[h][row.lane_id] = Math.round(row.avg_vehicles);
    }

    const labels   = Object.keys(hoursMap).sort().slice(-hours);
    const laneList = [...lanesSet];

    const datasets = laneList.map(lane => ({
      label:           LANE_LABELS[lane] || lane,
      data:            labels.map(h => hoursMap[h]?.[lane] ?? 0),
      backgroundColor: (LANE_COLORS[lane] || '#448aff') + '99',
      borderColor:     LANE_COLORS[lane] || '#448aff',
      borderWidth:     1,
      borderRadius:    3,
    }));

    state.chart.data.labels   = labels.map(h => h.split(' ')[1] || h);
    state.chart.data.datasets = datasets;
    state.chart.update();

  } catch (e) {
    console.warn('[Chart] Failed to load data:', e);
  }
}

function setChartHours(hours, btn) {
  state.chartHours = hours;
  document.querySelectorAll('.btn-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadChartData(hours);
}

// ── Stats summary ──────────────────────────────────────────────────
async function loadStats() {
  try {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('stat-total').textContent  = (data.total_vehicles_today  || 0).toLocaleString();
    document.getElementById('stat-green').textContent  = `${data.avg_green_time  || 0}s`;
    document.getElementById('stat-peak').textContent   = data.peak_vehicles  || 0;

    // Cycle count from signal cycles endpoint
    const cycRes  = await fetch('/api/signal_cycles?limit=1');
    const cycData = await cycRes.json();
    document.getElementById('stat-cycles').textContent = cycData.length > 0 ? '...' : '0';

  } catch (e) {
    console.warn('[Stats] Failed:', e);
  }
}

// ── History table ──────────────────────────────────────────────────
async function loadHistory() {
  const tbody = document.getElementById('history-tbody');
  try {
    const res  = await fetch('/api/signal_cycles?limit=30');
    const data = await res.json();

    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="table-empty">No history yet</td></tr>';
      return;
    }

    tbody.innerHTML = data.map(row => {
      const d    = Math.min(Math.round((row.density || 0) * 100), 100);
      const time = formatRelativeTime(row.cycled_at);
      return `
        <tr>
          <td>${LANE_LABELS[row.lane_id] || row.lane_id}</td>
          <td>${Math.round(row.green_time)}s</td>
          <td>${row.vehicle_count}</td>
          <td><span class="density-bar" style="width:${d}px" title="${d}%"></span></td>
          <td>${time}</td>
        </tr>
      `;
    }).join('');

  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" class="table-empty">Error loading history</td></tr>';
  }
}

function formatRelativeTime(isoStr) {
  if (!isoStr) return '—';
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60)   return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

// ── Video source ───────────────────────────────────────────────────
async function setSource() {
  const val = document.getElementById('source-input').value.trim();
  if (!val) return;
  try {
    await fetch('/api/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: val }),
    });
    document.getElementById('video-feed').src = `/video_feed?t=${Date.now()}`;
  } catch (e) {
    console.warn('[Source] Failed:', e);
  }
}

// ── Config panel ───────────────────────────────────────────────────
function toggleConfig() {
  document.getElementById('config-panel').classList.toggle('visible');
  document.getElementById('config-overlay').classList.toggle('visible');
}

async function loadConfig() {
  try {
    const res  = await fetch('/api/config');
    const data = await res.json();
    document.getElementById('cfg-min-green').value    = data.min_green;
    document.getElementById('cfg-max-green').value    = data.max_green;
    document.getElementById('cfg-base-green').value   = data.base_green;
    document.getElementById('cfg-yellow').value       = data.yellow_time;
    document.getElementById('cfg-density').value      = data.density_factor;
    document.getElementById('cfg-max-vehicles').value = data.max_vehicles;
    state.maxVehicles = data.max_vehicles;
  } catch (e) {
    console.warn('[Config] Failed to load:', e);
  }
}

async function saveConfig() {
  const payload = {
    min_green:      parseFloat(document.getElementById('cfg-min-green').value),
    max_green:      parseFloat(document.getElementById('cfg-max-green').value),
    base_green:     parseFloat(document.getElementById('cfg-base-green').value),
    yellow_time:    parseFloat(document.getElementById('cfg-yellow').value),
    density_factor: parseFloat(document.getElementById('cfg-density').value),
    max_vehicles:   parseInt(document.getElementById('cfg-max-vehicles').value),
  };
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    const msg  = document.getElementById('config-msg');
    msg.textContent = data.status === 'updated' ? '✓ Saved successfully' : '✗ Save failed';
    state.maxVehicles = payload.max_vehicles;
    setTimeout(() => { msg.textContent = ''; }, 3000);
  } catch (e) {
    document.getElementById('config-msg').textContent = '✗ Network error';
  }
}

// ── Auto-refresh ───────────────────────────────────────────────────
setInterval(loadStats,   30_000);
setInterval(loadHistory, 15_000);
setInterval(() => loadChartData(state.chartHours), 60_000);

// ── Boot ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  buildSignalGrid();
  initChart();
});

// ── Toggle combined / 4-grid view ─────────────────────────────────
let _combinedView = false;
function toggleCombined() {
  _combinedView = !_combinedView;
  document.getElementById('feeds-grid').classList.toggle('hidden', _combinedView);
  document.getElementById('feeds-combined').classList.toggle('hidden', !_combinedView);
  document.getElementById('btn-combined').textContent =
    _combinedView ? 'Split view' : 'Combined view';
}
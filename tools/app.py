from datetime import datetime
import json
import os
import sqlite3
import sys
from flask import Flask, g, redirect, render_template_string, request, url_for, jsonify
import time 

TOTAL_BATTERY_KWH = 18.3
TOTAL_FUEL_LITERS = 60.0

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
  import server
  from server import live_poll, DB
except ImportError:
  server = None
  live_poll = DB = None

app = Flask(__name__)
DATABASE = "trips.db"
RATES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "rates.json"
)

DEFAULT_RATES = {
    "fuel_price_per_l": 1.99,
    "elec_price_per_kwh": 0.33,
    "fuel_tank_capacity_l": 60.0,
    "battery_capacity_kwh": 18.3,
}

def convert_to_units(data):
    odo = data.get("odo", 0.0)
    
    # Convert battery percentage to kWh
    batt_pct = data.get("battery", 0.0)
    batt_kwh = (batt_pct / 100.0) * TOTAL_BATTERY_KWH
    
    # Convert fuel percentage to liters (60L full tank)
    fuel_pct = data.get("fuel_pct", 0.0)
    fuel_liters = (fuel_pct / 100.0) * TOTAL_FUEL_LITERS
    
    return odo, batt_kwh, fuel_liters

def load_rates():
  if os.path.exists(RATES_FILE):
    try:
      with open(RATES_FILE, "r") as f:
        return {**DEFAULT_RATES, **json.load(f)}
    except Exception as e:
      print(f"Error loading rates.json: {e}")
  return DEFAULT_RATES.copy()


def save_rates(rates):
  try:
    with open(RATES_FILE, "w") as f:
      json.dump(rates, f, indent=2)
  except Exception as e:
    print(f"Error saving rates.json: {e}")


def get_db():
  db = getattr(g, "_database", None)
  if db is None:
    db = g._database = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
  return db


@app.teardown_appcontext
def close_connection(exception):
  db = getattr(g, "_database", None)
  if db is not None:
    db.close()


def init_db():
  with app.app_context():
    db = get_db()
    db.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_type TEXT DEFAULT 'A',
                start_time TEXT,
                end_time TEXT,
                start_odo REAL,
                end_odo REAL,
                start_battery REAL,
                end_battery REAL,
                start_fuel REAL,
                end_fuel REAL,
                distance REAL,
                status TEXT
            )
        """)
    try:
      db.execute("ALTER TABLE trips ADD COLUMN trip_type TEXT DEFAULT 'A'")
    except sqlite3.OperationalError:
      pass
    db.commit()

def fetch_car_telemetry(active_a_start_odo=None, active_b_start_odo=None):
    """Fetch latest car telemetry directly from the 9-column carlinko.db schema with zero raw hex decoding."""
    HOLD = 120  # 2 minutes grace period for offline check
    try:
        if server and hasattr(server, "live_poll"):
            live_poll()

        db_path = DB if DB else "carlinko.db"
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=5.0)
            cursor = conn.cursor()
            
            # Select directly from the 9 clean columns in carlinko.db
            cursor.execute("""
                SELECT ts, dt, battery, fuel_pct, odo_guess, tyre_raw, online, volt12, speed 
                FROM telemetry 
                ORDER BY ts DESC 
                LIMIT 1
            """)
            row = cursor.fetchone()
            conn.close()

            if not row:
                return {
                    "status": "empty_db", "car_status": "OFFLINE", "odo": 0.0,
                    "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
                    "volt12": 0.0, "min_tyre": "--", "msg": "No telemetry records",
                    "dist_a": 0.0, "dist_b": 0.0
                }

            ts, dt, battery, fuel_pct, odo_guess, tyre_raw, online_flag, volt12, speed = row
            age = time.time() - ts

            # --- Direct State Machine using online flag & age ---
            #if age > HOLD or online_flag == 0:
            if online_flag == 0:
                car_status = "OFFLINE"
            #elif online_flag == 1 or (speed is not None and speed > 0.0):
            elif online_flag == 1 or age < HOLD
                car_status = "ACTIVE"
            else:
                car_status = "PARKED"

            current_odo = float(odo_guess or 0.0)
            battery_pct_val = float(battery or 0.0)
            battery_kwh = round((battery_pct_val / 100.0) * TOTAL_BATTERY_KWH, 2)
            fuel_val = float(fuel_pct or 0.0)
            volt12_val = float(volt12 or 0.0)
            min_tyre = tyre_raw if tyre_raw else "--"

            # Compute live distances for Trip A and B
            live_dist_a = max(0.0, current_odo - active_a_start_odo) if active_a_start_odo and current_odo > 0 else 0.0
            live_dist_b = max(0.0, current_odo - active_b_start_odo) if active_b_start_odo and current_odo > 0 else 0.0

            return {
                "status": "ok",
                "car_status": car_status,
                "odo": current_odo,
                "battery": battery_pct_val,
                "battery_kwh": battery_kwh,
                "fuel_pct": fuel_val,
                "volt12": volt12_val,
                "min_tyre": min_tyre,
                "dist_a": live_dist_a,
                "dist_b": live_dist_b,
                "msg": f"Telemetry active ({car_status})",
            }

        return {
            "status": "empty_db", "car_status": "OFFLINE", "odo": 0.0,
            "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
            "volt12": 0.0, "min_tyre": "--", "msg": "No database found",
            "dist_a": 0.0, "dist_b": 0.0
        }
    except Exception as e:
        print(f"Telemetry fetch error: {e}")
        return {
            "status": "error", "car_status": "OFFLINE", "odo": 0.0,
            "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
            "volt12": 0.0, "min_tyre": "Error", "msg": str(e),
            "dist_a": 0.0, "dist_b": 0.0
        }


def calculate_trip_cost(distance, start_bat, end_bat, start_fuel, end_fuel, rates):
    if not distance or distance <= 0:
        return 0.0, 0.0

    # Since start_bat and start_fuel already include any overnight/mid-trip additions 
    # (updated upon trip completion), we just do direct subtraction:
    kwh_used = (start_bat or 0.0) - (end_bat or 0.0)
    elec_cost = kwh_used * rates["elec_price_per_kwh"]

    liters_used = max(0.0, (start_fuel or 0.0) - (end_fuel or 0.0))
    fuel_cost = liters_used * rates["fuel_price_per_l"]

    total_cost = elec_cost + fuel_cost
    cost_per_km = total_cost / distance if distance > 0 else 0.0

    return total_cost, cost_per_km


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PHEV Dual-Trip Tracker</title>
    <!-- Include Chart.js CDN -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 16px; display: flex; justify-content: center; }
        .container { width: 100%; max-width: 520px; }
        .header-box { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 16px; }
        h1 { font-size: 1.4rem; margin: 0; color: #fff; }
        
        /* LED Indicator Styles */
        .led-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
            transition: background-color 0.3s ease, box-shadow 0.3s ease;
        }
        .led-ok { background-color: #2ecc71; box-shadow: 0 0 10px #2ecc71; }
        .led-empty { background-color: #f39c12; box-shadow: 0 0 10px #f39c12; }
        .led-error { background-color: #e74c3c; box-shadow: 0 0 10px #e74c3c; }
        .sub-status-bar {
            display: flex;
            justify-content: center;
            gap: 15px;
            background: #1e1e1e;
            padding: 8px;
            border-radius: 8px;
            margin-bottom: 16px;
            font-size: 0.85rem;
            color: #b0bec5;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }
        .sub-status-bar strong {
            color: #4caf50;
        }
        .trips-grid { display: flex; gap: 12px; margin-bottom: 16px; }
        .card { background: #1e1e1e; border-radius: 12px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); text-align: center; flex: 1; }
        .card-title { font-size: 1.1rem; font-weight: bold; margin-bottom: 12px; color: #64b5f6; }
        .btn { display: block; width: 100%; padding: 14px 8px; font-size: 1rem; font-weight: bold; border: none; border-radius: 8px; cursor: pointer; color: white; }
        .btn-start { background-color: #2e7d32; }
        .btn-end { background-color: #c62828; }
        .btn-save { background-color: #0288d1; margin-top: 10px; padding: 8px; font-size: 0.85rem; }
        .status-text { font-size: 0.9rem; margin-bottom: 12px; color: #b0bec5; }
        .stat-value { font-size: 1.1rem; font-weight: bold; color: #4caf50; margin-bottom: 2px; }
        .stat-label { font-size: 0.75rem; color: #888; text-transform: uppercase; margin-bottom: 10px; }
        
        /* Efficiency Chart Card Styles */
        .chart-card { background: #1e1e1e; border-radius: 12px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); margin-bottom: 16px; text-align: center; }
        .chart-container { position: relative; height: 220px; width: 100%; margin-top: 10px; }

        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.8rem; }
        th, td { padding: 8px 4px; text-align: left; border-bottom: 1px solid #2c2c2c; }
        th { color: #888; font-weight: 600; }
        .badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.75rem; }
        .badge-a { background: #1565c0; color: white; }
        .badge-b { background: #6a1b9a; color: white; }
        .cost-highlight { color: #4caf50; font-weight: bold; }
        .rates-form { display: flex; gap: 10px; justify-content: space-between; text-align: left; margin-top: 8px; }
        .rates-field { flex: 1; }
        .rates-field label { display: block; font-size: 0.7rem; color: #aaa; margin-bottom: 2px; }
        .rates-field input { width: 85%; padding: 6px; border-radius: 4px; border: 1px solid #333; background: #2a2a2a; color: #fff; font-size: 0.85rem; }
        details summary { cursor: pointer; color: #bbb; font-size: 0.85rem; }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.75rem; }
        th, td { padding: 8px 2px; text-align: center; border-bottom: 1px solid #2c2c2c; }
        th { color: #888; font-weight: 600; font-size: 0.7rem; }
        td:nth-child(2) { font-size: 0.7rem; color: #aaa; white-space: nowrap; } /* Prevents date breaking */
        .card-active { border: 1px solid #2e7d32; box-shadow: 0 0 12px rgba(46, 125, 50, 0.25); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-box">
            <span id="led-status" class="led-indicator led-empty" title="Checking status..."></span>
            <h1>🚗 PHEV Dual-Trip Tracker</h1>
        </div>
        <div class="sub-status-bar">
            <span>⚡ 12V: <strong id="val-volt12">{{ "%.1f"|format(telemetry.volt12) }}V</strong></span>
            <span>|</span>
            <span>🛞 Tyre Min: <strong id="val-tyre">{{ telemetry.min_tyre }}</strong></span>
        </div>
        
        <div class="trips-grid">
            <!-- TRIP A CARD -->
            <div class="card">
                <div class="card-title">Trip A </div>
                {% if active_a %}
                    <div class="status-text">In Progress...</div>
                    <div class="stat-value" id="timer-a">-- hr -- min</div>
                    <div class="stat-label">Elapsed Time</div>
                    <div class="stat-value" id="trip-a-mileage-value">{{ "%d"|format(active_a.live_dist|int) if active_a.live_dist > 0 else '0.0' }} km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='A') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip A</button>
                    </form>
                    
                    <script>
                        const startA = new Date("{{ active_a.start_time }}").getTime();
                        function updateTripA() {
                            const now = new Date().getTime();
                            const diffMins = Math.floor((now - startA) / 60000);
                            const days = Math.floor(diffMins / 1440);
                            const hours = Math.floor((diffMins % 1440) / 60);
                            const m = diffMins % 60;
                
                            if (days > 0) {
                                document.getElementById('timer-a').innerText = `${days} D - ${hours} Hr`;
                            } else {
                                const h = Math.floor(diffMins / 60);
                                document.getElementById('timer-a').innerText = `${h} hr ${m} min`;
                            }
                        }
                        setInterval(updateTripA, 10000); updateTripA();
                    </script>
                {% else %}
                    <div class="status-text">Ready</div>
                    <form action="{{ url_for('start_trip', trip_type='A') }}" method="POST">
                        <button type="submit" class="btn btn-start">Start Trip A</button>
                    </form>
                {% endif %}
            </div>

            <!-- TRIP B CARD -->
            <div class="card">
                <div class="card-title">Trip B </div>
                {% if active_b %}
                    <div class="status-text">In Progress...</div>
                    <div class="stat-value" id="timer-b">-- hr -- min</div>
                    <div class="stat-label">Elapsed Time</div>
                    <div class="stat-value" id="trip-b-mileage-value">{{ "%d"|format(active_b.live_dist|int) if active_b.live_dist > 0 else '0.0' }} km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='B') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip B</button>
                    </form>
                    <script>
                        const startB = new Date("{{ active_b.start_time }}").getTime();
                        function updateTripB() {
                            const now = new Date().getTime();
                            const diffMins = Math.floor((now - startB) / 60000);
                            const days = Math.floor(diffMins / 1440);
                            const hours = Math.floor((diffMins % 1440) / 60);
                            const m = diffMins % 60;
                
                            if (days > 0) {
                                document.getElementById('timer-b').innerText = `${days} D - ${hours} Hr`;
                            } else {
                                const h = Math.floor(diffMins / 60);
                                document.getElementById('timer-b').innerText = `${h} hr ${m} min`;
                            }
                        }
                        setInterval(updateTripB, 10000); updateTripB();
                    </script>
                {% else %}
                    <div class="status-text">Ready</div>
                    <form action="{{ url_for('start_trip', trip_type='B') }}" method="POST">
                        <button type="submit" class="btn btn-start">Start Trip B</button>
                    </form>
                {% endif %}
            </div>
        </div>

        <!-- 5-MINUTE INTERVAL EFFICIENCY BAR CHART CARD -->
        <div class="chart-card">
            <div class="card-title" style="margin-bottom: 4px;">Efficiency Trend</div>
            <div style="font-size: 0.75rem; color: #888; text-transform: uppercase; margin-bottom: 10px;">RM / 100km (5-Min Intervals)</div>
            <div class="chart-container">
                <canvas id="efficiencyChart"></canvas>
            </div>
        </div>

        <!-- SETTINGS & HISTORY CARD -->
        <div class="card" style="text-align: left;">
            <details style="margin-bottom: 12px;">
                <summary>⚙️ Tariff & Price Settings</summary>
                <form action="{{ url_for('update_rates') }}" method="POST">
                    <div class="rates-form">
                        <div class="rates-field">
                            <label>Fuel (RM/L)</label>
                            <input type="number" step="0.01" name="fuel_price_per_l" value="{{ rates.fuel_price_per_l }}">
                        </div>
                        <div class="rates-field">
                            <label>Elec (RM/kWh)</label>
                            <input type="number" step="0.01" name="elec_price_per_kwh" value="{{ rates.elec_price_per_kwh }}">
                        </div>
                    </div>
                    <button type="submit" class="btn btn-save">Save Rates</button>
                </form>
            </details>

            <h3 style="margin-top: 0; font-size: 1.05rem; color: #fff;">Trip History</h3>
              <table class="history-table">
                  <thead>
                      <tr>
                          <th>Trip</th>
                          <th>Date & Time</th>
                          <th>Dist km</th>
                          <th>Bat kWh</th>
                          <th>Fuel L</th>
                          <th>Total Cost</th>
                          <th>Cost/ 100km</th>
                      </tr>
                  </thead>
                  <tbody>
                    {% for item in completed_trips %}
                    <tr>
                        <td><span class="badge {{ 'badge-a' if item.trip.trip_type == 'A' else 'badge-b' }}">{{ item.trip.trip_type }}</span></td>
                        <td>{{ item.trip.end_time[5:16] }}</td> <!-- Strips year (first 5 chars) and seconds (last 3 chars) -->
                        <td>{{ "%.0f"|format(item.trip.distance) }}</td>
                        <td>{{ "%.2f"|format(item.batt_used) }}</td>
                        <td>{{ "%.1f"|format(item.fuel_used) }}</td>
                        <td class="cost-highlight">RM {{ "%.2f"|format(item.total_cost) }}</td>
                        <td>RM {{ "%.2f"|format(item.cost_per_100km) }}</td>
                    </tr>
                    {% endfor %}
                 </tbody>
              </table>
        </div>
    </div>

    <script>
        // Initialize Chart.js Bar Chart
        const ctx = document.getElementById('efficiencyChart').getContext('2d');
        const efficiencyChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [{
                    label: 'RM / 100km',
                    data: [],
                    backgroundColor: 'rgba(76, 175, 80, 0.6)',
                    borderColor: 'rgba(76, 175, 80, 1)',
                    borderWidth: 1,
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: '#2c2c2c' },
                        ticks: { color: '#aaa', font: { size: 10 } },
                        title: { display: false }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#aaa', font: { size: 10 } }
                    }
                }
            }
        });

        // Function to update the 5-minute efficiency chart
        async function updateEfficiencyChart() {
            try {
                const response = await fetch('/api/efficiency-trend');
                const data = await response.json();
                
                let labels = data.labels;
                let efficiency = data.efficiency;
                
                // Truncate to maximum 12 bars (60 minutes rolling window)
                const MAX_BARS = 12;
                if (labels.length > MAX_BARS) {
                    labels = labels.slice(-MAX_BARS);
                    efficiency = efficiency.slice(-MAX_BARS);
                }
                
                efficiencyChart.data.labels = labels;
                efficiencyChart.data.datasets[0].data = efficiency;
                efficiencyChart.update();
            } catch (err) {
                console.error("Error updating efficiency chart:", err);
            }
        }

        // Initial fetch on page load
        updateEfficiencyChart();
        
        // Refresh efficiency chart every 5 minutes (300,000 ms)
        setInterval(updateEfficiencyChart, 300000);

        // General status & telemetry ticker
        function checkTelemetryStatus() {
            fetch('/api/telemetry_status')
                .then(response => response.json())
                .then(data => {
                    const led = document.getElementById('led-status');
                    if (led) {
                        led.className = 'led-indicator';
                        if (data.car_status === 'ACTIVE') {
                            led.classList.add('led-ok');
                            led.title = "Car Status: ACTIVE (Moving / Awake)";
                        } else if (data.car_status === 'PARKED') {
                            led.classList.add('led-empty');
                            led.title = "Car Status: PARKED (Idle / Engine On)";
                        } else {
                            led.classList.add('led-error');
                            led.title = "Car Status: OFFLINE / Dark";
                        }
                    }
                    const voltEl = document.getElementById('val-volt12');
                    if (voltEl && data.volt12 !== undefined) {
                        voltEl.innerText = data.volt12.toFixed(1) + 'V';
                    }
    
                    const tyreEl = document.getElementById('val-tyre');
                    if (tyreEl && data.min_tyre !== undefined) {
                        tyreEl.innerText = data.min_tyre;
                    }
                    if (data.dist_a !== undefined) {
                        const distAEl = document.getElementById('trip-a-mileage-value');
                        if (distAEl) {
                            distAEl.innerText = data.dist_a.toFixed(0) + ' km';
                        }
                    }

                    if (data.dist_b !== undefined) {
                        const distBEl = document.getElementById('trip-b-mileage-value');
                        if (distBEl) {
                            distBEl.innerText = data.dist_b.toFixed(0) + ' km';
                        }
                    }
                })
                .catch(err => {
                    const led = document.getElementById('led-status');
                    if (led) {
                        led.className = 'led-indicator led-error';
                        led.title = "Server Connection Failed";
                    }
                    console.error("Failed to fetch live telemetry:", err);
                });
        }

        checkTelemetryStatus();
        setInterval(checkTelemetryStatus, 10000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    rates = load_rates()
    db = get_db()  # Connected to trips.db
    
    active_a = db.execute(
        "SELECT * FROM trips WHERE trip_type = 'A' AND status = 'ACTIVE' LIMIT 1"
    ).fetchone()
    active_b = db.execute(
        "SELECT * FROM trips WHERE trip_type = 'B' AND status = 'ACTIVE' LIMIT 1"
    ).fetchone()

    start_odo_a = active_a["start_odo"] if active_a else None
    start_odo_b = active_b["start_odo"] if active_b else None
    
    current_telemetry = fetch_car_telemetry(start_odo_a, start_odo_b)

    active_a_dict = dict(active_a) if active_a else None
    if active_a_dict:
        active_a_dict["live_dist"] = current_telemetry["dist_a"]

    active_b_dict = dict(active_b) if active_b else None
    if active_b_dict:
        active_b_dict["live_dist"] = current_telemetry["dist_b"]

    raw_completed = db.execute(
        "SELECT * FROM trips WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 10"
    ).fetchall()

    completed_trips = []
    
    for trip in raw_completed:
        kwh_added = 0.0
        fuel_added = 0.0
        
        db_path = DB if DB else "carlinko.db"
        if os.path.exists(db_path):
            try:
                with sqlite3.connect(db_path, timeout=5.0) as cl_conn:
                    cl_cursor = cl_conn.cursor()
                    cl_cursor.execute(
                        "SELECT SUM(kwh_added) FROM charging_logs WHERE ts BETWEEN ? AND ?",
                        (trip["start_time"], trip["end_time"])
                    )
                    res_kwh = cl_cursor.fetchone()[0]
                    kwh_added = res_kwh if res_kwh else 0.0

                    cl_cursor.execute(
                        "SELECT SUM(liters_added) FROM fuel_logs WHERE ts BETWEEN ? AND ?",
                        (trip["start_time"], trip["end_time"])
                    )
                    res_liters = cl_cursor.fetchone()[0]
                    fuel_added = res_liters if res_liters else 0.0
            except Exception:
                pass

        total_cost, cost_per_km = calculate_trip_cost(
            trip["distance"],
            trip["start_battery"],  # <-- updated key name
            trip["end_battery"],
            trip["start_fuel"],     # <-- updated key name
            trip["end_fuel"],
            rates,
        )
        cost_per_100km = cost_per_km * 100.0
        
        total_start_batt = trip["start_battery"] + kwh_added
        batt_used_kwh = total_start_batt - trip["end_battery"]

        total_start_fuel = trip["start_fuel"] + fuel_added
        fuel_used_l = max(0.0, total_start_fuel - trip["end_fuel"])
        
        completed_trips.append({
            "trip": trip, 
            "total_cost": total_cost, 
            "cost_per_100km": cost_per_100km,
            "batt_used": batt_used_kwh,
            "fuel_used": fuel_used_l
        })

    return render_template_string(
        HTML_TEMPLATE,
        active_a=active_a_dict,
        active_b=active_b_dict,
        completed_trips=completed_trips,
        rates=rates,
        telemetry=current_telemetry,
    )


@app.route("/api/telemetry_status", methods=["GET"])
def telemetry_status_endpoint():
  db = get_db()
  active_a = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'A' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()
  active_b = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'B' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()

  start_odo_a = active_a["start_odo"] if active_a else None
  start_odo_b = active_b["start_odo"] if active_b else None

  data = fetch_car_telemetry(start_odo_a, start_odo_b)
  return jsonify(data)

@app.route('/api/efficiency-trend')
def get_efficiency_trend():
    rates = load_rates()
    db = get_db() # Connected to trips.db
    
    # Get active trip start time from trips.db
    active_trip = db.execute(
        "SELECT start_time FROM trips WHERE status = 'ACTIVE' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    
    if active_trip and active_trip["start_time"]:
        trip_start_ts = int(datetime.strptime(active_trip["start_time"], "%Y-%m-%d %H:%M:%S").timestamp())
    else:
        trip_start_ts = int(time.time()) - 7200 # Fallback to last 2 hours

    labels = []
    efficiency_data = []

    db_path = DB if DB else "carlinko.db"
    if os.path.exists(db_path):
        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                
                # Query only ACTIVE logs (online = 1) since the trip started
                query = """
                    SELECT 
                        strftime('%H:%M', (ts / 300) * 300, 'unixepoch', 'localtime') AS time_bucket,
                        MAX(odo_guess) - MIN(odo_guess) AS dist_km,
                        MAX(fuel_pct) - MIN(fuel_pct) AS fuel_drop,
                        MAX(battery) - MIN(battery) AS batt_drop
                    FROM telemetry
                    WHERE ts >= ? AND online = 1
                    GROUP BY time_bucket
                    ORDER BY time_bucket DESC
                    LIMIT 12
                """
                cursor.execute(query, (trip_start_ts,))
                rows = cursor.fetchall()
                
                for row in reversed(rows):
                    time_bucket, dist_km, fuel_drop, batt_drop = row
                    
                    fuel_l = max(0.0, (fuel_drop or 0.0) / 100.0 * TOTAL_FUEL_LITERS)
                    elec_kwh = max(0.0, (batt_drop or 0.0) / 100.0 * TOTAL_BATTERY_KWH)
                    
                    total_cost_rm = (fuel_l * rates["fuel_price_per_l"]) + (elec_kwh * rates["elec_price_per_kwh"])
                    
                    if dist_km and dist_km > 0:
                        rm_per_100km = round((total_cost_rm / dist_km) * 100.0, 2)
                    else:
                        rm_per_100km = 0.0
                        
                    labels.append(time_bucket)
                    efficiency_data.append(rm_per_100km)
        except Exception as e:
            print(f"Efficiency trend error: {e}")

    return jsonify({
        "labels": labels,
        "efficiency": efficiency_data
    })

@app.route("/start_trip/<trip_type>", methods=["POST"])
def start_trip(trip_type):
  data = fetch_car_telemetry()
  odo, batt_kwh, fuel_liters = convert_to_units(data)
  
  db = get_db()
  db.execute(
      """
        INSERT INTO trips (trip_type, start_time, start_odo, start_battery, start_fuel, status)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE')
    """,
      [
          trip_type,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          odo,
          batt_kwh,
          fuel_liters,
      ],
  )
  db.commit()
  return redirect(url_for("index"))

@app.route("/end_trip/<trip_type>", methods=["POST"])
def end_trip(trip_type):
    data = fetch_car_telemetry()
    db = get_db()
    active_trip = db.execute(
        "SELECT * FROM trips WHERE trip_type = ? AND status = 'ACTIVE' LIMIT 1",
        [trip_type],
    ).fetchone()

    if active_trip:
        end_time_dt = datetime.now()
        end_time_str = end_time_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        current_odo = data.get("odo", 0.0)
        end_odo = current_odo if current_odo > 0 else active_trip["start_odo"]
        if end_odo < active_trip["start_odo"]:
            end_odo = active_trip["start_odo"]
            
        distance = end_odo - active_trip["start_odo"]

        # Convert percentages to kWh (18.3 kWh total) and Liters (60L tank)
        end_battery_kwh = (data.get("battery", 0.0) / 100.0) * TOTAL_BATTERY_KWH
        end_fuel_liters = (data.get("fuel_pct", 0.0) / 100.0) * TOTAL_FUEL_LITERS
        
        # --- QUERY LOGS FOR TOTAL ADDITIONS FROM carlinko.db (using DB variable) ---
        total_kwh_added = 0.0
        total_fuel_added = 0.0
        
        try:
            import time
            from datetime import datetime as dt
            start_epoch = int(dt.strptime(active_trip["start_time"], "%Y-%m-%d %H:%M:%S").timestamp())
            end_epoch = int(end_time_dt.timestamp())

            db_path = DB if DB else "carlinko.db"
            if os.path.exists(db_path):
                with sqlite3.connect(db_path) as log_conn:
                    log_cursor = log_conn.cursor()
                    
                    # Check if tables exist first to prevent crashes
                    tables = [row[0] for row in log_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                    
                    if "charging_logs" in tables:
                        kwh_added_res = log_cursor.execute(
                            "SELECT SUM(kwh_added) FROM charging_logs WHERE ts BETWEEN ? AND ?",
                            [start_epoch, end_epoch]
                        ).fetchone()
                        if kwh_added_res and kwh_added_res[0]:
                            total_kwh_added = kwh_added_res[0]

                    if "fuel_logs" in tables:
                        fuel_added_res = log_cursor.execute(
                            "SELECT SUM(liters_added) FROM fuel_logs WHERE ts BETWEEN ? AND ?",
                            [start_epoch, end_epoch]
                        ).fetchone()
                        if fuel_added_res and fuel_added_res[0]:
                            total_fuel_added = fuel_added_res[0]
                    
        except Exception as e:
            print(f"Log summation warning (defaulting to 0): {e}")
        # --------------------------------------------------------------------------

        db.execute(
            """
                UPDATE trips 
                SET end_time = ?, 
                    end_odo = ?, 
                    end_battery = ?, 
                    end_fuel = ?, 
                    distance = ?, 
                    start_battery = start_battery + ?,
                    start_fuel = start_fuel + ?,
                    status = 'COMPLETED'
                WHERE id = ?
            """,
            [
                end_time_str,
                end_odo,
                end_battery_kwh,
                end_fuel_liters,
                distance,
                total_kwh_added,
                total_fuel_added,
                active_trip["id"],
            ],
        )
        db.commit()

    return redirect(url_for("index"))

@app.route("/update_rates", methods=["POST"])
def update_rates():
  rates = load_rates()
  try:
    rates["fuel_price_per_l"] = float(
        request.form.get("fuel_price_per_l", rates["fuel_price_per_l"])
    )
    rates["elec_price_per_kwh"] = float(
        request.form.get("elec_price_per_kwh", rates["elec_price_per_kwh"])
    )
    save_rates(rates)
  except ValueError:
    pass
  return redirect(url_for("index"))


if __name__ == "__main__":
  init_db()
  app.run(host="0.0.0.0", port=8088)

from datetime import datetime
import json
import os
import sqlite3
import sys
from flask import Flask, g, redirect, render_template_string, request, url_for,jsonify
import time 

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
  import server
  from server import live_poll, DB, decode
except ImportError:
  server = None
  live_poll = DB = decode = None

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

def fetch_car_telemetry():
    """Fetch latest car telemetry from carlinko.db safely and determine car state using speed and a holding grace period."""
    HOLD = 120 # idle time when speed = 0
    try:
        if hasattr(server, "live_poll"):
            live_poll()

        db_path = DB if DB else "carlinko.db"
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=5.0)
            cursor = conn.cursor()
            
            # Fetch the latest telemetry record
            cursor.execute("SELECT ts, online, raw FROM telemetry ORDER BY ts DESC LIMIT 1")
            latest_row = cursor.fetchone()
            
            if not latest_row:
                conn.close()
                return {
                    "status": "empty_db", "car_status": "OFFLINE", "odo": 0.0,
                    "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
                    "volt12": 0.0, "min_tyre": "--", "msg": "No telemetry records"
                }

            latest_ts, online_flag, raw_hex = latest_row[0], latest_row[1], latest_row[2]
            age = time.time() - latest_ts

            # Check recent window (HOLD = 600s) for any movement (speed > 0)
            hold_window = time.time() - HOLD
            cursor.execute("SELECT raw FROM telemetry WHERE ts >= ?", (hold_window,))
            recent_rows = cursor.fetchall()
            conn.close()

            current_speed = 0.0
            has_recent_movement = False
            
            if raw_hex:
                decoded_latest = decode(raw_hex)
                current_speed = decoded_latest.get("speed", 0.0) or 0.0

            for r in recent_rows:
                if r[0]:
                    dec = decode(r[0])
                    if (dec.get("speed", 0.0) or 0.0) > 0.0:
                        has_recent_movement = True
                        break

            # --- State Machine Rules ---
            if age > HOLD or online_flag == 0:
                car_status = "OFFLINE"
            elif current_speed > 0.0 or has_recent_movement:
                car_status = "ACTIVE"  # Covers moving now AND holding through traffic lights/stops
            else:
                car_status = "PARKED"  # True parked/idle state after grace period expires

            # Decode metrics from latest raw hex
            decoded = decode(raw_hex) if raw_hex else {}

            odo = (
                decoded.get("odometer")
                or decoded.get("odo_guess")
                or decoded.get("odo", 0.0)
            )

            battery_pct = decoded.get("battery", 0.0) or 0.0
            battery_kwh = round((battery_pct / 100.0) * 18.3, 2)

            fuel_pct = decoded.get("fuel_pct")
            if fuel_pct is None and raw_hex:
                try:
                    b = bytes.fromhex(raw_hex)
                    if len(b) > 21:
                        fuel_pct = b[21]
                except Exception:
                    fuel_pct = 0.0

            volt12 = decoded.get("volt12", 0.0)

            # Tyre health check
            # --- ENHANCED: Tyre Delta & Health Warning Check ---
            tyre_vals = []
            for k, v in decoded.items():
                if "tyre" in k.lower() or "tire" in k.lower() or k.endswith("_pressure"):
                    if isinstance(v, (int, float)):
                        tyre_vals.append(v)
    
            if tyre_vals and len(tyre_vals) >= 2:
                min_tyre = min(tyre_vals)
                # Create a list of the other tyres (excluding the minimum)
                others = [v for v in tyre_vals if v != min_tyre]
                
                if others:
                    avg_others = sum(others) / len(others)
                    if avg_others > 0:
                        # Calculate percentage drop of the minimum tyre compared to the average of the rest
                        delta_pct = (avg_others - min_tyre) / avg_others * 100
                        
                        if delta_pct > 30:
                            min_tyre = f"ALERT ({int(min_tyre)} - {int(delta_pct)}%)"
                        elif delta_pct > 10:
                            min_tyre = f"Warning ({int(min_tyre)} - {int(delta_pct)}%)"
                        else:
                            min_tyre = f"OK ({int(min_tyre)})"
                    else:
                        min_tyre = f"OK ({int(min_tyre)})"
                else:
                    min_tyre = f"OK ({int(min_tyre)})"
            else:
                min_tyre = tyre_vals[0] if tyre_vals else "OK"

            return {
                "status": "ok",
                "car_status": car_status,
                "odo": float(odo or 0.0),
                "battery": float(battery_pct),
                "battery_kwh": float(battery_kwh),
                "fuel_pct": float(fuel_pct or 0.0),
                "volt12": float(volt12 or 0.0),
                "min_tyre": min_tyre,
                "msg": f"Telemetry active ({car_status})",
            }

        return {
            "status": "empty_db", "car_status": "OFFLINE", "odo": 0.0,
            "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
            "volt12": 0.0, "min_tyre": "--", "msg": "No database found"
        }
    except Exception as e:
        print(f"Telemetry fetch error: {e}")
        return {
            "status": "error", "car_status": "OFFLINE", "odo": 0.0,
            "battery": 0.0, "battery_kwh": 0.0, "fuel_pct": 0.0,
            "volt12": 0.0, "min_tyre": "Error", "msg": str(e)
        }


def calculate_trip_cost(
    distance, start_bat, end_bat, start_fuel, end_fuel, rates
):
  if not distance or distance <= 0:
    return 0.0, 0.0

  bat_drop = max(0.0, (start_bat or 0.0) - (end_bat or 0.0))
  kwh_used = (bat_drop / 100.0) * rates["battery_capacity_kwh"]
  elec_cost = kwh_used * rates["elec_price_per_kwh"]

  fuel_drop = max(0.0, (start_fuel or 0.0) - (end_fuel or 0.0))
  liters_used = (fuel_drop / 100.0) * rates["fuel_tank_capacity_l"]
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
        /* ADD YOUR NEW CSS HERE: */
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
    </style>
</head>
<body>
    <div class="container">
        <div class="header-box">
            <span id="led-status" class="led-indicator led-empty" title="Checking status..."></span>
            <h1>🚗 PHEV Dual-Trip Tracker</h1>
        </div>
        <!-- NEW: Sub-header Status Bar for 12V and Tyre Min -->
        <div class="sub-status-bar">
            <span>⚡ 12V: <strong id="val-volt12">{{ "%.1f"|format(telemetry.volt12) }}V</strong></span>
            <span>|</span>
            <span>🛞 Tyre Min: <strong id="val-tyre">{{ telemetry.min_tyre }}</strong></span>
        </div>
        
        <div class="trips-grid">
            <!-- TRIP A CARD -->
            <div class="card">
                <div class="card-title">Trip A (Short / Leg)</div>
                {% if active_a %}
                    <div class="status-text">In Progress...</div>
                    <div class="stat-value" id="timer-a">-- hr -- min</div>
                    <div class="stat-label">Elapsed Time</div>
                    <div class="stat-value">{{ "%.1f"|format(active_a.live_dist) if active_a.live_dist > 0 else '0.0' }} km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='A') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip A</button>
                    </form>
                    <script>
                        const startA = new Date("{{ active_a.start_time }}").getTime();
                        function updateTripA() {
                            const now = new Date().getTime();
                            const diffMins = Math.floor((now - startA) / 60000);
                            const h = Math.floor(diffMins / 60);
                            const m = diffMins % 60;
                            document.getElementById('timer-a').innerText = `${h} hr ${m} min`;
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
                <div class="card-title">Trip B (Long / Tank)</div>
                {% if active_b %}
                    <div class="status-text">In Progress...</div>
                    <div class="stat-value" id="timer-b">-- hr -- min</div>
                    <div class="stat-label">Elapsed Time</div>
                    <div class="stat-value">{{ "%.1f"|format(active_b.live_dist) if active_b.live_dist > 0 else '0.0' }} km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='B') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip B</button>
                    </form>
                    <script>
                        const startB = new Date("{{ active_b.start_time }}").getTime();
                        function updateTripB() {
                            const now = new Date().getTime();
                            const diffMins = Math.floor((now - startB) / 60000);
                            const h = Math.floor(diffMins / 60);
                            const m = diffMins % 60;
                            document.getElementById('timer-b').innerText = `${h} hr ${m} min`;
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
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Date</th>
                        <th>Dist</th>
                        <th>Bat</th>
                        <th>Fuel</th>
                        <th>Cost/km</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in completed_trips %}
                    <tr>
                        <td>
                            <span class="badge {{ 'badge-a' if item.trip.trip_type == 'A' else 'badge-b' }}">
                                {{ item.trip.trip_type }}
                            </span>
                        </td>
                        <td>{{ item.trip.end_time[:10] }}</td>
                        <td>{{ "%.1f"|format(item.trip.distance or 0) }} km</td>
                        <td>{{ "%.0f"|format(item.trip.start_battery - item.trip.end_battery) }}%</td>
                        <td>{{ "%.1f"|format(item.trip.start_fuel - item.trip.end_fuel) }}%</td>
                        <td class="cost-highlight">RM {{ "%.2f"|format(item.cost_per_km) }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="6" style="text-align: center; color: #666;">No trips logged yet</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Live Telemetry & LED Diagnosis Script -->
    <script>
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

                    // Update Trip A and Trip B live active mileage on the screen
                    if (data.dist_a !== undefined) {
                        const distAEl = document.getElementById('trip-a-mileage-value'); // Make sure this matches your Trip A element ID
                        if (distAEl) {
                            distAEl.innerText = data.dist_a.toFixed(1) + ' km';
                        }
                    }

                    if (data.dist_b !== undefined) {
                        const distBEl = document.getElementById('trip-b-mileage-value'); // Make sure this matches your Trip B element ID
                        if (distBEl) {
                            distBEl.innerText = data.dist_b.toFixed(1) + ' km';
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

        // Run immediately on page load, then poll every 3 to 10 seconds
        checkTelemetryStatus();
        setInterval(checkTelemetryStatus, 10000); // Poll every 3 seconds for smooth live feedback
    </script>
</body>
</html>
"""


@app.route("/")
def index():
  rates = load_rates()
  db = get_db()
  active_a = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'A' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()
  active_b = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'B' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()

  current_telemetry = fetch_car_telemetry()
  current_odo = current_telemetry.get("odo", 0.0)

  live_dist_a = 0.0
  if active_a and current_odo > 0:
    live_dist_a = max(0.0, current_odo - active_a["start_odo"])

  live_dist_b = 0.0
  if active_b and current_odo > 0:
    live_dist_b = max(0.0, current_odo - active_b["start_odo"])

  raw_completed = db.execute(
      "SELECT * FROM trips WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 10"
  ).fetchall()

  completed_trips = []
  for trip in raw_completed:
    total_cost, cost_per_km = calculate_trip_cost(
        trip["distance"],
        trip["start_battery"],
        trip["end_battery"],
        trip["start_fuel"],
        trip["end_fuel"],
        rates,
    )
    completed_trips.append(
        {"trip": trip, "total_cost": total_cost, "cost_per_km": cost_per_km}
    )

  active_a_dict = dict(active_a) if active_a else None
  if active_a_dict:
    active_a_dict["live_dist"] = (
        live_dist_a if live_dist_a > 0 else (current_odo - active_a["start_odo"])
    )

  active_b_dict = dict(active_b) if active_b else None
  if active_b_dict:
    active_b_dict["live_dist"] = (
        live_dist_b if live_dist_b > 0 else (current_odo - active_b["start_odo"])
    )

  return render_template_string(
      HTML_TEMPLATE,
      active_a=active_a_dict,
      active_b=active_b_dict,
      completed_trips=completed_trips,
      rates=rates,
      telemetry=current_telemetry,  # <--- Pass telemetry object here
  )


@app.route("/api/telemetry_status", methods=["GET"])
def telemetry_status_endpoint():
  """JSON endpoint powering the frontend LED indicator and live metrics."""
  db = get_db()
  active_a = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'A' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()
  active_b = db.execute(
      "SELECT * FROM trips WHERE trip_type = 'B' AND status = 'ACTIVE' LIMIT 1"
  ).fetchone()

  data = fetch_car_telemetry()
  current_odo = data.get("odo", 0.0)

  # Calculate live distances for active trips
  live_dist_a = 0.0
  if active_a and current_odo > 0:
    live_dist_a = max(0.0, current_odo - active_a["start_odo"])

  live_dist_b = 0.0
  if active_b and current_odo > 0:
    live_dist_b = max(0.0, current_odo - active_b["start_odo"])

  # Attach computed distances to the JSON dictionary
  data["dist_a"] = live_dist_a
  data["dist_b"] = live_dist_b

  return jsonify(data)


@app.route("/start_trip/<trip_type>", methods=["POST"])
def start_trip(trip_type):
  data = fetch_car_telemetry()
  db = get_db()
  db.execute(
      """
        INSERT INTO trips (trip_type, start_time, start_odo, start_battery, start_fuel, status)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE')
    """,
      [
          trip_type,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          data.get("odometer", 0.0),  # Matches decoder key: "odometer"
          data.get("battery", 0),       # Matches decoder key: "battery"
          data.get("fuel_pct", 0),      # Matches decoder key: "fuel_pct"
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
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_odo = data.get("odometer", 0.0)
    end_odo = current_odo if current_odo > 0 else active_trip["start_odo"]
    distance = end_odo - active_trip["start_odo"]

    db.execute(
        """
            UPDATE trips 
            SET end_time = ?, end_odo = ?, end_battery = ?, end_fuel = ?, distance = ?, status = 'COMPLETED'
            WHERE id = ?
        """,
        [
            end_time,
            end_odo,
            data.get("battery", 0),       # Matches decoder key: "battery"
            data.get("fuel_pct", 0),      # Matches decoder key: "fuel_pct"
            distance,
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

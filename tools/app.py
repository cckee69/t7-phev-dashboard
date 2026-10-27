import json
import os
import sqlite3
import sys
from datetime import datetime
from flask import Flask, g, redirect, render_template_string, request, url_for

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
  import server
except ImportError:
  server = None

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
  try:
    if hasattr(server, "live_poll"):
      server.live_poll()

    db_path = getattr(server, "DB", "carlinko.db")
    if os.path.exists(db_path):
      conn = sqlite3.connect(db_path)
      cursor = conn.cursor()
      cursor.execute(
          "SELECT timestamp, raw FROM telemetry ORDER BY timestamp DESC LIMIT 1"
      )
      row = cursor.fetchone()
      conn.close()

      if row and server:
        decoded = server.decode(row[1])
        return {
            "odo": decoded.get("odometer", 0.0),
            "battery": decoded.get("battery", 0.0),
            "fuel": decoded.get("fuel_pct", 0.0),
        }

    return {"odo": 0.0, "battery": 0.0, "fuel": 0.0}
  except Exception as e:
    print(f"Telemetry fetch error: {e}")
    return {"odo": 0.0, "battery": 0.0, "fuel": 0.0}


def calculate_trip_cost(distance, start_bat, end_bat, start_fuel, end_fuel, rates):
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
        h1 { text-align: center; font-size: 1.4rem; margin-bottom: 16px; color: #fff; }
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
        <h1>🚗 PHEV Dual-Trip Tracker</h1>
        
        <div class="trips-grid">
            <!-- TRIP A CARD -->
            <div class="card">
                <div class="card-title">Trip A (Short / Leg)</div>
                {% if active_a %}
                    <div class="status-text">In Progress...</div>
                    <div class="stat-value" id="timer-a">-- hr -- min</div>
                    <div class="stat-label">Elapsed Time</div>
                    <div class="stat-value" id="dist-a">-- km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='A') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip A</button>
                    </form>
                    <script>
                        const startA = new Date("{{ active_a.start_time }}").getTime();
                        const startOdoA = {{ active_a.start_odo }};
                        function updateTripA() {
                            const now = new Date().getTime();
                            const diffMins = Math.floor((now - startA) / 60000);
                            const h = Math.floor(diffMins / 60);
                            const m = diffMins % 60;
                            document.getElementById('timer-a').innerText = `${h} hr ${m} min`;
                            
                            // Estimate live mileage based on server ODO if updated, else show elapsed time baseline
                            // (Will pull fresh odo from next page refresh or live fetch)
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
                    <div class="stat-value" id="dist-b">-- km</div>
                    <div class="stat-label">Active Mileage</div>
                    <form action="{{ url_for('end_trip', trip_type='B') }}" method="POST">
                        <button type="submit" class="btn btn-end">End Trip B</button>
                    </form>
                    <script>
                        const startB = new Date("{{ active_b.start_time }}").getTime();
                        const startOdoB = {{ active_b.start_odo }};
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

  # Calculate live active mileage if active
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

  # Inject live distance into active objects for template display
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
  )


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
          data["odo"],
          data["battery"],
          data["fuel"],
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
    end_odo = data["odo"] if data["odo"] > 0 else active_trip["start_odo"]
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
            data["battery"],
            data["fuel"],
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

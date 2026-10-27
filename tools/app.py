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

# Default fallback values
DEFAULT_RATES = {
    "fuel_price_per_l": 1.99,
    "elec_price_per_kwh": 0.33,
    "fuel_tank_capacity_l": 60.0,
    "battery_capacity_kwh": 18.3,
}


def load_rates():
  """Loads fuel and electricity rates from rates.json."""
  if os.path.exists(RATES_FILE):
    try:
      with open(RATES_FILE, "r") as f:
        data = json.load(f)
        return {**DEFAULT_RATES, **data}
    except Exception as e:
      print(f"Error loading rates.json: {e}")
  return DEFAULT_RATES.copy()


def save_rates(rates):
  """Saves updated rates to rates.json."""
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
        hex_data = row[1]
        decoded = server.decode(hex_data)
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

  bat_drop_pct = max(0.0, (start_bat or 0.0) - (end_bat or 0.0))
  kwh_used = (bat_drop_pct / 100.0) * rates["battery_capacity_kwh"]
  elec_cost = kwh_used * rates["elec_price_per_kwh"]

  fuel_drop_pct = max(0.0, (start_fuel or 0.0) - (end_fuel or 0.0))
  liters_used = (fuel_drop_pct / 100.0) * rates["fuel_tank_capacity_l"]
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
    <title>PHEV One-Touch Tracker</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; display: flex; justify-content: center; }
        .container { width: 100%; max-width: 520px; }
        h1 { text-align: center; font-size: 1.5rem; margin-bottom: 20px; color: #fff; }
        .card { background: #1e1e1e; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); text-align: center; }
        .btn { display: block; width: 100%; padding: 16px; font-size: 1.1rem; font-weight: bold; border: none; border-radius: 8px; cursor: pointer; text-decoration: none; color: white; }
        .btn-start { background-color: #2e7d32; }
        .btn-end { background-color: #c62828; }
        .btn-save { background-color: #0288d1; margin-top: 10px; padding: 10px; font-size: 0.9rem; }
        .status-text { font-size: 1.1rem; margin-bottom: 16px; color: #b0bec5; }
        .live-stats { display: flex; justify-content: space-around; background: #262626; padding: 12px; border-radius: 8px; margin-bottom: 20px; }
        .stat-box { text-align: center; }
        .stat-value { font-size: 1.3rem; font-weight: bold; color: #4caf50; }
        .stat-label { font-size: 0.8rem; color: #888; text-transform: uppercase; margin-top: 4px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }
        th, td { padding: 10px 6px; text-align: left; border-bottom: 1px solid #2c2c2c; }
        th { color: #888; font-weight: 600; }
        .cost-highlight { color: #4caf50; font-weight: bold; }
        .rates-form { display: flex; gap: 10px; justify-content: space-between; text-align: left; margin-top: 10px; }
        .rates-field { flex: 1; }
        .rates-field label { display: block; font-size: 0.75rem; color: #aaa; margin-bottom: 4px; }
        .rates-field input { width: 90%; padding: 6px; border-radius: 4px; border: 1px solid #333; background: #2a2a2a; color: #fff; font-size: 0.9rem; }
        details { color: #888; font-size: 0.85rem; }
        summary { cursor: pointer; padding: 4px 0; color: #bbb; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚗 PHEV One-Touch Tracker</h1>
        
        <div class="card">
            {% if active_trip %}
                <div class="status-text">Trip in Progress...</div>
                <div class="live-stats">
                    <div class="stat-box">
                        <div class="stat-value" id="timer">00:00:00</div>
                        <div class="stat-label">Elapsed Time</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value" id="live-dist">{{ "%.1f"|format(active_trip.start_odo) }} km</div>
                        <div class="stat-label">Start Odo</div>
                    </div>
                </div>
                <form action="{{ url_for('end_trip') }}" method="POST">
                    <button type="submit" class="btn btn-end">End Journey</button>
                </form>
                <script>
                    const startTime = new Date("{{ active_trip.start_time }}").getTime();
                    function updateTimer() {
                        const now = new Date().getTime();
                        const diffSecs = Math.floor((now - startTime) / 1000);
                        if (diffSecs >= 0) {
                            const hours = String(Math.floor(diffSecs / 3600)).padStart(2, '0');
                            const minutes = String(Math.floor((diffSecs % 3600) / 60)).padStart(2, '0');
                            const seconds = String(diffSecs % 60).padStart(2, '0');
                            document.getElementById('timer').innerText = `${hours}:${minutes}:${seconds}`;
                        }
                    }
                    setInterval(updateTimer, 1000);
                    updateTimer();
                </script>
            {% else %}
                <div class="status-text">Ready for your next drive</div>
                <form action="{{ url_for('start_trip') }}" method="POST">
                    <button type="submit" class="btn btn-start">Start Journey</button>
                </form>
            {% endif %}
        </div>

        <div class="card" style="text-align: left;">
            <details style="margin-bottom: 10px;">
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

            <h3 style="margin-top: 10px; font-size: 1.1rem; color: #fff;">Recent Trips</h3>
            <table>
                <thead>
                    <tr>
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
                        <td>{{ item.trip.end_time[:10] }}</td>
                        <td>{{ "%.1f"|format(item.trip.distance or 0) }} km</td>
                        <td>{{ "%.0f"|format(item.trip.start_battery - item.trip.end_battery) }}%</td>
                        <td>{{ "%.1f"|format(item.trip.start_fuel - item.trip.end_fuel) }}%</td>
                        <td class="cost-highlight">RM {{ "%.2f"|format(item.cost_per_km) }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="5" style="text-align: center; color: #666;">No trips logged yet</td></tr>
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
  active_trip = db.execute(
      "SELECT * FROM trips WHERE status = 'ACTIVE' LIMIT 1"
  ).fetchone()
  raw_completed = db.execute(
      "SELECT * FROM trips WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 5"
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

  return render_template_string(
      HTML_TEMPLATE,
      active_trip=active_trip,
      completed_trips=completed_trips,
      rates=rates,
  )


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


@app.route("/start_trip", methods=["POST"])
def start_trip():
  data = fetch_car_telemetry()
  db = get_db()
  db.execute(
      """
        INSERT INTO trips (start_time, start_odo, start_battery, start_fuel, status)
        VALUES (?, ?, ?, ?, 'ACTIVE')
    """,
      [
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          data["odo"],
          data["battery"],
          data["fuel"],
      ],
  )
  db.commit()
  return redirect(url_for("index"))


@app.route("/end_trip", methods=["POST"])
def end_trip():
  data = fetch_car_telemetry()
  db = get_db()
  active_trip = db.execute(
      "SELECT * FROM trips WHERE status = 'ACTIVE' LIMIT 1"
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


if __name__ == "__main__":
  init_db()
  app.run(host="0.0.0.0", port=8088)

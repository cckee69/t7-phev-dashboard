import sqlite3
import sys
import os
from datetime import datetime
from flask import Flask, g, redirect, render_template_string, request, url_for

# Add current directory to path so we can import from server.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    import server
except ImportError:
    server = None

app = Flask(__name__)
DATABASE = "trips.db"

# Rates for Cost Calculation
FUEL_PRICE_PER_L = 1.99      # RM 1.99 / Liter (RON95)
ELEC_PRICE_PER_KWH = 0.33    # RM 0.33 / kWh (Home Charging Tariff)
FUEL_TANK_CAPACITY_L = 60.0  # Approx usable tank size in Liters
BATTERY_CAPACITY_KWH = 18.3  # Chery Tiggo 7 PHEV battery size in kWh

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
    """Pulls live stats by interacting with your existing server.py / carlinko database."""
    try:
        if hasattr(server, 'live_poll'):
            server.live_poll()

        db_path = getattr(server, 'DB', 'carlinko.db')
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, raw FROM telemetry ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            
            if row and server:
                hex_data = row[1]
                decoded = server.decode(hex_data)
                return {
                    "odo": decoded.get("odometer", 0.0),
                    "battery": decoded.get("battery", 0.0),
                    "fuel": decoded.get("fuel_pct", 0.0)
                }

        return {"odo": 0.0, "battery": 0.0, "fuel": 0.0}
    except Exception as e:
        print(f"Telemetry fetch error: {e}")
        return {"odo": 0.0, "battery": 0.0, "fuel": 0.0}

def calculate_trip_cost(distance, start_bat, end_bat, start_fuel, end_fuel):
    """Calculates Cost/km in RM based on battery and fuel used."""
    if not distance or distance <= 0:
        return 0.0, 0.0

    # Electricity used (kWh) from battery % drop
    bat_drop_pct = max(0.0, (start_bat or 0.0) - (end_bat or 0.0))
    kwh_used = (bat_drop_pct / 100.0) * BATTERY_CAPACITY_KWH
    elec_cost = kwh_used * ELEC_PRICE_PER_KWH

    # Fuel used (L) from fuel tank % drop
    fuel_drop_pct = max(0.0, (start_fuel or 0.0) - (end_fuel or 0.0))
    liters_used = (fuel_drop_pct / 100.0) * FUEL_TANK_CAPACITY_L
    fuel_cost = liters_used * FUEL_PRICE_PER_L

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
        h1 { text-align: center; font-size: 1.5rem; margin-bottom: 24px; color: #fff; }
        .card { background: #1e1e1e; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); text-align: center; }
        .btn { display: block; width: 100%; padding: 18px; font-size: 1.2rem; font-weight: bold; border: none; border-radius: 8px; cursor: pointer; text-decoration: none; color: white; }
        .btn-start { background-color: #2e7d32; }
        .btn-start:hover { background-color: #388e3c; }
        .btn-end { background-color: #c62828; }
        .btn-end:hover { background-color: #d32f2f; }
        .status-text { font-size: 1.1rem; margin-bottom: 16px; color: #b0bec5; }
        .live-stats { display: flex; justify-content: space-around; background: #262626; padding: 12px; border-radius: 8px; margin-bottom: 20px; }
        .stat-box { text-align: center; }
        .stat-value { font-size: 1.3rem; font-weight: bold; color: #4caf50; }
        .stat-label { font-size: 0.8rem; color: #888; text-transform: uppercase; margin-top: 4px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }
        th, td { padding: 10px 6px; text-align: left; border-bottom: 1px solid #2c2c2c; }
        th { color: #888; font-weight: 600; }
        .cost-highlight { color: #4caf50; font-weight: bold; }
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
            <h3 style="margin-top: 0; font-size: 1.1rem; color: #fff;">Recent Trips</h3>
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
    db = get_db()
    active_trip = db.execute("SELECT * FROM trips WHERE status = 'ACTIVE' LIMIT 1").fetchone()
    raw_completed = db.execute("SELECT * FROM trips WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 5").fetchall()
    
    completed_trips = []
    for trip in raw_completed:
        total_cost, cost_per_km = calculate_trip_cost(
            trip['distance'],
            trip['start_battery'],
            trip['end_battery'],
            trip['start_fuel'],
            trip['end_fuel']
        )
        completed_trips.append({
            "trip": trip,
            "total_cost": total_cost,
            "cost_per_km": cost_per_km
        })

    return render_template_string(HTML_TEMPLATE, active_trip=active_trip, completed_trips=completed_trips)

@app.route("/start_trip", methods=["POST"])
def start_trip():
    data = fetch_car_telemetry()
    db = get_db()
    db.execute("""
        INSERT INTO trips (start_time, start_odo, start_battery, start_fuel, status)
        VALUES (?, ?, ?, ?, 'ACTIVE')
    """, [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), data["odo"], data["battery"], data["fuel"]])
    db.commit()
    return redirect(url_for("index"))

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data = fetch_car_telemetry()
    db = get_db()
    active_trip = db.execute("SELECT * FROM trips WHERE status = 'ACTIVE' LIMIT 1").fetchone()
    
    if active_trip:
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        end_odo = data["odo"] if data["odo"] > 0 else active_trip["start_odo"]
        distance = end_odo - active_trip["start_odo"]
        
        db.execute("""
            UPDATE trips 
            SET end_time = ?, end_odo = ?, end_battery = ?, end_fuel = ?, distance = ?, status = 'COMPLETED'
            WHERE id = ?
        """, [end_time, end_odo, data["battery"], data["fuel"], distance, active_trip["id"]])
        db.commit()
        
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8088)

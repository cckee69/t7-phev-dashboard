from flask import Flask, render_template_string, request, redirect, url_for
import sqlite3
from datetime import datetime

app = Flask(__name__)
DB_PATH = "trips.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                start_odo REAL NOT NULL,
                end_odo REAL,
                distance REAL,
                start_soc REAL,
                end_soc REAL,
                soc_consumed REAL,
                start_fuel REAL,
                end_fuel REAL,
                fuel_consumed REAL,
                duration_str TEXT,
                notes TEXT
            )
        """)

@app.route("/")
def index():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Check for ongoing active trip
        cursor.execute("SELECT id, start_time, start_odo, start_soc, start_fuel FROM trips WHERE end_time IS NULL ORDER BY id DESC LIMIT 1")
        active_trip = cursor.fetchone()
        
        # Get history of completed trips
        cursor.execute("""
            SELECT id, start_time, end_time, distance, soc_consumed, fuel_consumed, duration_str, notes 
            FROM trips WHERE end_time IS NOT NULL ORDER BY id DESC LIMIT 10
        """)
        history = cursor.fetchall()

    return render_template_string(HTML_TEMPLATE, active_trip=active_trip, history=history)

@app.route("/start", methods=["POST"])
def start_trip():
    start_odo = float(request.form.get("start_odo", 0))
    start_soc = float(request.form.get("start_soc", 0))
    start_fuel = float(request.form.get("start_fuel", 0))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO trips (start_time, start_odo, start_soc, start_fuel) 
            VALUES (?, ?, ?, ?)
        """, (now, start_odo, start_soc, start_fuel))
    return redirect(url_for("index"))

@app.route("/end/<int:trip_id>", methods=["POST"])
def end_trip(trip_id):
    end_odo = float(request.form.get("end_odo", 0))
    end_soc = float(request.form.get("end_soc", 0))
    end_fuel = float(request.form.get("end_fuel", 0))
    notes = request.form.get("notes", "")
    end_time_dt = datetime.now()
    end_time_str = end_time_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT start_time, start_odo, start_soc, start_fuel FROM trips WHERE id = ?", (trip_id,))
        row = cursor.fetchone()
        if row:
            start_time_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            start_odo = row[1]
            start_soc = row[2]
            start_fuel = row[3]
            
            # Calculations
            distance = round(end_odo - start_odo, 2)
            soc_consumed = round(start_soc - end_soc, 2)
            fuel_consumed = round(end_fuel - end_fuel, 2) if start_fuel == 0 else round(start_fuel - end_fuel, 2)
            
            # Duration calculation
            diff = end_time_dt - start_time_dt
            hours = int(diff.total_seconds() // 3600)
            minutes = int((diff.total_seconds() % 3600) // 60)
            duration_str = f"{hours}h {minutes}m"
            
            conn.execute("""
                UPDATE trips 
                SET end_time = ?, end_odo = ?, distance = ?, 
                    end_soc = ?, soc_consumed = ?, 
                    end_fuel = ?, fuel_consumed = ?, 
                    duration_str = ?, notes = ? 
                WHERE id = ?
            """, (end_time_str, end_odo, distance, end_soc, soc_consumed, end_fuel, fuel_consumed, duration_str, notes, trip_id))
            
    return redirect(url_for("index"))

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>EV & PHEV Manual Trip Logger</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #fff; padding: 15px; max-width: 550px; margin: auto; }
        .card { background: #1e1e1e; padding: 18px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        label { font-size: 12px; color: #aaa; display: block; margin-top: 8px; }
        input, button { width: 100%; padding: 12px; margin-top: 5px; border-radius: 6px; border: none; font-size: 15px; box-sizing: border-box; }
        input { background: #2d2d2d; color: #fff; border: 1px solid #444; }
        button { background: #4CAF50; color: white; font-weight: bold; cursor: pointer; margin-top: 12px; }
        button.end { background: #f44336; }
        table { width: 100%; font-size: 13px; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 8px 4px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #888; font-size: 11px; text-transform: uppercase; }
    </style>
</head>
<body>
    <h2 style="text-align: center; margin-bottom: 20px;">🚗 PHEV Journey Tracker</h2>

    {% if active_trip %}
    <div class="card" style="border: 2px solid #4CAF50;">
        <h3 style="margin-top:0; color: #4CAF50;">Trip in Progress...</h3>
        <p style="font-size: 13px; color: #aaa;">Started at: {{ active_trip[1] }}</p>
        
        <form action="/end/{{ active_trip[0] }}" method="POST">
            <label>End Odometer (km)</label>
            <input type="number" step="0.1" name="end_odo" required placeholder="e.g. 15025.4">
            
            <div class="grid">
                <div>
                    <label>End Battery (%)</label>
                    <input type="number" step="0.1" name="end_soc" required placeholder="e.g. 75">
                </div>
                <div>
                    <label>End Fuel Level (%) or L</label>
                    <input type="number" step="0.1" name="end_fuel" required placeholder="e.g. 45">
                </div>
            </div>

            <label>Notes (Optional)</label>
            <input type="text" name="notes" placeholder="e.g. Balik Pulau drive">
            
            <button type="submit" class="end">End Journey & Calculate</button>
        </form>
    </div>
    {% else %}
    <div class="card">
        <h3 style="margin-top:0;">Start New Trip</h3>
        <form action="/start" method="POST">
            <label>Starting Odometer (km)</label>
            <input type="number" step="0.1" name="start_odo" required placeholder="e.g. 15000.0">
            
            <div class="grid">
                <div>
                    <label>Starting Battery (%)</label>
                    <input type="number" step="0.1" name="start_soc" required placeholder="e.g. 95">
                </div>
                <div>
                    <label>Starting Fuel Level (%) or L</label>
                    <input type="number" step="0.1" name="start_fuel" required placeholder="e.g. 50">
                </div>
            </div>

            <button type="submit">Start Journey</button>
        </form>
    </div>
    {% endif %}

    <div class="card">
        <h3 style="margin-top:0;">Recent Trip Logs</h3>
        <table>
            <tr>
                <th>Date</th>
                <th>Dist</th>
                <th>Bat Use</th>
                <th>Fuel Use</th>
                <th>Time</th>
            </tr>
            {% for trip in history %}
            <tr>
                <td>{{ trip[1].split()[0] }}</td>
                <td><b>{{ trip[3] }} km</b></td>
                <td style="color: #4CAF50;">-{{ trip[4] }}%</td>
                <td style="color: #ff9800;">-{{ trip[5] }}</td>
                <td>{{ trip[6] }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8088)

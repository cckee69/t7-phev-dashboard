"""CarLinko telemetry logger.
Polls the realtime WebSocket (token auth, no signing) and records each frame to SQLite.
Stores the RAW blob every poll so fields decoded later (odometer, charge flag, tyres) can
be back-filled from history.

Usage:
  python logger.py            # single poll (good for Windows Task Scheduler)
  python logger.py --loop 600 # poll every 600s forever (Ctrl-C to stop)
  python logger.py --adaptive # fast when awake, slow when parked
  python logger.py --stream   # persistent WS socket, push-driven (recommended)
"""
import sys, json, time, sqlite3, argparse, os, socket
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import websocket  # websocket-client

# Force IPv4: some hosts resolve an AAAA record and hang on the WS handshake.
_orig_gai = socket.getaddrinfo
def _gai_v4(host, port, family=0, *a, **k):
    res = _orig_gai(host, port, socket.AF_INET, *a, **k)
    return res or _orig_gai(host, port, family, *a, **k)
socket.getaddrinfo = _gai_v4

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.environ.get("CARLINKO_DATA") or _HERE   # Docker data dir; else alongside the code
def _cfg():
    try:
        return json.load(open(os.path.join(_DATA, "creds.json")))
    except Exception:
        return {}
_C = _cfg()
WS_URL  = f"ws://wss-cqr-{_C.get('region','sea')}.hzhjcl.com:4002/"
# Token comes from token.txt (auto-refreshed by auth.login() on expiry); vehicle id + device SN from creds.json.
_TOKEN_FILE = os.path.join(_DATA, "token.txt")
TOKEN   = open(_TOKEN_FILE).read().strip() if os.path.exists(_TOKEN_FILE) else ""
VEHICLE = str(_C.get("vehicle_id") or "")
SN      = _C.get("device_sn") or ""
DB      = os.path.join(_DATA, "carlinko.db") if os.environ.get("CARLINKO_DATA") else os.path.join(_HERE, "..", "carlinko.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
  ts        INTEGER PRIMARY KEY,   -- unix seconds
  dt        TEXT,                  -- local ISO time
  battery   INTEGER,
  range_km  INTEGER,
  odo_guess INTEGER,               -- candidate odometer (bytes 12-13), validate by driving
  tyre_raw  TEXT,                  -- 8 bytes hex (4 psi + 4 temp), FF=invalid
  online    INTEGER,               -- 1 if a fresh blob arrived
  raw       TEXT                   -- full blob hex (for back-decoding)
);

-- Separate tracking tables for external charging and refueling events
CREATE TABLE IF NOT EXISTS charging_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  kwh_added REAL,
);

CREATE TABLE IF NOT EXISTS fuel_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  liters_added REAL,
);
"""

def decode(hexstr):
    b = bytes.fromhex(hexstr)
    d = {"raw": hexstr}
    if len(b) > 30:
        d["battery"]  = b[28]
        d["range_km"] = int.from_bytes(b[29:31], "big")
        d["odo_guess"] = int.from_bytes(b[18:21], "big")  # validated =882 (0x0372)
        d["unlocked"] = b[3] != 0                       # 0=locked (see docs/api-map.md -- verified #5)
        d["speed"]    = int.from_bytes(b[14:16], "big") / 16.0
        # If your payload has fuel_pct mapped elsewhere, ensure it's extracted here. 
        # (Assuming byte 32 or similar based on your previous logs; adjust if your map differs)
        d["fuel_pct"] = b[32] if len(b) > 32 else None 
    if len(b) >= 52:
        d["tyre_raw"] = b[44:52].hex()
    return d

def connect(attempts=3):
    last = None
    for i in range(attempts):
        try:
            return websocket.create_connection(
                WS_URL, timeout=20, suppress_origin=True,
                header=["User-Agent: Dart/3.10 (dart:io)"])
        except Exception as e:
            last = e
            time.sleep(2 + i * 2)
    raise last

def reload_config():
    """Re-read creds.json + token.txt so a fresh login is picked up without restarting."""
    global TOKEN, VEHICLE, SN, WS_URL, _C
    _C = _cfg()
    VEHICLE = str(_C.get("vehicle_id") or "") or VEHICLE
    SN = _C.get("device_sn") or SN
    WS_URL = f"ws://wss-cqr-{_C.get('region','sea')}.hzhjcl.com:4002/"
    if os.path.exists(_TOKEN_FILE):
        TOKEN = open(_TOKEN_FILE).read().strip() or TOKEN

def check_parking_deltas(conn, d, ts):
    cursor = conn.cursor()
    
    # Fetch the last valid recorded battery and fuel state from telemetry
    # Note: Ensure your decoder extracts 'fuel_pct' (or adjust the key to match your payload decoder)
    cursor.execute("SELECT battery, fuel_pct FROM telemetry WHERE online = 1 ORDER BY ts DESC LIMIT 1;")
    last_row = cursor.fetchone()
    
    curr_battery = d.get("battery")
    curr_fuel = d.get("fuel_pct")
    
    event_detected = False

    if last_row:
        last_battery, last_fuel = last_row
        
        # 1. Check for stationary Battery increase (External Charging)
        if last_battery is not None and curr_battery is not None:
            bat_diff = curr_battery - last_battery
            if bat_diff > 0:
                # 18.3 kWh total capacity / 100 = 0.183 kWh per 1%
                est_kwh = bat_diff * 0.183  
                cursor.execute(
                    "INSERT INTO charging_logs (ts, kwh_added) VALUES (?, ?);",
                    (ts, est_kwh)
                )
                print(f"[Auto-Log] Charging detected: +{bat_diff}% battery jump (Est. {est_kwh:.2f} kWh)")
                event_detected = True

        # 2. Check for stationary Fuel increase (Refueling)
        if last_fuel is not None and curr_fuel is not None:
            fuel_diff = curr_fuel - last_fuel
            if fuel_diff > 1:  # Threshold to filter out minor sloshing/sensor noise
                # 60 Liters total capacity / 100 = 0.6 Liters per 1%
                est_liters = fuel_diff * 0.6  
                cursor.execute(
                    "INSERT INTO fuel_logs (ts, liters_added) VALUES (?, ?);",
                    (ts, est_liters)
                )
                print(f"[Auto-Log] Refuel detected: +{fuel_diff}% fuel jump (Est. {est_liters:.1f} Liters)")
                event_detected = True

    conn.commit()
    return event_detected

def poll_once(conn, _retried=False):
    global TOKEN
    if not _retried:
        reload_config()
        conn.executescript(SCHEMA)
    ws = connect()
    try:
        ws.send(json.dumps({"action": 1, "data": {"token": TOKEN, "vehicleId": VEHICLE}}))
        login = json.loads(ws.recv())
        if login.get("code") != "0000":
            try: ws.close()
            except Exception: pass
            if not _retried:
                print(f"token invalid ({login.get('code')}); self-logging in...")
                try:
                    import auth
                    TOKEN = auth.login()
                    print("got fresh token, retrying poll")
                    return poll_once(conn, _retried=True)
                except Exception as e:
                    print("self-login FAILED:", e)
                    return None
            return None
        ws.send(json.dumps({"action": 6}))
        ws.send(json.dumps({"action": 0, "data": {"sn": SN}}))
        blob = None
        ws.settimeout(8)
        t_end = time.time() + 10
        while time.time() < t_end:
            try:
                j = json.loads(ws.recv())
            except Exception:
                break
            if j.get("action") == 6 and isinstance(j.get("data"), str):
                blob = j["data"]; break
    finally:
        ws.close()

    ts = int(time.time())
    dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    if blob:
        d = decode(blob)
        check_parking_deltas(conn, d)
        conn.execute(
            "INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?)",
            (ts, dt, d.get("battery"), d.get("range_km"), d.get("odo_guess"),
             d.get("tyre_raw"), 1, blob))
        conn.commit()
        print(f"{dt}  battery={d.get('battery')}%  range={d.get('range_km')}km  "
              f"odo?={d.get('odo_guess')}  spd={d.get('speed')}  unl={d.get('unlocked')}")
        return d
    conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?)",
                 (ts, dt, None, None, None, None, 0, None))
    conn.commit()
    print(f"{dt}  (no blob — car offline/basement)")
    return None

ACTIVE = int(_C.get("poll_active")  or 5)
PARK   = int(_C.get("poll_parked")  or 30)
OFFLINE_SLOW = int(_C.get("poll_offline") or 900)
HOLD = 600
OFFLINE_AFTER = 3

def adaptive_loop(conn):
    print(f"adaptive logging to {os.path.abspath(DB)}")
    active_until = 0.0
    miss = 0    
    while True:
        # 1. Poll the car
        d = poll_once(conn)  
        now = time.time()  
        if d:
            # Successfully connected — reset miss counter
            miss = 0
            speed = d.get("speed", 0)          
            if speed > 0:
                # Car is moving: extend the active grace period (e.g., keep active for 60s after stopping)
                active_until = now + 60.0
                delay = DELAY_ACTIVE
            else:
                # Car speed is 0, but check if we are still within the active grace period
                if now < active_until:
                    delay = DELAY_ACTIVE  # Still in grace period (e.g., stopped at a light)
                else:
                    delay = DELAY_PARKED  # Truly parked for a while
        else:
            # Poll failed (car offline / basement)
            miss += 1
            # Optional: Back off sleep time if missing for a long time
            delay = DELAY_PARKED * 2 if miss > 10 else DELAY_PARKED

        time.sleep(delay)

HEARTBEAT       = 5
STREAM_BACKSTOP = int(_C.get("stream_backstop") or 20)
TOUCH           = 30
RECONNECT_WAIT  = 3

def _store_blob(conn, blob):
    ts = int(time.time()); dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    d = decode(blob)
    
    # Run the parking delta checker before writing
    check_parking_deltas(conn, d)
    
    conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?)",
                 (ts, dt, d.get("battery"), d.get("range_km"), d.get("odo_guess"),
                  d.get("tyre_raw"), 1, blob))
    conn.commit()
    return d

def _stream_session(conn):
    global TOKEN
    reload_config()
    ws = connect()
    try:
        ws.send(json.dumps({"action": 1, "data": {"token": TOKEN, "vehicleId": VEHICLE}}))
        login = json.loads(ws.recv())
        if login.get("code") != "0000":
            print(f"token invalid ({login.get('code')}); self-logging in...")
            import auth
            TOKEN = auth.login()
            ws.send(json.dumps({"action": 1, "data": {"token": TOKEN, "vehicleId": VEHICLE}}))
            login = json.loads(ws.recv())
            if login.get("code") != "0000":
                print("LOGIN FAILED after refresh:", login); return
        ws.send(json.dumps({"action": 6}))
        ws.send(json.dumps({"action": 0, "data": {"sn": SN}}))
        ws.settimeout(2)
        last_hb = last_req = last_store = time.time()
        last_blob = None
        while True:
            now = time.time()
            if now - last_hb >= HEARTBEAT:
                ws.send(json.dumps({"action": 0, "data": {"sn": SN}})); last_hb = now
            if now - last_req >= STREAM_BACKSTOP:
                ws.send(json.dumps({"action": 6})); last_req = now
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                if last_blob and time.time() - last_store >= TOUCH:
                    _store_blob(conn, last_blob); last_store = time.time()
                continue
            try:
                j = json.loads(msg)
            except Exception:
                continue
            if j.get("action") == 6 and isinstance(j.get("data"), str):
                blob = j["data"]
                changed = blob != last_blob
                
                # Optimization: If parked and blob is identical, skip DB write unless TOUCH timer is hit
                # But if speed > 0 (moving), always write.
                d = decode(blob)
                speed = d.get("speed", 0.0) or 0.0
                is_moving = speed > 0.0

                if is_moving or changed or (time.time() - last_store >= TOUCH):
                    _store_blob(conn, blob)
                    last_store = time.time()
                    print(f"{time.strftime('%H:%M:%S')}  batt={d.get('battery')}%  "
                          f"range={d.get('range_km')}km  spd={speed}  "
                          f"{'moving' if is_moving else ('push' if changed else 'touch')}")
                last_blob = blob
    finally:
        try: ws.close()
        except Exception: pass

def stream_loop(conn):
    reload_config()
    conn.executescript(SCHEMA)
    print(f"streaming to {os.path.abspath(DB)} (push + {HEARTBEAT}s heartbeat, auto-reconnect)")
    while True:
        try:
            _stream_session(conn)
        except Exception as e:
            print("stream error, reconnecting:", repr(e))
        time.sleep(RECONNECT_WAIT)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="fixed seconds between polls; 0 = single")
    ap.add_argument("--adaptive", action="store_true", help="fast when awake, slow when parked")
    ap.add_argument("--stream", action="store_true", help="persistent WS socket, push-driven (recommended)")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    if args.stream:
        stream_loop(conn)
    elif args.adaptive:
        adaptive_loop(conn)
    elif args.loop <= 0:
        poll_once(conn); conn.commit()
    else:
        print(f"logging every {args.loop}s to {os.path.abspath(DB)} (Ctrl-C to stop)")
        while True:
            try:
                poll_once(conn); conn.commit()
            for _ in range(args.loop): time.sleep(1)
            except Exception as e:
                print("poll error:", repr(e))
            time.sleep(args.loop)

if __name__ == "__main__":
    main()

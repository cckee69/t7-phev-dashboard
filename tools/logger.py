"""CarLinko telemetry logger.
Polls the realtime WebSocket (token auth, no signing) and records each frame to SQLite.
Stores clean telemetry columns directly into carlinko.db (no raw blob hex parsing needed in Flask).

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
    ts        INTEGER PRIMARY KEY,
    dt        TEXT,
    battery   INTEGER,
    fuel_pct  INTEGER,
    odo_guess INTEGER,
    tyre_raw  TEXT,
    online    INTEGER,
    volt12    REAL,
    speed     REAL
);

CREATE TABLE IF NOT EXISTS charging_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    kwh_added REAL
);

CREATE TABLE IF NOT EXISTS fuel_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    liters_added REAL
);
"""

def evaluate_tyre_status(decoded):
    """Evaluate tyre pressures and compute delta drops against the group average."""
    tyre_vals = []
    for k, v in decoded.items():
        if "tyre" in k.lower() or "tire" in k.lower() or k.endswith("_pressure"):
            if isinstance(v, (int, float)):
                tyre_vals.append(v)
    
    if tyre_vals and len(tyre_vals) >= 2:
        min_tyre = min(tyre_vals)
        others = [v for v in tyre_vals if v != min_tyre]
        
        if others:
            avg_others = sum(others) / len(others)
            if avg_others > 0:
                delta_pct = (avg_others - min_tyre) / avg_others * 100
                if delta_pct > 30:
                    return f"ALERT ({int(min_tyre)} - {int(delta_pct)}%)"
                elif delta_pct > 10:
                    return f"Warning ({int(min_tyre)} - {int(delta_pct)}%)"
                else:
                    return f"OK ({int(min_tyre)})"
        return f"OK ({int(min_tyre)})"
    elif tyre_vals:
        return f"OK ({int(tyre_vals[0])})"
    
    return "--"

def decode(hexstr):
    b = bytes.fromhex(hexstr)
    d = {"raw": hexstr}
    
    if len(b) > 30:
        d["battery"]   = b[28]
        d["range_km"]  = int.from_bytes(b[29:31], "big")
        d["odo_guess"] = int.from_bytes(b[18:21], "big")
        d["unlocked"]  = b[3] != 0
        d["speed"]     = int.from_bytes(b[14:16], "big") / 16.0
        
        # 12V battery voltage mapped from bytes 12-13 (big endian, 0.1V scale)
        d["volt12"]    = int.from_bytes(b[12:14], "big") / 100.0

    if len(b) >= 22:
        d["fuel_pct"]  = b[21]


    # Unpack individual 1-byte tyre pressures from bytes 44-48
    if len(b) >= 48:
        fl = b[44]
        fr = b[45]
        rl = b[46]
        rr = b[47]
        
        # Filter out invalid bytes (0xFF means no sensor data / uninitialized)
        tyre_vals = [val for val in [fl, fr, rl, rr] if val != 0xFF]
        
        if tyre_vals:
            d["fl_pressure"] = fl if fl != 0xFF else 0
            d["fr_pressure"] = fr if fr != 0xFF else 0
            d["rl_pressure"] = rl if rl != 0xFF else 0
            d["rr_pressure"] = rr if rr != 0xFF else 0
            d["tyre_raw"] = evaluate_tyre_status(d)
        else:
            d["tyre_raw"] = "--"
    else:
        d["tyre_raw"] = "--"

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
    """Re-read creds.json + token.txt so a fresh login (e.g. via the web login page) is picked
    up without restarting the process."""
    global TOKEN, VEHICLE, SN, WS_URL, _C
    _C = _cfg()
    VEHICLE = str(_C.get("vehicle_id") or "") or VEHICLE
    SN = _C.get("device_sn") or SN
    WS_URL = f"ws://wss-cqr-{_C.get('region','sea')}.hzhjcl.com:4002/"
    if os.path.exists(_TOKEN_FILE):
        TOKEN = open(_TOKEN_FILE).read().strip() or TOKEN

def poll_once(conn, _retried=False):
    global TOKEN
    if not _retried:
        reload_config()
        conn.executescript(SCHEMA)          # idempotent: ensure the tables exist for any caller
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
            print("LOGIN FAILED after refresh:", login)
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
        # Determine online flag state (1 = active if speed > 0 or unlocked, 2 = parked if stationary, 0 = offline)
        speed = d.get("speed", 0.0) or 0.0
        unlocked = d.get("unlocked", False)
        online_flag = 1 if (speed > 0.0 ) else 2

        conn.execute(
            "INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, dt, d.get("battery"), d.get("fuel_pct"), d.get("odo_guess"),
             d.get("tyre_raw"), online_flag, d.get("volt12", 0.0), speed))
        print(f"{dt}  battery={d.get('battery')}%  fuel={d.get('fuel_pct')}%  range={d.get('range_km')}km  "
              f"odo?={d.get('odo_guess')}  spd={speed}  tyre={d.get('tyre_raw')}  unl={unlocked}")
        return d
    
    # Empty blob / offline state (online = 0)
    conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?,?)",
                 (ts, dt, None, None, None, None, 0, None, None))
    print(f"{dt}  (no blob — car offline/basement)")
    return None

# adaptive cadence (seconds)
ACTIVE = int(_C.get("poll_active")  or 30)
PARK   = int(_C.get("poll_parked")  or 120)
OFFLINE_SLOW = int(_C.get("poll_offline") or 900)
HOLD = 600
OFFLINE_AFTER = 3

def adaptive_loop(conn):
    print(f"adaptive logging to {os.path.abspath(DB)}")
    active_until = 0.0
    miss = 0
    
    while True:
        try:
            st = poll_once(conn)
            conn.commit()
        except Exception as e:
            print("poll error:", repr(e))
            st = None
            
        now = time.time()
        
        if st:
            miss = 0
            speed = st.get("speed", 0.0) or 0.0
            battery = st.get("battery")          
            is_moving = speed > 0.0
            
            if is_moving:
                delay = ACTIVE
                active_until = now + HOLD
            else:
                delay = PARK
        else:
            miss += 1
            if miss >= OFFLINE_AFTER:
                delay = OFFLINE_SLOW
            else:
                delay = PARK
                
        time.sleep(delay)

HEARTBEAT       = 5
STREAM_BACKSTOP = int(_C.get("stream_backstop") or 20)
TOUCH           = 30
RECONNECT_WAIT  = 3

def _store_blob(conn, blob):
    ts = int(time.time()); dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    d = decode(blob)
    speed = d.get("speed", 0.0) or 0.0
    unlocked = d.get("unlocked", False)
    online_flag = 1 if (speed > 0.0 or unlocked) else 2

    conn.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?,?,?,?,?)",
                 (ts, dt, d.get("battery"), d.get("fuel_pct"), d.get("odo_guess"),
                  d.get("tyre_raw"), online_flag, d.get("volt12", 0.0), speed))
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
                if changed or time.time() - last_store >= TOUCH:
                    d = _store_blob(conn, blob); last_store = time.time()
                    print(f"{time.strftime('%H:%M:%S')}  batt={d.get('battery')}%  "
                          f"range={d.get('range_km')}km  odo?={d.get('odo_guess')}  "
                          f"{'push' if changed else 'touch'}")
                last_blob = blob
    finally:
        try: ws.close()
        except Exception: pass

def stream_loop(conn):
    reload_config()
    conn.executescript(SCHEMA)
    print(f"streaming to {os.path.abspath(DB)}  (push + {HEARTBEAT}s heartbeat, auto-reconnect)")
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
        print(f"logging every {args.loop}s to {os.path.abspath(DB)}  (Ctrl-C to stop)")
        while True:
            try:
                poll_once(conn); conn.commit()
            except Exception as e:
                print("poll error:", repr(e))
            time.sleep(args.loop)

if __name__ == "__main__":
    main()

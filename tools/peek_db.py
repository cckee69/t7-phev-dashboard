import os
import sqlite3
import sys
from datetime import datetime

# Add current directory to path so we can try importing server settings
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    import server
except ImportError:
    server = None


def peek_telemetry(limit=10):
    db_path = getattr(server, "DB", "carlinko.db")

    if not os.path.exists(db_path):
        print(f"❌ Error: Database file '{db_path}' not found.")
        return

    print(f"🔍 Inspecting last {limit} rows from: {db_path}\n" + "=" * 80)

    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cursor = conn.cursor()

        # Fetch table column names
        cursor.execute("PRAGMA table_info(telemetry)")
        columns = [col[1] for col in cursor.fetchall()]

        # Fetch last N rows ordered by timestamp descending
        cursor.execute(f"SELECT * FROM telemetry ORDER BY ts DESC LIMIT {limit}")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("⚠️ The 'telemetry' table is empty.")
            return

        for i, row in enumerate(rows, 1):
            row_dict = dict(zip(columns, row))
            ts_val = row_dict.get("ts", "Unknown Time")
            # Safely handle both epoch integers/floats and pre-formatted text strings
            if isinstance(ts_val, (int, float)):
                try:
                    readable_ts = datetime.fromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    readable_ts = str(ts_val)
            else:
                readable_ts = str(ts_val)
            raw_hex = row_dict.get("raw", row_dict.get("hex", row[7] if len(row) > 7 else "N/A"))

            print(f"[{i}] Timestamp: {readable_ts}")
            print(f"    Raw Hex: {raw_hex}")

            # Try decoding if server.decode is available
            if server and hasattr(server, "decode") and raw_hex:
                try:
                    decoded = server.decode(raw_hex)
                    # Filter out empty or zero values for cleaner printing
                    meaningful = {k: v for k, v in decoded.items() if v}
                    print(f"    Decoded: {meaningful}")
                except Exception as ex:
                    print(f"    Decoded: (Error parsing hex: {ex})")
            
            print("-" * 80)

    except Exception as e:
        print(f"❌ Database error: {e}")


if __name__ == "__main__":
    limit = 10
    if len(sys.argv) > 1:
        try:
            # Handles both positive and negative inputs (e.g. 10 or -10)
            limit = abs(int(sys.argv[1]))
        except ValueError:
            print(f"⚠️ Invalid argument '{sys.argv[1]}'. Defaulting to 10 rows.")
            
    peek_telemetry(limit)

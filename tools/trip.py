import argparse
import sqlite3

def view_trips(limit):
    conn = sqlite3.connect('trips.db')
    cursor = conn.cursor()

    # Fetch records with the specified limit
    cursor.execute("SELECT * FROM trips ORDER BY id DESC LIMIT ?;", (limit,))
    rows = cursor.fetchall()

    print(f"--- Latest {limit} Trip Record(s) ---")
    print(f"id|trip:start_time|end_time:start_odometer|end_odometer|battery_start|battery_end|fuel_start|fuel_end|distance")
    if not rows:
        print("No trip records found.")
    else:
        for row in rows:
            print(row)

    conn.close()

def delete_trip(trip_id=None):
    conn = sqlite3.connect('trips.db')
    cursor = conn.cursor()
    
    if trip_id is not None:
        cursor.execute("DELETE FROM trips WHERE id = ?;", (trip_id,))
        print(f"Deleted trip record with ID: {trip_id}")
    else:
        # Delete the most recent record (highest ID)
        cursor.execute("DELETE FROM trips WHERE id = (SELECT MAX(id) FROM trips);")
        print("Deleted the latest trip record.")
        
    conn.commit()
    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Inspect CarLinko trip records from trips.db")
    
    # Arguments
    parser.add_argument('limit', nargs='?', type=int, default=1, help='Number of records to print (default: 1)')
    parser.add_argument('--del-last', action='store_true', help='Delete the most recent trip record')
    parser.add_argument('--del-id', type=int, help='Delete a specific trip record by ID')
    
    args = parser.parse_args()
    
    if args.del_last:
        delete_trip(None)
    elif args.del_id is not None:
        delete_trip(args.del_id)
    else:
        view_trips(args.limit)

# python trip.py --del-id 5
# python trip.py --del-last
# python trip.py 5

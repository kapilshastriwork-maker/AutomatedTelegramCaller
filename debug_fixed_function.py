import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Debug the FIXED function with a six minute old row
# Use the same format as what insert_call actually stores (UTC time in SQLite format)
test_run_id = "debug_fixed_test"
test_chat = 99017
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))

# Get current UTC time and subtract 6 minutes to simulate a 6-minute-old row
now_utc = datetime.utcnow()
six_min_ago_utc = now_utc - timedelta(minutes=6)
# Format as SQLite UTC time string (what insert_call would store)
six_min_ago_utc_str = six_min_ago_utc.strftime("%Y-%m-%d %H:%M:%S")
print("Inserting 6-min-ago UTC time:", six_min_ago_utc_str)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_debug_fixed",
        test_run_id,
        "Debug Fixed Clinic",
        "+919876543210",
        six_min_ago_utc_str,
    ),
)
conn.commit()
conn.close()

# Debug the FIXED SQL
print("\n--- Debugging FIXED get_stuck_running_calls ---")
conn = db._connect()
cursor = conn.execute(
    """
    SELECT 
        created_at,
        datetime(created_at) as created_dt,
        datetime('now') as now_dt,
        datetime('now', ?) as threshold_dt,
        datetime(created_at) < datetime('now', ?) as is_stuck
    FROM calls WHERE run_id = ?
""",
    ("-300 seconds", "-300 seconds", test_run_id),
)
row = cursor.fetchone()
if row:
    created_at, created_dt, now_dt, threshold_dt, is_stuck = row
    print(f"Stored created_at: {created_at}")
    print(f"datetime(created_at): {created_dt}")
    print(f'datetime("now"): {now_dt}')
    print(f'datetime("now", "-300 seconds"): {threshold_dt}')
    print(f"IS_STUCK (created < now-5min): {is_stuck}")

    # Also let's calculate what the actual age should be
    row2 = conn.execute(
        "SELECT created_at FROM calls WHERE run_id=?", (test_run_id,)
    ).fetchone()
    if row2:
        actual_stored = row2["created_at"]
        print(f"Actually stored in DB: {actual_stored}")

        # Convert to datetime for comparison
        from datetime import datetime

        stored_utc = datetime.strptime(actual_stored, "%Y-%m-%d %H:%M:%S")
        now_utc = datetime.utcnow()
        age_seconds = (now_utc - stored_utc).total_seconds()
        print(f"Stored as UTC time: {stored_utc}")
        print(f"Current UTC time: {now_utc}")
        print(f"Actual age in seconds: {age_seconds}")
        print(f"Should be stuck? (age > 300): {age_seconds > 300}")

conn.close()

import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime

# Test the FIXED function with a fresh insert (should NOT be stuck)
test_run_id = "debug_fixed_fresh"
test_chat = 99018
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))

# Insert a fresh row using insert_call (which uses the default UTC time)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, patient_name) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_debug_fixed_fresh",
        test_run_id,
        "Debug Fixed Fresh Clinic",
        "+919876543210",
        "Test Patient",
    ),
)
conn.commit()
conn.close()

# Debug the FIXED SQL for fresh insert
print("\n--- Debugging FIXED get_stuck_running_calls for FRESH insert ---")
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

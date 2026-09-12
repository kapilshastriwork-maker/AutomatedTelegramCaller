import sys

sys.path.insert(0, ".")
from scripts.test_w1_call import test_safety_net_six_minute_old_row_is_matched
from app import db
from datetime import datetime, timedelta

print("=== Debugging six minute old row test ===")

# Replicate what the test does
test_run_id = "six_min_old_test"
test_chat = 99003
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
# Insert row created 6 minutes ago
six_min_ago = (datetime.now() - timedelta(minutes=6)).isoformat(timespec="seconds")
print(f"Inserting six_min_ago (ISO format): {six_min_ago}")
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    "VALUES (?, ?, ?, 'running', ?, ?, ?)",
    (
        test_chat,
        "plan_six_min",
        test_run_id,
        "Six Minute Old Clinic",
        "+919876543210",
        six_min_ago,
    ),
)
conn.commit()
conn.close()

# Check what's actually stored
conn = db._connect()
row = conn.execute(
    "SELECT created_at FROM calls WHERE run_id=?", (test_run_id,)
).fetchone()
if row:
    actual_stored = row["created_at"]
    print(f"Actually stored in DB: {actual_stored}")

    # Test what our fixed function thinks
    stuck = db.get_stuck_running_calls(300)
    print(f"get_stuck_running_calls(300) returned {len(stuck)} rows")
    if stuck:
        for srow in stuck:
            print(f"  Stuck row: {srow}")
            if srow["run_id"] == test_run_id:
                print("  -> The test row IS in the stuck list")
            else:
                print("  -> Some other row is stuck")
    else:
        print("  -> No rows are stuck")

# Let's also test what the comparison logic computes for this specific row
print("\n--- Debugging the comparison ---")
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

    # Calculate actual age
    from datetime import datetime

    try:
        # Try parsing as SQLite format first
        stored_utc = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Try parsing as ISO format
        stored_utc = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S")
    now_utc = datetime.utcnow()
    age_seconds = (now_utc - stored_utc).total_seconds()
    print(f"Parsed stored time as UTC: {stored_utc}")
    print(f"Current UTC time: {now_utc}")
    print(f"Actual age in seconds: {age_seconds}")
    print(f"Should be stuck? (age > 300): {age_seconds > 300}")

conn.close()

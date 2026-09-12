import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Insert a six minute old row and test the fix approach
test_run_id = "debug_fix_test"
test_chat = 99011
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
six_min_ago_local = (datetime.now() - timedelta(minutes=6)).isoformat(
    timespec="seconds"
)
print("Inserting 6-min-ago local time:", six_min_ago_local)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_debug_fix",
        test_run_id,
        "Debug Fix Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()

# Test the approach: replace T with space and use datetime()
print("\n--- Testing the fix approach ---")
cursor = conn.execute(
    """
    SELECT 
        created_at,
        REPLACE(created_at, 'T', ' ') as created_at_sqlite_format,
        datetime(REPLACE(created_at, 'T', ' ')) as created_at_datetime,
        datetime('now') as now_datetime,
        datetime(REPLACE(created_at, 'T', ' ')) < datetime('now', ?) as is_stuck
    FROM calls WHERE run_id = ?
""",
    ("-300 seconds", test_run_id),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print("created_at_sqlite_format:", row["created_at_sqlite_format"])
    print("created_at_datetime:", row["created_at_datetime"])
    print("now_datetime:", row["now_datetime"])
    print("IS_STUCK (created < now-5min):", row["is_stuck"])

# Now let's test the actual function with this approach
print("\n--- Testing get_stuck_running_calls with the fix ---")
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        print(f"  Stuck row: {row}")

conn.close()

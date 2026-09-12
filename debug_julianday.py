import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Insert a six minute old row and debug the SQL with correct Unix timestamp conversion
test_run_id = "debug_jd_test"
test_chat = 99010
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
        "plan_debug_jd",
        test_run_id,
        "Debug JD Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()

# Let's debug the SQL with CORRECT Unix timestamp conversion
print("\n--- Debugging the SQL with CORRECT Unix timestamp ---")

# First, let's see what raw data we have
cursor = conn.execute(
    """
    SELECT 
        created_at,
        (julianday(created_at) - 2440587.5) * 86400.0 as unix_ts_created,
        (julianday('now') - 2440587.5) * 86400.0 as unix_ts_now,
        ((julianday('now') - 2440587.5) * 86400.0 - 300) as unix_ts_threshold
    FROM calls WHERE run_id = ?
""",
    (test_run_id,),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print("unix_ts_created:", row["unix_ts_created"])
    print("unix_ts_now:", row["unix_ts_now"])
    print("unix_ts_threshold:", row["unix_ts_threshold"])
    print(
        "unix_ts_created < unix_ts_threshold?",
        row["unix_ts_created"] < row["unix_ts_threshold"],
    )
    print("Age (now - created):", row["unix_ts_now"] - row["unix_ts_created"])

# Now let's test the actual function
print("\n--- Testing get_stuck_running_calls ---")
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        print(f"  Stuck row: {row}")

conn.close()

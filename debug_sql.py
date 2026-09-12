import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Insert a six minute old row and debug the SQL
test_run_id = "debug_sql_test"
test_chat = 99009
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
        "plan_debug_sql",
        test_run_id,
        "Debug SQL Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()

# Let's debug the SQL step by step
print("\n--- Debugging the SQL ---")

# First, let's see what raw data we have
cursor = conn.execute(
    """
    SELECT 
        created_at,
        julianday(created_at) as jd_created,
        (julianday(created_at) - julianday('1840-01-01')) * 86400.0 as ts_created,
        julianday('now') as jd_now,
        (julianday('now') - julianday('1840-01-01')) * 86400.0 as ts_now,
        ((julianday('now') - julianday('1840-01-01')) * 86400.0 - 300) as ts_threshold
    FROM calls WHERE run_id = ?
""",
    (test_run_id,),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print("jd_created:", row["jd_created"])
    print("ts_created:", row["ts_created"])
    print("jd_now:", row["jd_now"])
    print("ts_now:", row["ts_now"])
    print("ts_threshold:", row["ts_threshold"])
    print("ts_created < ts_threshold?", row["ts_created"] < row["ts_threshold"])

# Now let's test the actual function
print("\n--- Testing get_stuck_running_calls ---")
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        print(f"  Stuck row: {row}")

conn.close()

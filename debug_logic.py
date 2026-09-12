import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Simulate what happens in the test: inserting local time string
test_run_id = "debug_test"
test_chat = 99006
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))

# Insert a time that is 6 minutes ago in LOCAL time (as the test does)
local_six_min_ago = (datetime.now() - timedelta(minutes=6)).isoformat(
    timespec="seconds"
)
print("Inserting local time:", local_six_min_ago)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_debug",
        test_run_id,
        "Debug Clinic",
        "+919876543210",
        local_six_min_ago,
    ),
)
conn.commit()
conn.close()

# Reconnect for queries
conn = db._connect()

# Check what's actually stored
row = conn.execute("SELECT * FROM calls WHERE run_id=?", (test_run_id,)).fetchone()
print("Stored in DB:", dict(row) if row else None)

# Now let's see what the OLD comparison logic would do
print("\n--- OLD LOGIC ---")
# This is what the original code did:
# strftime('%Y-%m-%d %H:%M:%S', created_at) < strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ?)
cursor = conn.execute(
    """
    SELECT 
        created_at,
        strftime('%Y-%m-%d %H:%M:%S', created_at) as created_fmt,
        strftime('%Y-%m-%d %H:%M:%S', 'now') as now_utc,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') as now_local,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ?) as threshold_5min
    FROM calls WHERE run_id = ?
    """,
    ("-300 seconds", test_run_id),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print(
        "strftime(created_at):", row["created_fmt"]
    )  # This treats the string as UTC format!
    print("now_utc:", row["now_utc"])
    print("now_local:", row["now_local"])
    print("threshold (local - 5min):", row["threshold_5min"])
    print(
        "Comparison result (created < threshold):",
        row["created_fmt"] < row["threshold_5min"],
    )

# Now let's see what the NEW comparison logic should do
print("\n--- NEW LOGIC (using datetime) ---")
# We want to compare both as Unix timestamps for accuracy
cursor = conn.execute(
    """
    SELECT 
        created_at,
        (julianday(created_at) - julianday('1840-01-01')) * 86400.0 as created_ts,
        (julianday('now') - julianday('1840-01-01')) * 86400.0 as now_ts,
        ((julianday('now') - julianday('1840-01-01')) * 86400.0 - ?) as threshold_ts
    FROM calls WHERE run_id = ?
    """,
    (300, test_run_id),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print("created_ts:", row["created_ts"])
    print("now_ts:", row["now_ts"])
    print("threshold_ts (now - 300):", row["threshold_ts"])
    print(
        "Comparison result (created_ts < threshold_ts):",
        row["created_ts"] < row["threshold_ts"],
    )
    print("Age in seconds:", row["now_ts"] - row["created_ts"])

conn.close()

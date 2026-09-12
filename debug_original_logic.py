import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Insert a six minute old row and test the original logic
test_run_id = "debug_original_test"
test_chat = 99012
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
        "plan_debug_original",
        test_run_id,
        "Debug Original Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()

# Test the ORIGINAL logic
print("\n--- Testing ORIGINAL logic ---")
cursor = conn.execute(
    """
    SELECT 
        created_at,
        strftime('%Y-%m-%d %H:%M:%S', created_at) as created_fmt,
        strftime('%Y-%m-%d %H:%M:%S', 'now') as now_utc_fmt,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') as now_local_fmt,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ?) as threshold_local_fmt
    FROM calls WHERE run_id = ?
""",
    ("-300 seconds", test_run_id),
)
row = cursor.fetchone()
if row:
    print("created_at:", row["created_at"])
    print(
        "strftime(created_at):", row["created_fmt"]
    )  # This treats ISO string as SQLite format!
    print("now_utc_fmt:", row["now_utc_fmt"])
    print("now_local_fmt:", row["now_local_fmt"])
    print("threshold_local_fmt (now-5min):", row["threshold_local_fmt"])
    print(
        "Comparison (created_fmt < threshold_local_fmt):",
        row["created_fmt"] < row["threshold_local_fmt"],
    )
    print("")
    print("Analysis:")
    print("  - created_at is local time string: {}".format(row["created_at"]))
    print("  - strftime(created_at) treats it as if it were UTC time in SQLite format")
    print("  - now_local_fmt is correct local time")
    print("  - The comparison is WRONG because it compares apples to oranges")

conn.close()

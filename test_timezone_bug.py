import sys

sys.path.insert(0, ".")
from app import db

# Reproduce the exact test from the beginning of our investigation
conn = db._connect()
test_run_id = "timezone_bug_test"
test_chat = 99015
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))

# Use a specific timestamp we can control - store it as local time string
# Let's say we want to test what happens if we store a time that is
# actually recent but gets interpreted as being in the past due to timezone confusion
fixed_local_time = "2026-09-12 10:00:00"  # This is intended as local time
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_timezone_bug",
        test_run_id,
        "Timezone Bug Clinic",
        "+919876543210",
        fixed_local_time,
    ),
)
conn.commit()
conn.close()

# Now test what the original query thinks about this row's age
print("--- Testing original logic with fixed local time string ---")
conn = db._connect()
cursor = conn.execute(
    """
    SELECT 
        created_at,
        strftime('%Y-%m-%d %H:%M:%S', created_at) as created_fmt,
        strftime('%Y-%m-%d %H:%M:%S', 'now') as now_utc,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') as now_local,
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ?) as threshold
    FROM calls WHERE run_id = ?
""",
    ("-300 seconds", test_run_id),
)  # 5 minutes ago
row = cursor.fetchone()
if row:
    created_at, created_fmt, now_utc, now_local, threshold = row
    print(f"Stored created_at: {created_at}")
    print(f"Formatted created_at: {created_fmt}")
    print(f"Current UTC time: {now_utc}")
    print(f"Current local time: {now_local}")
    print(f"Threshold (local time - 5 min): {threshold}")
    print(f"Is created < threshold? {created_fmt < threshold}")

    # Interpretation:
    # If this returns TRUE, it means the system thinks the row is older than 5 minutes
    # If created_fmt < threshold is TRUE, then the safety net would trigger

    if created_fmt < threshold:
        print(
            "RESULT: Safety net would INCORRECTLY trigger (row seems older than 5 min)"
        )
    else:
        print("RESULT: Safety net would correctly NOT trigger")

# Also let's see what the actual age should be
# We stored '2026-09-12 10:00:00' as a local time string
# Let's see what time it is now in local time
from datetime import datetime

now_local = datetime.now()
print(f"\nCurrent local time: {now_local}")

# Parse our stored time as local time
stored_local = datetime.strptime(fixed_local_time, "%Y-%m-%d %H:%M:%S")
print(f"Stored as local time: {stored_local}")

# Calculate actual age
age_seconds = (now_local - stored_local).total_seconds()
print(f"Actual age in seconds: {age_seconds}")
print(f"Should be stuck? (age > 300): {age_seconds > 300}")

conn.close()

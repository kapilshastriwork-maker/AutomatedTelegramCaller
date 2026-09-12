import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Test the actual get_stuck_running_calls function with current code
test_run_id = "test_actual"
test_chat = 99013
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
        "plan_test_actual",
        test_run_id,
        "Test Actual Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()
conn.close()

# Test the actual function
print("\n--- Testing ACTUAL get_stuck_running_calls ---")
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        print(f"  Stuck row: {row}")
        if row["run_id"] == test_run_id:
            print("  -> The 6-minute-old row IS flagged as stuck")
        else:
            print("  -> Some other row is flagged as stuck")
else:
    print("  -> No rows flagged as stuck")

# Cleanup
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
conn.commit()
conn.close()

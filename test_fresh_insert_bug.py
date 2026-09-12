import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Test if FRESH inserts are incorrectly flagged as stuck with ORIGINAL code
test_run_id = "fresh_bug_test"
test_chat = 99014
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
now_local = datetime.now().isoformat(timespec="seconds")
print("Inserting FRESH local time:", now_local)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_fresh_bug",
        test_run_id,
        "Fresh Bug Clinic",
        "+919876543210",
        now_local,
    ),
)
conn.commit()
conn.close()

# Test the actual function - should NOT be stuck
print("\n--- Testing FRESH insert with ORIGINAL code ---")
stuck = db.get_stuck_running_calls(300)  # 5 minute threshold
print(f"Fresh insert - Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        if row["run_id"] == test_run_id:
            print("BUG: Fresh row INCORRECTLY flagged as stuck!")
            print("  Row:", row)
            break
    else:
        print("OK: Fresh row correctly NOT flagged as stuck")
else:
    print("OK: Fresh row correctly NOT flagged as stuck")

# Cleanup
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
conn.commit()
conn.close()

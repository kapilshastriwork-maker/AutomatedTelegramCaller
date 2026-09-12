import sys

sys.path.insert(0, ".")
from scripts.test_w1_call import test_safety_net_fresh_insert_not_matched
from app import db
from datetime import datetime, timedelta

print("=== Testing fixed fresh insert function ===")

# Replicate what the test does, but using UTC SQLite format like the real app
test_run_id = "fresh_insert_test_fixed"
test_chat = 99002
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
# Insert row created just now - using UTC SQLite format like insert_call default
now_utc = datetime.utcnow()
now_created = now_utc.strftime("%Y-%m-%d %H:%M:%S")
print(f"Inserting now_utc (SQLite format): {now_created}")
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    "VALUES (?, ?, ?, 'running', ?, ?, ?)",
    (
        test_chat,
        "plan_fresh",
        test_run_id,
        "Fresh Test Clinic",
        "+919876543210",
        now_created,
    ),
)
conn.commit()
conn.close()

# Should NOT be matched (age < 5 minutes)
conn = db._connect()
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        if row["run_id"] == test_run_id:
            print("ERROR: Fresh row incorrectly matched as stuck!")
            break
    else:
        print("OK: Fresh row correctly NOT matched as stuck")
else:
    print("OK: Fresh row correctly NOT matched as stuck")

# Cleanup
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
conn.commit()
conn.close()

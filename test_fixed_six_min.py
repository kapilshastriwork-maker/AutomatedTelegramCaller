import sys

sys.path.insert(0, ".")
from scripts.test_w1_call import test_safety_net_six_minute_old_row_is_matched
from app import db
from datetime import datetime, timedelta

print("=== Testing fixed six minute old row function ===")

# Replicate what the test does, but using UTC SQLite format like the real app
test_run_id = "six_min_old_test_fixed"
test_chat = 99003
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
# Insert row created 6 minutes ago - using UTC SQLite format like insert_call default
now_utc = datetime.utcnow()
six_min_ago_utc = now_utc - timedelta(minutes=6)
six_min_ago = six_min_ago_utc.strftime("%Y-%m-%d %H:%M:%S")
print(f"Inserting six_min_ago_utc (SQLite format): {six_min_ago}")
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

# SHOULD be matched (age > 5 minutes)
conn = db._connect()
stuck = db.get_stuck_running_calls(300)
print(f"Stuck rows found: {len(stuck)}")
found = False
if stuck:
    for row in stuck:
        if row["run_id"] == test_run_id:
            print("OK: Six-minute-old row correctly matched as stuck")
            found = True
            break
    if not found:
        print("ERROR: Six-minute-old row NOT matched as stuck!")
else:
    print("ERROR: Six-minute-old row NOT matched as stuck!")

# Cleanup
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
conn.commit()
conn.close()

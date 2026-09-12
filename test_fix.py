import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime, timedelta

# Test 1: Fresh insert (should NOT be stuck)
print("=== Test 1: Fresh insert ===")
conn = db._connect()
test_run_id = "fresh_test"
test_chat = 99007
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
now_local = datetime.now().isoformat(timespec="seconds")
print("Inserting fresh local time:", now_local)
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (test_chat, "plan_fresh", test_run_id, "Fresh Clinic", "+919876543210", now_local),
)
conn.commit()
conn.close()

stuck = db.get_stuck_running_calls(300)  # 5 minute threshold
print(f"Fresh insert - Stuck rows found: {len(stuck)}")
if stuck:
    for row in stuck:
        if row["run_id"] == test_run_id:
            print("ERROR: Fresh row incorrectly flagged as stuck!")
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

# Test 2: Six minute old row (SHOULD be stuck)
print("\n=== Test 2: Six minute old row ===")
conn = db._connect()
test_run_id = "old_test"
test_chat = 99008
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
        "plan_old",
        test_run_id,
        "Old Clinic",
        "+919876543210",
        six_min_ago_local,
    ),
)
conn.commit()
conn.close()

stuck = db.get_stuck_running_calls(300)  # 5 minute threshold
print(f"Six-min-old - Stuck rows found: {len(stuck)}")
found_old = False
if stuck:
    for row in stuck:
        if row["run_id"] == test_run_id:
            print("OK: Six-minute-old row correctly flagged as stuck")
            found_old = True
            break
    if not found_old:
        print("ERROR: Six-minute-old row NOT flagged as stuck!")
else:
    print("ERROR: Six-minute-old row NOT flagged as stuck!")

# Cleanup
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
conn.commit()
conn.close()

print("\n=== All tests completed ===")

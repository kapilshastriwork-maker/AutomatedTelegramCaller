import sys

sys.path.insert(0, ".")
from app import db
from datetime import datetime

# Check what insert_call actually stores in created_at
test_run_id = "check_storage_test"
test_chat = 99016
conn = db._connect()
conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))

# Use insert_call which does NOT specify created_at, so it should use the default
conn.execute(
    "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, patient_name) "
    'VALUES (?, ?, ?, "running", ?, ?, ?)',
    (
        test_chat,
        "plan_storage_test",
        test_run_id,
        "Storage Test Clinic",
        "+919876543210",
        "Test Patient",
    ),
)
conn.commit()

# Check what's actually stored
row = conn.execute(
    "SELECT created_at FROM calls WHERE run_id=?", (test_run_id,)
).fetchone()
print("Stored created_at:", row["created_at"] if row else "None")

# Also check what the default would produce
cursor = conn.execute('SELECT datetime("now") as default_value')
default_row = cursor.fetchone()
print('SQLite datetime("now") default:', default_row[0] if default_row else "None")

conn.close()

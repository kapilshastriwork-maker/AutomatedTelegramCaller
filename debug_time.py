import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "atc.db"
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Test what SQLite's datetime('now') produces
cursor = conn.execute('SELECT datetime("now") as now_time')
row = cursor.fetchone()
print('SQLite datetime("now"):', row["now_time"])

# Test what Python's datetime.now().isoformat() produces
from datetime import datetime

python_iso = datetime.now().isoformat(timespec="seconds")
print("Python isoformat():", python_iso)

# Test what Python's datetime.now() produces in SQLite format
python_sqlite = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
print("Python SQLite format:", python_sqlite)

conn.close()

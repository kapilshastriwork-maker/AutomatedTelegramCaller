import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "atc.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.execute('SELECT datetime("now") as now_utc, time("now") as time_now')
row = cursor.fetchone()
print('datetime("now"):', row[0])
print('time("now"):', row[1])
conn.close()

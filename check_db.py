import sys

sys.path.insert(0, ".")
from app import db

# This should initialize the database
conn = db._connect()
print("Database connected")
# Check if calls table exists
cursor = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='calls'"
)
tables = cursor.fetchall()
print("Calls table exists:", len(tables) > 0)
conn.close()

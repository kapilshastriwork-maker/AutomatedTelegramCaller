import sys

sys.path.insert(0, ".")
from app import db

# Test what formats SQLite's datetime() can parse
conn = db._connect()

print("--- Testing SQLite datetime() parsing ---")

# Test 1: SQLite format (what insert_call stores)
sqlite_format = "2026-09-12 05:00:10"
cursor = conn.execute("SELECT datetime(?) as parsed", (sqlite_format,))
row = cursor.fetchone()
print(f'SQLite format "{sqlite_format}" -> {row[0] if row else "None"}')

# Test 2: ISO format with T (what the test functions use)
iso_format = "2026-09-12T05:00:10"
cursor = conn.execute("SELECT datetime(?) as parsed", (iso_format,))
row = cursor.fetchone()
print(f'ISO format "{iso_format}" -> {row[0] if row else "None"}')

# Test 3: ISO format with space
iso_space_format = "2026-09-12 05:00:10"
cursor = conn.execute("SELECT datetime(?) as parsed", (iso_space_format,))
row = cursor.fetchone()
print(f'ISO space format "{iso_space_format}" -> {row[0] if row else "None"}')

# Test what the comparison looks like for each format
print("\n--- Testing comparison logic ---")
test_time = "2026-09-12 04:55:10"  # 5 minutes ago in UTC
now_utc = "2026-09-12 05:00:10"  # current UTC

print(f"Test time (5 min ago): {test_time}")
print(f"Now (UTC): {now_utc}")

# Test datetime() comparison
cursor = conn.execute(
    """
    SELECT 
        datetime(?) < datetime(?, '-300 seconds') as result
""",
    (test_time, now_utc),
)
row = cursor.fetchone()
print(
    f'datetime(test_time) < datetime(now, "-300 seconds"): {row[0] if row else "None"}'
)

# Test what happens if we store ISO format but compare with datetime()
cursor = conn.execute(
    """
    SELECT 
        datetime(?) < datetime(?, '-300 seconds') as result
""",
    (iso_format, now_utc),
)
row = cursor.fetchone()
print(
    f'datetime(ISO_format) < datetime(now, "-300 seconds"): {row[0] if row else "None"}'
)

conn.close()

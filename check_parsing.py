from datetime import datetime, timezone, timedelta
import time

# Parse the string as UTC time
utc_time = datetime.strptime("2026-09-12T10:20:40", "%Y-%m-%dT%H:%M:%S").replace(
    tzinfo=timezone.utc
)
print("Parsed as UTC:", utc_time.isoformat())
print("UTC timestamp:", utc_time.timestamp())

# Convert to local time
local_time = utc_time.astimezone()
print("As local time:", local_time.isoformat())
print("Local timestamp:", time.mktime(local_time.timetuple()))

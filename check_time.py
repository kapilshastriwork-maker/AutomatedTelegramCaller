from datetime import datetime, timezone, timedelta
import time

# Get current time in various formats
now_local = datetime.now()
now_utc = datetime.now(timezone.utc)
now_utc_naive = datetime.utcnow()

print("Local time:", now_local.isoformat(timespec="seconds"))
print("UTC time:", now_utc.isoformat(timespec="seconds"))
print("UTC naive:", now_utc_naive.isoformat(timespec="seconds"))

# Unix timestamps
print("Local timestamp:", time.mktime(now_local.timetuple()))
print("UTC timestamp:", now_utc.timestamp())
print("UTC naive timestamp:", now_utc_naive.timestamp())

# What time was it 6 minutes ago in local time?
six_min_ago_local = now_local - timedelta(minutes=6)
print("6 min ago local:", six_min_ago_local.isoformat(timespec="seconds"))
print("6 min ago local timestamp:", time.mktime(six_min_ago_local.timetuple()))

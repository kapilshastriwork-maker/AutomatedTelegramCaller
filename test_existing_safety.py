import sys

sys.path.insert(0, ".")
from scripts.test_w1_call import test_poller_safety_net_force_reports_stuck_row

print("Testing existing safety net test...")
try:
    test_poller_safety_net_force_reports_stuck_row()
    print("PASS test_poller_safety_net_force_reports_stuck_row")
except Exception as e:
    print(f"FAIL test_poller_safety_net_force_reports_stuck_row: {e}")
    import traceback

    traceback.print_exc()

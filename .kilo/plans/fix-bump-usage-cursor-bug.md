# Fix Plan: bump_usage cursor indexing bug

## Bug
**File:** `app/db.py`, function `bump_usage` (lines 304-319)

**Current broken code (line 313-317):**
```python
row = conn.execute(
    "SELECT count FROM usage_counters WHERE chat_id = ? AND day = ?",
    (chat_id, day),
)
return int(row["count"])
```

**Problem:** `conn.execute()` returns a cursor object, not a row. The code tries to subscript `row["count"]` directly on the cursor, causing `TypeError: 'sqlite3.Cursor' object is not subscriptable`.

**Correct pattern** (as used correctly in `get_usage` at line 295-299):
```python
row = conn.execute(
    "SELECT count FROM usage_counters WHERE chat_id = ? AND day = ?",
    (chat_id, day),
).fetchone()
return int(row["count"])
```

## Fix

Add `.fetchone()` after the `conn.execute()` call on line 316.

**Lines 313-317 should become:**
```python
row = conn.execute(
    "SELECT count FROM usage_counters WHERE chat_id = ? AND day = ?",
    (chat_id, day),
).fetchone()
return int(row["count"])
```

## Audit: No other functions have this bug

Scanned all `row = conn.execute(` patterns in db.py:
- Line 271 (`find_scheduled_by_prefix`): **OK** — has `.fetchone()`
- Line 295 (`get_usage`): **OK** — has `.fetchone()`
- Line 313 (`bump_usage`): **BUG** — missing `.fetchone()` ← fix this
- Line 344 (`get_pending_confirmation`): **OK** — has `.fetchone()`
- Line 437 (`get_pending_confirmation_by_original_run`): **OK** — has `.fetchone()`
- Line 508 (`get_chain_run`): **OK** — has `.fetchone()`

All other SELECT queries either chain `.fetchall()` or `.fetchone()` properly, or are INSERT/UPDATE statements. **No other fixes needed.**

## Crash Context Analysis

The crash occurs in `_launch_ready_call` → `bump_usage`. Based on the call flow:
1. `plan_call` succeeds (creates the call plan)
2. `run_call` executes the call (places the real phone call via CALL-E)
3. `bump_usage` increments the daily counter ← **crashes here**
4. `insert_call` is never reached (the call record is never saved to DB)
5. The user never gets a result message

**Impact:** The real call was placed (run_call completed), but:
- The call record was NOT inserted into the `calls` table
- The usage counter was NOT incremented
- The user got no feedback

**Run ID recovery:** Since `insert_call` never ran, the `run_id` returned by `run_call` was never stored. The `run_id` lives only in the local variable `run` in `_launch_ready_call`, which is now lost after the crash.

**Can we recover?** Without storing the `run_id` somewhere first, we cannot query `get_call_status` to recover the result. The call was placed but its outcome is untrackable from our end.

## Verification Plan

After applying the fix:
1. Run one real `/call` end-to-end
2. Confirm no crash (bump_usage completes)
3. Query DB: `SELECT * FROM usage_counters;` — verify count incremented
4. Query DB: `SELECT * FROM calls;` — verify call record exists with status

## Files to Modify
- `app/db.py`: Line 316 — add `.fetchone()` after `conn.execute()` in `bump_usage`

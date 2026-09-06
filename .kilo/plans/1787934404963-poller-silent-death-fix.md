# Plan: Fix Silent Poller Death (no poller activity after call completes)

## Problem
The user reported: plan_call and run_call completed successfully (visible in logs at 17:03:43 and 17:03:49), but **zero poller activity** logged afterward for 2+ minutes. The poller started at boot (`Call status poller started`) but never emitted `Polling run_id=...` lines. This means the poller task is silently dying before it can check the call status.

## Root Causes Identified (3 distinct issues)

### 1. Missing `phone` column in DB SELECT (immediate crash)
**File:** `app/db.py` line 157
```python
rows = conn.execute(
    "SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name, phone "
    "FROM calls WHERE status = 'running'"
).fetchall()
```
The `calls` table schema (lines 7-15) has NO `phone` column. The `_migrate` function (lines 92-130) adds `status_message_id`, `batch_id`, `clinic_name`, `earliest_available`, `earliest_batch_summarised` — but NOT `phone`. The SELECT fails with `sqlite3.OperationalError: no such column: phone` on the **first poll iteration**, killing the task silently.

### 2. No outer try/except wrapping the poller loop
**File:** `app/bot.py` `poll_loop` (lines 2547-2586)
The while-True loop has a per-row try/except for `CalleError` (line 2557), but **no try/except around the entire loop body**. Any exception from `get_running_calls`, `db.finish_call`, `_maybe_offer_alternatives`, `_report_terminal`, or other lines crashes the task with no log.

### 3. No `add_done_callback` on the poller task
**File:** `app/bot.py` line 2924
```python
application.bot_data["poller_task"] = asyncio.create_task(poll_loop(application))
```
No exception handler or done callback. If the task dies, nothing is logged — this is the "silent death" pattern.

## Additional Finding: No idle-logging
When `get_running_calls` returns empty (lines 2549-2551), the poller sleeps with **no log line**. This makes it impossible to distinguish "poller is running but idle" from "poller is dead" without the heartbeat log we're adding.

## Solution: Four changes

### Change 1: Fix the DB SELECT (remove `phone` or add column)
Option A (simpler, no migration): Remove `phone` from SELECT in `get_running_calls` (line 157-158) — it's not used by `poll_loop` anyway.

### Change 2: Wrap entire poller loop in try/except + heartbeat log
Add outer try/except around the while-True body, log any exception, then `await asyncio.sleep(POLL_ACTIVE_SECONDS)` and continue (so one bad iteration doesn't kill the poller permanently).

### Change 3: Add idle heartbeat log
Add `logger.debug("Poller tick: %d running calls", len(running))` (or INFO if preferred) on every iteration, including when empty.

### Change 4: Add done-callback to poller task
Attach `add_done_callback` that logs any exception: `lambda t: logger.exception("poller task died", exc_info=t.exception()) if t.exception() else None`

## Code Changes

### 1. `app/db.py` line 157 — Fix SELECT
```python
# Before
rows = conn.execute(
    "SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name, phone "
    "FROM calls WHERE status = 'running'"
).fetchall()

# After (remove unused phone column)
rows = conn.execute(
    "SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name "
    "FROM calls WHERE status = 'running'"
).fetchall()
```

### 2. `app/bot.py` `poll_loop` — Wrap in try/except + heartbeat
```python
async def poll_loop(application) -> None:
    bot = application.bot
    logger.info("Call status poller started")
    while True:
        try:
            running = await asyncio.to_thread(db.get_running_calls)
            logger.debug("Poller tick: %d running calls", len(running))  # or INFO
            if not running:
                await asyncio.sleep(POLL_IDLE_SECONDS)
                continue
            for row in running:
                run_id = row["run_id"]
                try:
                    payload = await asyncio.to_thread(calle_client.get_call_status, run_id)
                    sc = calle_client.structured_result(payload)
                except CalleError as exc:
                    count = _error_counts.get(run_id, 0) + 1
                    _error_counts[run_id] = count
                    logger.warning(
                        "poll error for %s (%d/%d): %s", run_id, count, MAX_ROW_ERRORS, exc
                    )
                    if count >= MAX_ROW_ERRORS:
                        await asyncio.to_thread(db.finish_call, run_id, "POLL_ERROR")
                        _error_counts.pop(run_id, None)
                        await _safe_send(
                            bot,
                            row["chat_id"],
                            f"⚠️ I lost track of your call ({exc}). It may still be "
                            "in progress — please check with the clinic.",
                        )
                    continue

                _error_counts.pop(run_id, None)
                status = sc.get("status")
                logger.info("Polling run_id=%s, current status=%s", run_id, status)
                if status in TERMINAL_STATUSES:
                    logger.info(
                        "Call %s is terminal (status=%s) — starting result processing",
                        run_id, status,
                    )
                    await asyncio.to_thread(db.finish_call, run_id, str(status))
                    handled = await _maybe_offer_alternatives(bot, row, sc, str(status))
                    if not handled:
                        await _report_terminal(bot, row, sc, str(status))
        except Exception:
            logger.exception("poller loop crashed, will retry after %ss", POLL_ACTIVE_SECONDS)
        await asyncio.sleep(POLL_ACTIVE_SECONDS)
```

### 3. `app/bot.py` line 2924 — Add done_callback
```python
poller_task = asyncio.create_task(poll_loop(application))
poller_task.add_done_callback(
    lambda t: logger.exception("poller task died", exc_info=t.exception())
    if t.exception() else None
)
application.bot_data["poller_task"] = poller_task
```

## Files Affected
- `app/db.py` line 157 — fix SELECT
- `app/bot.py` `poll_loop` function (lines 2544-2586) — wrap in try/except + heartbeat
- `app/bot.py` `post_init` function (line 2924) — add done_callback

## Validation Plan
1. Restart the bot.
2. Confirm `Call status poller started` appears.
3. Make a real `/call` that triggers `plan_call` and `run_call` (both should complete).
4. **Within 1 second after `run_call` completes**, verify in the terminal:
   - `Poller tick: 1 running calls` (or debug variant) appears repeatedly every `POLL_ACTIVE_SECONDS`
   - `Polling run_id=..., current status=...` appears for the call
   - Eventually `Call ... is terminal (status=...) — starting result processing` appears
5. If any iteration fails, verify `poller loop crashed` is logged and the poller continues.

## Risk
- Low: Changes are defensive (try/except, logging, removing unused column).
- The `phone` column removal is safe because it's not used by the poller.
- The outer try/except prevents a single bad iteration from killing the poller permanently.

## Open Questions
- Should the heartbeat log be `INFO` or `DEBUG`? User's earlier request used `INFO` for "Polling run_id=..." — a periodic idle log at `DEBUG` is less noisy but `INFO` ensures visibility. Recommend `INFO` for first deployment, can downgrade later.
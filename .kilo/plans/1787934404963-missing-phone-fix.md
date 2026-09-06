# Plan: Fix `_maybe_offer_alternatives` crash — missing phone in calls table

## Problem
`_maybe_offer_alternatives` (bot.py line 2835) crashes with:
```
sqlite3.IntegrityError: NOT NULL constraint failed: pending_confirmations.phone
```

Because:
- `phone = row.get("phone")` returns `None`
- The `phone` column was never added to the `calls` table schema
- The `insert_call` function supports `phone` parameter but callers don't pass it
- My earlier fix to `get_running_calls` SELECT removed `phone` (it didn't exist in the table)

## Root Cause
The phone number from the user's original request is extracted at line 202 (`phone = details.get("phone")`) but **never stored** in the `calls` table when the call record is created after `run_call` succeeds.

## Solution: Four coordinated changes

### 1. `app/db.py` — Add `phone` column migration
In `_migrate()` (around line 108), add:
```python
if "phone" not in columns:
    try:
        conn.execute("ALTER TABLE calls ADD COLUMN phone TEXT")
    except sqlite3.OperationalError:
        pass
```

### 2. `app/db.py` — Fix `get_running_calls` SELECT to include `phone`
Restore the `phone` column to the SELECT:
```python
rows = conn.execute(
    "SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name, phone "
    "FROM calls WHERE status = 'running'"
).fetchall()
```

### 3. `app/bot.py` — Pass `phone` and `clinic_name` to `insert_call` at all three call sites
**Site 1** (main `/call` flow, ~line 668):
```python
await asyncio.to_thread(
    db.insert_call,
    chat_id,
    str(plan_id),
    str(run_id),
    thread.message_id if thread else None,
    None,  # batch_id
    clinic_name,  # clinic_name from the confirmation
    phone,        # phone from user input
)
```

**Site 2** (`_ca_single_call`, ~line 1725):
```python
row_id = db.insert_call(
    chat_id,
    plan_id,
    None,
    thread_msg.message_id if thread_msg else None,
    batch_id=batch_id,
    clinic_name=name,
    phone=phone,  # add this
)
```

**Site 3** (`_execute_chain`, ~line 3558):
```python
await asyncio.to_thread(
    db.insert_call,
    chat_id,
    str(plan["plan_id"]),
    str(run_id),
    None,
    batch_id=batch_id,
    clinic_name=clinic_name,
    phone=phone,  # add this
)
```

### 4. `app/bot.py` — Defensive fallback in `_maybe_offer_alternatives`
If phone is still unavailable (None), log clearly and send a plain-text alternative message instead of crashing:
```python
phone = row.get("phone")
if not phone:
    logger.warning(
        "Alternatives offered for run_id=%s but phone is missing from calls row; "
        "falling back to plain-text message",
        original_call_run_id,
    )
    # Send plain text without buttons
    text = f"📅 The clinic offered alternatives: {', '.join(a['raw_phrase'] for a in alternatives)}"
    await _safe_send(bot, chat_id, text)
    return True  # treated as handled
```

## Current Code State (before fix)

### `app/db.py` `_migrate()` — phone NOT added (lines 92-130)
### `app/db.py` `get_running_calls()` — phone removed from SELECT (line 157)
### `app/bot.py` line 668-673 — insert_call called without phone/clinic_name
### `app/bot.py` line 1725-1732 — insert_call called without phone
### `app/bot.py` line 3558-3563 — insert_call called without phone
### `app/bot.py` line 2828 — `phone = row.get("phone")` returns None
### `app/bot.py` line 2835-2845 — crashes on insert_pending_confirmation

## Validation Plan
1. Restart bot
2. Send `/call` with a request designed to trigger alternatives (e.g., request a fully booked time)
3. Confirm alternatives card with buttons appears
4. Check terminal: no `sqlite3.IntegrityError: NOT NULL constraint failed: pending_confirmations.phone`
5. Verify `Poller tick: N running calls` continues

## Risk
- Low — additive schema migration (ALTER TABLE ADD COLUMN is safe)
- The three `insert_call` call sites are all called after `run_call` succeeds, when phone/clinic_name are known
- Defensive fallback ensures no user-facing crash even if phone is missing for some edge case
# Plan: Fix _nested TypeError and _maybe_offer_alternatives cross-contamination

## Goal
Fix two bugs in bot.py:
1. `TypeError: _nested() takes 2 positional arguments but 3 were given` at bot.py:3709
2. `_maybe_offer_alternatives` incorrectly triggered for /chain step results instead of only /call single-call flow

## Bug 1: _nested TypeError

### Root Cause
`_nested` defined at bot.py:2306 with signature `def _nested(sc: dict, key: str) -> dict:` accepts exactly 2 parameters. But at bot.py:3709 it's called as `_nested(terminal_sc, "result", "outcome")` with 3 args.

### Evidence
- `_nested(sc, "result")` used 5x in `_friendly_result_text` (bot.py:2320,2321,2322,2323,2324) — all 2-arg calls
- New call at 3709: `_nested(terminal_sc, "result", "outcome")` — crashes with TypeError
- `_probe(d, *keys)` at bot.py:2311 already handles variadic keys — same pattern should apply to `_nested`

### Fix: Make _nested variadic
Change `_nested` at bot.py:2306 to accept `*keys` and walk through them level by level:
```python
def _nested(sc: dict, *keys: str) -> dict:
    value = sc
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return {}
    return value if isinstance(value, dict) else {}
```
Backward-compatible: existing `_nested(sc, "result")` calls work unchanged; new `_nested(terminal_sc, "result", "outcome")` also works.

## Bug 2: _maybe_offer_alternatives triggered for chain steps

### Root Cause
The global `poll_loop` at bot.py:2583 iterates `db.get_running_calls()` which returns ALL running calls including chain steps. At bot.py:2624, `_maybe_offer_alternatives` is called for every terminal call. Chain steps are inserted into the `calls` table via `db.insert_call` at bot.py:3662 during `_execute_chain`. So when a chain step completes, the poller picks it up and shows the alternatives card — wrong flow.

### Evidence
- `poll_loop` queries `SELECT id, chat_id, plan_id, run_id, ... FROM calls WHERE status = 'running'` — includes both /call and /chain steps
- `_maybe_offer_alternatives` is designed for /call's single-call flow: checks Groq alternatives from summary, persists pending_confirmations, sends alternatives card
- Chain steps have their own fallback logic at bot.py:3713-3718 (books if `terminal_status == "COMPLETED" and task_completed is True`); they should never also trigger Feature A's separate callback-confirmation flow
- Chain steps have `batch_id = f"chain_{short_id}"` (bot.py:3462) while single/multi-call uses `batch_id = uuid.uuid4().hex[:12]` (bot.py:1856) — clean discriminator
- `db.get_running_calls()` at db.py:170 does NOT select `batch_id` currently

### Fix: Two changes

#### Change A: Add batch_id to get_running_calls()
In `db.py:get_running_calls()` (line 170), add `batch_id` to the SELECT:
```python
SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name, phone, patient_name, batch_id
FROM calls WHERE status = 'running'
```

#### Change B: Guard _maybe_offer_alternatives in poll_loop
In `poll_loop` at bot.py:2624, skip `_maybe_offer_alternatives` for chain steps:
```python
if not (row.get("batch_id") or "").startswith("chain_"):
    handled = await _maybe_offer_alternatives(bot, row, sc, str(status))
    if not handled:
        await _report_terminal(bot, row, sc, str(status))
else:
    await _report_terminal(bot, row, sc, str(status))
```
This ensures chain steps only go through their own booking logic, not Feature A's callback-confirmation flow.

## Files Changed
1. `app/bot.py`:
   - Line 2306: Make `_nested` variadic (`*keys`)
   - Line 2624: Guard `_maybe_offer_alternatives` with batch_id chain prefix check
2. `app/db.py`:
   - Line 170: Add `batch_id` to SELECT in `get_running_calls()`

## Validation
1. Run `/chain` with 2+ clinics — verify no alternatives card appears for chain steps
2. Run `/call` with a clinic — verify alternatives card still appears for single calls when applicable
3. Check logs for no TypeError at line 3709
4. Verify `_nested("a", "b", "c")` works for the multi-level lookup at bot.py:3709

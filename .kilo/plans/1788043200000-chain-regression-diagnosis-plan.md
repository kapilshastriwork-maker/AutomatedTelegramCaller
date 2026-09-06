# Plan: Diagnose and fix /chain regression — step 1 falsely reported as "booked"

## Goal
Investigate and fix the regression where a /chain run against Dr Radhika got back a result explicitly saying "no appointment was booked" but the chain reported "✅ booked at step 1" and stopped, never trying step 2.

## Current Logic (bot.py:3717-3738)
```python
outcome = _nested(terminal_sc, "result", "outcome") if terminal_sc else {}
task_completed = outcome.get("task_completed")
booked = terminal_status == "COMPLETED" and task_completed is True

if booked:
    success_step = step_num
    success_clinic = clinic_name
    trace.append(f"{step_num}. {clinic_name}: ✅ booked")
    await asyncio.to_thread(db.set_chain_status, chain_short_id, "completed")
    break

trace.append(
    f"{step_num}. {clinic_name}: not booked "
    f"(status={terminal_status}, task_completed={task_completed})"
)
await _safe_send(...)
await asyncio.to_thread(db.advance_chain_step, chain_short_id, idx + 1)
```

## Hypothesis
The bug is that `task_completed` is not strictly `False` when "no appointment was booked" — it could be:
- `None` (missing key)
- A truthy string like `"yes"` or `"partial"`
- A string `"false"` (Python truthy)
- Any value where `task_completed is True` incorrectly evaluates to True (impossible for non-True, but worth verifying)

The condition `task_completed is True` requires exact identity with the `True` singleton. So `None`, `False`, `"yes"`, `1`, `"false"` all yield `False` for `is True`. The only way `booked` becomes True is if `task_completed` is literally the boolean `True`.

But wait — what if CALL-E returns a nested structure where `task_completed` is a dict or something truthy? No, `outcome.get("task_completed")` returns the value directly.

## Git History
**No git history available** — the repo was initialized without commits (`git log` shows "no commits yet"). Cannot show pre-W1 diff as requested.

## Diagnostic Plan
1. **Add logger.info** at the evaluation point (right before `booked = ...`) to print exact values:
   - `terminal_status`
   - `task_completed` (raw value and type)
   - `booked` (computed result)
   - Also log `outcome` dict for full context

2. **Re-run /chain** with the same scenario (Dr Radhika → Dr Raghav) to capture the log output

3. **Analyze log** to determine:
   - What exact value `task_completed` has when "no appointment was booked"
   - Whether `terminal_status` is unexpectedly `"COMPLETED"`
   - Whether the `_nested` path `"result" → "outcome"` is correct

4. **Fix** based on findings:
   - If `task_completed` is truthy but not `True` (e.g., string), tighten to explicit boolean check
   - If `task_completed` is missing/`None`, verify `_nested` path is correct
   - If `terminal_status` is wrong, trace why CALL-E reports COMPLETED for failed booking

## Implementation Steps (for implementation-capable agent)
1. Edit `app/bot.py` line ~3717: add `logger.info("chain %s step %d eval status=%s task_completed=%r type=%s outcome_keys=%s", chain_short_id, step_num, terminal_status, task_completed, type(task_completed).__name__, list(outcome.keys()) if isinstance(outcome, dict) else "not-dict")`
2. Restart bot
3. User runs `/chain` with Dr Radhika (step 1) and Dr Raghav (step 2)
4. Capture log output
5. Propose and implement fix

## Validation
- After fix, re-run `/chain` with a clinic that should NOT book
- Verify chain correctly logs "not booked" and proceeds to step 2
- Verify chain still correctly stops and reports "✅ booked" when booking actually succeeds
- No regression in single-call `/call` flow

## Files to Modify
- `app/bot.py` (diagnostic log, then fix)

## Open Questions
- What exact structure does CALL-E return for a failed booking? (Need log to confirm)
- Was this logic moved/changed during W1 refactor? (No git history to verify)
- Should we also log the `summary` field for context?
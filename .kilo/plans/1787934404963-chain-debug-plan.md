# Plan: Fix /chain preflight task text bug

## Goal
Fix the bug where `_preflight_chain` sends a generic task to CALL-E without clinic name or phone number, causing CALL-E's clarify loop to ask "What phone number should I call?" even though the extraction already captured them correctly.

## Root Cause Confirmed
Debug logs showed `chain_received` extraction succeeded perfectly (steps=2, valid_count=2, both targets with correct name/phone, patient_name captured). But `_preflight_chain` at line 3155-3161 composes:
```
f"I will call a sequence of clinics about this request: {what}. {SAFETY_SUFFIX}"
```
`what` is just `reason, preferred_date, preferred_time` — no clinic name, no phone number. CALL-E correctly asks for the phone because the task text never included one.

## Fix Required
Modify `_preflight_chain` (bot.py:3150-3181) to:
1. Read `steps` from `context.user_data` (stored at line 3266)
2. Read `patient_name` from `context.user_data`
3. Use `_compose_chain_task_input(first_step, patient_name, reason, preferred_date, preferred_time, alternatives_on=True)` to compose the task — same function used by `_execute_chain` per-step
4. Fallback to generic text only if `steps` is empty (defensive)

## Changes Made
**File**: `app/bot.py` — `_preflight_chain` function (lines 3150-3187)

**Before**:
```python
async def _preflight_chain(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    what = context.user_data.get("what", "")
    language = _get_flow_language(context)
    await _safe_send(context.bot, chat_id, "🔍 Checking your chain request with CALL-E…")
    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            f"I will call a sequence of clinics about this request: {what}. "
            f"{SAFETY_SUFFIX}",
            None,
            language,
        )
    except CalleError as exc:
        # ... error handling
```

**After**:
```python
async def _preflight_chain(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    what = context.user_data.get("what", "")
    language = _get_flow_language(context)
    await _safe_send(context.bot, chat_id, "🔍 Checking your chain request with CALL-E…")

    steps = context.user_data.get("steps", [])
    patient_name = context.user_data.get("patient_name")
    chain_details = context.user_data.get("chain_details", {})
    if steps:
        first_step = steps[0]
        task_input = _compose_chain_task_input(
            first_step,
            patient_name,
            chain_details.get("reason"),
            chain_details.get("preferred_date"),
            chain_details.get("preferred_time"),
            alternatives_on=True,
        )
    else:
        task_input = f"I will call a sequence of clinics about this request: {what}. {SAFETY_SUFFIX}"

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            task_input,
            None,
            language,
        )
    except CalleError as exc:
        # ... error handling
```

## Verification
Restart bot, run `/chain` with 2 valid clinics + phones + patient name. Verify:
1. Preflight plan_call no longer triggers "What phone number should I call?" clarify questions
2. Confirm card shows correctly
3. `_execute_chain` still works correctly for actual calls (unchanged)

## Status
✅ FIXED: The bug has been resolved. The preflight now uses the actual first clinic's name and phone number in the task text sent to CALL-E.
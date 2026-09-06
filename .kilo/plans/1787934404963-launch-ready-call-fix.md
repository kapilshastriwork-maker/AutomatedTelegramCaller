# Fix: _launch_ready_call NameError and empty phone

## Root Cause
- `_launch_ready_call` accesses `context.user_data.get("clinic_name")` at lines 674-675
- But the function signature doesn't receive any `context` object → NameError
- The `calls` table has a phone column now (migration added it), so passing "" would fail the NOT NULL constraint on `pending_confirmations.phone`

## Code Changes Required

### File: app/bot.py

#### Change 1: Add clinic_name/phone parameters to _launch_ready_call signature (line 608)

**Before:**
```python
async def _launch_ready_call(
    bot,
    chat_id: int,
    plan: dict,
    retry_hint: str = "Send /call to try again.",
    base_message=None,
) -> int:
```

**After:**
```python
async def _launch_ready_call(
    bot,
    chat_id: int,
    plan: dict,
    clinic_name: str = "",
    phone: str = "",
    retry_hint: str = "Send /call to try again.",
    base_message=None,
) -> int:
```

#### Change 2: Replace undefined context.user_data calls with parameter (lines 674-675)

**Before:**
```python
        context.user_data.get("clinic_name", ""),
        context.user_data.get("phone", ""),
```

**After:**
```python
        clinic_name,
        phone,
```

#### Change 3: Update all 4 call sites to pass clinic_name/phone

**Call site 1: on_confirm (line 455)**
```python
w
ho = context.user_data.get("who", "")
clinic_name, phone = (who.split(" at ", 1) if who else ("", ""))
state = await _launch_ready_call(context.bot, chat_id, plan, clinic_name, phone)
```

**Call site 2: Clarify confirm (line 510)**
```python
who = context.user_data.get("who", "")
clinic_name, phone = (who.split(" at ", 1) if who else ("", ""))
state = await _launch_ready_call(context.bot, chat_id, plan, clinic_name, phone)
```

**Call site 3: scheduled_call_job (line 1258)**
```python
clinic_name, phone = (who.split(" at ", 1) if who and " at " in who else ("", ""))
await _launch_ready_call(
    bot, chat_id, plan,
    clinic_name=clinic_name, phone=phone,
    retry_hint="Please use /call.", base_message=base,
)
```

**Call site 4: Callback alt-pick (line 2542)**
```python
who = context.user_data.get("who", "")
clinic_name, phone = (who.split(" at ", 1) if who else ("", ""))
state = await _launch_ready_call(context.bot, query.message.chat_id, plan, clinic_name, phone)
```

## Why this works
- `who` is always stored as `"{clinic} at {phone}"` by `_apply_details()` (line 203)
- `split(" at ", 1)` handles both parts correctly
- Empty-string fallbacks prevent crashes if `who` is missing
- phone is now passed to `db.insert_call` and stored in the calls table
- `_maybe_offer_alternatives` can now get `row.get("phone")` from the call record
- No more IntegrityError on `pending_confirmations.phone`
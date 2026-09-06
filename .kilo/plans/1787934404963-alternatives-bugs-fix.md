# Plan: Fix Callback Alternative Bugs — Phone Re-Ask + Placeholder Leak

## Bug 1: Callback re-asks for phone number and patient name

### Location
`app/bot.py` `on_pick_alternative` (lines 2503-2527)

### Problem
When user selects an alternative, the second call's `plan_call` task input is:
```python
f"Call {who}. {what}. {SAFETY_SUFFIX}"
```
At line 2503, `who` is set to `pending["clinic_name"] or "the clinic"` — **the phone number is omitted**. Since the task input string has no phone, CALL-E's planning stage asks for it, re-triggering the clarify loop the user just finished.

The `pending_confirmations` row stores both `clinic_name` and `phone`, so the fix is to reconstruct `who` with the phone.

### Current Code (line 2503)
```python
who = pending["clinic_name"] or "the clinic"
```

### Fix
```python
who = f"{pending['clinic_name']} at {pending['phone']}" if pending.get("phone") else (pending["clinic_name"] or "the clinic")
```

This ensures the second call's task input carries the full `"{clinic} at {phone}"` string that `plan_call` / `_compose_book_task_input` expects, so CALL-E doesn't re-ask for the phone.

---

## Bug 2: "Patient: the patient" placeholder leaks into UI

### Locations
1. `_maybe_offer_alternatives` (line 2860): `patient_name = ""` is **hardcoded** and stored in DB.
2. `on_pick_alternative` (line 2504): `patient_name = pending["patient_name"] or "the patient"` — falls back to literal "the patient" because the DB row has `patient_name=""`.
3. `on_pick_alternative` message edit (line 2514): `f"📞 Calling {who} to book for {patient_name} at {raw_phrase}…"` — renders the placeholder.
4. Alternatives display message (line 2891): `patient_display = patient_name or "the patient"` — same fallback.

### Problem
Patient name is **not extracted** from the user's original message by the Groq extraction pipeline (which only extracts `clinic_or_doctor`, `phone`, `reason`, `preferred_date`, `preferred_time`). Since it was never captured, `pending_confirmations.patient_name` is always `""`, and the `or "the patient"` fallback makes a hardcoded placeholder appear in the UI — inconsistent with the first confirmation message, which also omits patient name.

### Fix
Since patient name is not part of the extracted data, and it was also omitted from the original `/call` confirmation message, the alternatives flow should not display a patient line that appears to contain data when it doesn't. Two sub-fixes:

**Sub-fix A — In `_maybe_offer_alternatives` (line 2860):**
Keep `patient_name = ""` (no change needed — it's already correct that it's empty).

**Sub-fix B — In the alternatives display text (around line 2890-2891):**
Only show the patient line when there's actual data:
```python
patient_display = patient_name if patient_name else ""
# Then in the text build (line 2893-2894):
# Omit the "Patient: ..." line entirely when patient_display is empty,
# or conditionally include it:
text_parts = []
if patient_display:
    text_parts.append(f"Patient: {patient_display}")
```

**Sub-fix C — In `on_pick_alternative` callback message (line 2514):**
Remove the patient reference from the call announcement, or make it conditional:
```python
patient_name_display = pending["patient_name"] if pending.get("patient_name") else ""
call_text = f"📞 Calling {who} to book"
if patient_name_display:
    call_text += f" for {patient_name_display}"
call_text += f" at {raw_phrase}…\n\n"
```

This avoids leaking the "the patient" string and is consistent with the original confirmation message which also omitted patient name.

---

## Summary of Changes

| File | Line(s) | Change |
|------|---------|--------|
| `app/bot.py` | 2503 | `who = f"{pending['clinic_name']} at {pending['phone']}" if pending.get('phone') else (pending['clinic_name'] or 'the clinic')` |
| `app/bot.py` | 2514 | Make patient display conditional in callback message |
| `app/bot.py` | 2890-2891 | Only show patient line in alternatives message when patient_name has actual value |

## Validation
1. Restart bot with all fixes
2. Run `/call` with a request designed to trigger alternatives (e.g., fully booked time)
3. Select an alternative from the card
4. Verify:
   - The second call's `plan_call` input includes the phone number → no re-clarify
   - The alternatives message does NOT show "Patient: the patient" placeholder
   - The call proceeds (or gracefully handles missing data) without IntegrityError
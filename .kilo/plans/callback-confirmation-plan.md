# Plan: `/chain` — sequential fallback call workflow

## Goal

Add a new `/chain` command that lets the user specify an ordered list
of clinics as fallbacks. The bot calls them **sequentially**: if step
N does not result in a booking, it proceeds to step N+1. The first
step to actually book stops the chain. If every step fails to book,
the user gets a plain "nothing worked" summary with the full trace.

Example user message:

> /chain Call Dr Sharma about a cleaning tomorrow; if no slot this week,
> call Vision Care instead; if neither works, tell me.

## Decisions (resolved)

### 1. Fallback trigger (simplified)

**Continue to the next step whenever the current step's outcome is
anything other than `status == "COMPLETED"` and
`task_completed: true`.** No-answer, declined, voicemail, no-availability,
"we don't do that", "fully booked" — all trigger the fallback
identically. The chain only stops on a confirmed booking.

This is a single-line condition. The previous "COMPLETED +
task_completed=false (and not VOICEMAIL) ⇒ continue" distinction is
dropped. Reason classification is dropped entirely. This is
intentionally conservative for the user: if a real clinic said
anything other than "yes, booked", the user asked for a fallback and
gets one.

### 2. Quota gate (mirrors `/callaround`)

Before showing the confirm card, check `MAX_CALLS_PER_DAY - usage`:
- `remaining >= chain_length`: standard confirm card.
- `0 < remaining < chain_length`: offer "▶️ Try first R steps" where
  `R = min(remaining, chain_length)`, same UX as `_ask_ca_confirm`.
  The chain then runs the first R steps only (truncated).
- `remaining == 0`: standard daily-limit message, clear state, end.

Quota is bumped per-step on successful `run_call` launch (matches
existing semantics). If the chain is truncated by quota, the
truncated steps are silently skipped — no "skipped" trace line.

### 3. State in DB (visibility only, no restart-survival)

A new `chain_runs` table stores the chain definition and progress:

```sql
CREATE TABLE chain_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id TEXT UNIQUE NOT NULL,
    chat_id INTEGER NOT NULL,
    language TEXT,
    patient_name TEXT,
    reason TEXT,
    preferred_date TEXT,
    preferred_time TEXT,
    steps_json TEXT NOT NULL,  -- [{name, phone, reason, preferred_date, preferred_time}, ...]
    current_step INTEGER NOT NULL DEFAULT 0,  -- 0-indexed; equals steps length when done
    status TEXT NOT NULL DEFAULT 'running',  -- running | completed | failed | cancelled
    last_message_id INTEGER,  -- the latest per-step result thread, for debugging
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

**Restart-survival is explicitly out of scope.** If the bot restarts
mid-chain, the chain is left in `status='running'` with whatever
`current_step` it had reached. A human inspects the table; the
chain is not auto-resumed. Rationale: chains run in real time over
minutes (each step is a real call), unlike `/schedule` which spans
hours/days, so the cost of restart-survival machinery is not
worth it for the demo. This is documented as a known limitation in
`progress.md` and `README.md`.

Helpers (visibility / debugging only — the executor does not use
`get_running_chain_runs` to auto-resume):
- `insert_chain_run(...)`
- `get_chain_run(short_id) -> dict | None`
- `advance_chain_step(short_id, new_step_index)`
- `set_chain_status(short_id, status)`

### 4. Execution: dedicated async function per chain (no shared poller)

`_execute_chain(bot, chat_id, chain_short_id, steps, language)` is
launched exactly once from `on_chain_confirm` via
`asyncio.create_task`. The function does its own plan → run → wait →
decide loop per step. It does **not** coordinate with the global
`poll_loop` — the chain task polls its own `run_id` directly via
`calle_client.get_call_status(run_id)`.

Why not share the poller: chains don't interleave with concurrent
calls (only one chain step is in-flight at a time per chat). Adding
Event-keyed coordination into `poll_loop` for a single-call-at-a-time
flow is more complexity than a self-contained async loop. The
`poll_loop` still processes chain `calls` rows in the background and
will edit each step's result message via the standard path
(`_report_terminal`) — that is desired (the user sees the friendly
summary in the same thread as the "📞 Trying..." message). The chain
task does not need to send a second friendly message; it just reads
the terminal `sc` to decide CONTINUE/STOP.

Per-step loop body:
1. Compose `task_input` for the step (see step-task composition
   below).
2. `plan_call` via `asyncio.to_thread` (no per-step pre-flight
   clarify loop beyond the global one that ran on step 1 in the
   confirm flow).
3. `run_call` via `asyncio.to_thread`. On failure, mark
   `chain_runs.status='failed'`, send a single error message, break.
4. Insert `calls` row with `batch_id="chain_<short_id>"` +
   `clinic_name=step.name` + `status_message_id` of the "📞
   Trying..." message.
5. Send "📞 Trying {clinic}..." to the chat. (This is the thread
   the global poller will later edit into the friendly summary.)
6. Wait for terminal status: in a loop, call
   `calle_client.get_call_status(run_id)` every
   `POLL_ACTIVE_SECONDS`. Break when status is in
   `TERMINAL_STATUSES`.
7. Apply the trigger rule (decision 1):
   - `status == "COMPLETED"` and `task_completed is True` →
     STOP, success. Mark `chain_runs.status='completed'`.
   - anything else → CONTINUE. If there are more steps, mark
     `current_step += 1` and loop. If we just processed the last
     step, mark `chain_runs.status='failed'` and stop.
8. After the loop ends, send a one-line trace summary to the chat
   (the user sees a final wrap-up message on top of the per-step
   threads).

The global `poll_loop` runs in parallel and edits each step's result
message — but the chain task does not depend on it. If `poll_loop` is
slow to pick up a row, the chain task still has the terminal `sc` and
can decide. (In practice `poll_loop` is faster than the chain's
3-second poll, so the user usually sees the per-step friendly
summary before the chain moves on.)

### 5. Step task composition

`_compose_chain_task_input(step, patient_name) -> str`:

```
"Call {step.name} at {step.phone} about {step.reason or inherited.reason}"
" (preferred: {step.preferred_date or inherited.preferred_date} {step.preferred_time or inherited.preferred_time})"
" for {patient_name or 'the patient'}."
" {BOOKING_ALTERNATIVES_INSTRUCTION}"   # only on step 1; later steps are clean
" {SAFETY_SUFFIX}"
```

Step 1 includes `BOOKING_ALTERNATIVES_INSTRUCTION` so the callback
confirmation flow still works (an alternatives offer on step 1
short-circuits the chain via the existing `pending_confirmations`
flow, which is fine — the chain ends at the first booked slot).
Steps 2..N use a plain booking template (no alternatives request)
because each is a fallback, not the primary attempt.

### 6. Groq extraction

New `extract_chain(text) -> dict` in `app/groq_client.py` with its own
system prompt. Return shape:

```json
{
  "patient_name": "John" | null,
  "reason": "cleaning" | null,
  "preferred_date": "tomorrow" | null,
  "preferred_time": null,
  "steps": [
    {"clinic_or_doctor": "Dr Sharma", "phone": "+91..."},
    {"clinic_or_doctor": "Vision Care", "phone": "+91..."}
  ]
}
```

The prompt instructs the model to preserve the user's order and to
treat the first clinic as the primary; subsequent clinics as
fallbacks ("if X doesn't work, try Y"). Each step inherits the
shared `preferred_date`/`preferred_time`/`reason`/`patient_name` from
the message; later steps may override `preferred_date`/
`preferred_time` if the user says e.g. "if no slot this week, try
Vision Care next week".

Validation in code (not in the model):
- each step must have a valid E.164 phone (else `invalid_names` lists
  the unparseable clinic; user re-sends the whole message with
  corrected numbers).
- minimum 2 steps (else redirect to `/call`).
- maximum 5 steps (matches `MAX_CA_TARGETS`).

### 7. Pre-flight clarify loop

The standard `_preflight_and_ask` is invoked once on the chain's
shared request (the same way `/callaround` does it for its shared
request) before the confirm card. This reuses the existing CALL-E
clarify loop without per-step pre-flight overhead. After confirm, the
chain's `_execute_chain` does not re-run pre-flight per step — it
just plans and runs each step directly. If a later step's
`plan_call` returns `ready_to_run: false`, that's treated as a
failure (the step couldn't even be planned) and the chain continues
to the next step or fails.

### 8. Confirmation card

`_ask_chain_confirm` mirrors `_ask_ca_confirm`:
- Card text lists each step in order ("Step 1: Dr Sharma…",
  "Step 2: Vision Care…") plus the shared request.
- ⚠️ "This will place up to N real calls" with N = chain length.
- If quota insufficient: "▶️ Try first R steps" button (truncated
  chain).
- Standard quota-exhausted message at zero.

### 9. Conversation states

```python
CHAIN_FIRST, CHAIN_FOLLOWUP, CHAIN_CONFIRM = range(26, 29)
```

Plus the existing `LANG_CHAIN` slot to keep the language-keyboard
flow consistent. Actually since we already have `LANG_EARLY` (25),
we'll add `LANG_CHAIN = 29`. ConversationHandler states for the new
command.

## Implementation steps

### `app/groq_client.py`
1. Add `_CHAIN_SYSTEM_PROMPT` constant.
2. Add `extract_chain(text) -> dict`. Validates phones in code;
   returns `{patient_name, reason, preferred_date, preferred_time,
   steps: [{name, phone, reason, preferred_date, preferred_time}],
   valid_count, invalid_names, missing_fields}` similar to
   `extract_call_around`.

### `app/db.py`
1. Add `_CHAIN_SCHEMA` constant and execute it in `_connect`.
2. Helpers:
   - `insert_chain_run(short_id, chat_id, language, patient_name,
     reason, preferred_date, preferred_time, steps_json)`
   - `get_chain_run(short_id) -> dict | None`
   - `advance_chain_step(short_id, new_step_index)`
   - `set_chain_status(short_id, status)`

### `app/bot.py`
1. New states `CHAIN_FIRST`, `CHAIN_FOLLOWUP`, `CHAIN_CONFIRM`,
   `LANG_CHAIN`.
2. New `chain_entry`, `chain_received`, `chain_followup`
   (mirroring `ca_received`/`ca_followup`).
3. New `_ask_chain_confirm` (quota + keyboard, mirrors
   `_ask_ca_confirm`).
4. New `on_chain_confirm` (patterns: `chain_yes`, `chain_no`,
   `chain_reduce`).
5. New `_compose_chain_task_input(step, patient_name,
   alternatives_on: bool) -> str` — step 1 passes True, later
   steps False.
6. New `_execute_chain(bot, chat_id, chain_short_id, steps,
   language)`. Per-step loop as in decision 4.
7. New "📞 Trying..." message sender (a small helper that
   creates a per-step thread; the global poller edits it).
8. New one-line trace-sender at end of `_execute_chain`.
9. Update `HELP_TEXT` with `/chain` line.
10. Register `CommandHandler("chain", chain_entry)` in `main()`.

### `README.md`
- Add `/chain` example to Usage section.
- Add "Known limitation: `/chain` does not survive bot restarts
  mid-flight. If the bot is restarted while a chain is in progress,
  the chain's `current_step` is preserved in the DB but execution
  does not resume." to Known Issues.

### `progress.md`
1. Add Session 13 entry at the top of the Session Log.
2. Add a Current State paragraph summarising `/chain`.
3. Add a Key Decision about the simplified trigger rule and the
   no-restart-survival descope.
4. Add "Chains don't survive bot restarts mid-flight" to Known
   Issues / TODO.

## Validation

### Offline test suite (fast, in-process)
- `extract_chain` returns correctly shaped `steps[]` for a 2-step
  message; inherits shared fields; flags invalid phones with
  `invalid_names`; rejects single-step chain.
- `_ask_chain_confirm`:
  - quota ≥ N → standard confirm card, button label "Yes, run all N".
  - 0 < quota < N → "▶️ Try first R" card with R = min(quota, N).
  - quota == 0 → standard daily-limit message, no buttons.
- `on_chain_confirm`:
  - `chain_yes` → launches `_execute_chain` for full chain.
  - `chain_reduce` → launches `_execute_chain` for truncated chain.
  - `chain_no` → cancels, clears state.
- `_execute_chain` (with stubbed `calle_client`):
  - Step 1 COMPLETED + task_completed=true → STOP, success,
    `chain_runs.status='completed'`, trace "✅ Booked at step 1 (X)."
  - Step 1 COMPLETED + task_completed=false → CONTINUE, step 2
    launches.
  - Step 1 NO_ANSWER → CONTINUE, step 2 launches (per the
    simplified rule).
  - All steps not booked → `chain_runs.status='failed'`, trace
    "❌ No availability at any of the N clinics." + per-step list.
  - `run_call` exception → mark failed, break loop, single error
    message.
- Per-step message order in the chat: "📞 Trying X" (from chain
  task), then friendly summary (from poller, may arrive slightly
  after), then "📞 Trying Y" (next step) or final trace.
- `chain_runs` row: `current_step` advances, `status` transitions
  correctly, `steps_json` round-trips.

### Live 2-step chain test (flagged)

**Cost: 2 real calls per test cycle** (default daily quota = 3).
One full test uses 2/3 calls. The test will validate the trigger
rule and the inter-step transition.

**Goal of the live test:** confirm that when step 1 ends without a
booking (for any reason — no-answer, declined, voicemail,
unavailable, "we don't do that"), step 2 launches automatically and
the trace summary is correct.

**How to produce a non-booking step-1 outcome reliably:**

- **Best bet**: a deliberately impossible slot. Ask for "a complex
  cleaning at 3:47am on a Tuesday in 6 months" — most clinics will
  not be open at 3:47am, and 6 months out is past typical booking
  windows. High probability of the clinic saying "no" politely.
- **Fallback**: ask for a service the clinic doesn't offer, e.g.
  "MRI scan at a dental clinic". Some clinics will redirect; some
  will flat-decline. The latter triggers CONTINUE; the former
  might trigger STOP (if the AI treats the redirect as
  task_completed=true). If STOP fires here, the test still
  validates the flow end-to-end, just not the fallback transition.
- **Worst case**: the clinic says "yes, 3:47am works." The test
  inconclusively passes the flow but doesn't validate the
  fallback. Retry with a more obviously-impossible request.

**Test sequence:**
1. `/chain` → "Call Dr Sharma at +91... for a complex cleaning
   at 3:47am in 6 months; if not available, call Vision Care at
   +91... instead; if neither works, just tell me."
2. Confirm card → tap "Yes, run all 2."
3. Expected: 📞 Trying Dr Sharma, then either (a) friendly summary
   on that thread + 📞 Trying Vision Care (continuing), or
   (b) friendly summary + final trace ✅ (booked at step 1).
4. If continuing: 📞 Trying Vision Care, then friendly summary
   on that thread, then final trace ❌ or ✅ depending on step 2.
5. Verify: `chain_runs` row has correct `current_step` and
   `status`. Verify: per-step threads have the friendly summary
   (not a second "📞 Trying" message).

**Known limitation verified manually:** kill the bot (`taskkill`)
between step 1 and step 2. Restart. Confirm the chain does NOT
auto-resume. The `chain_runs` row shows `current_step=1,
status='running'` indefinitely until a human intervenes. Document
this in the Session 13 entry's `Verified` section.

## Files touched

- `app/groq_client.py` — `extract_chain` + `_CHAIN_SYSTEM_PROMPT`.
- `app/db.py` — `_CHAIN_SCHEMA` + 4 helpers.
- `app/bot.py` — new states, handlers, executor, HELP_TEXT,
  CommandHandler registration.
- `README.md` — `/chain` example + known limitation.
- `progress.md` — Session 13 entry + Current State + Key Decisions
  + Known Issues.

## Out of scope

- Restart-survival for in-flight chains (see decision 3).
- Per-step alternatives (BOOKING_ALTERNATIVES_INSTRUCTION is only
  appended to step 1's task; steps 2..N use a plain booking
  template). A user can still get a callback confirmation on
  step 1 because it inherits the instruction.
- Mid-chain user cancellation via inline button (user can `/cancel`
  during a step's clarify loop, matching existing flows).
- Reordering the chain mid-flight.
- Conditional fallback expressions like "if X says > $200, call Y"
  — only "try the next clinic" semantics are supported.

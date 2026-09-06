# Plan: `/earliest` — find the earliest available appointment across clinics

## Goal

Add a `/earliest` command that calls several clinics in parallel, asks each for
their **earliest** open slot (no booking), and once every clinic in the batch
finishes, posts a **final summary** sorted soonest-first. Per-clinic results are
still reported individually as each call completes (same UX as `/callaround`).

## What's already in the codebase (do not duplicate)

- `app/groq_client.py:217` — `extract_earliest_details(text)` already exists
  (same shape as `extract_call_around`): `{reason, preferred_date,
  preferred_time, targets[], valid_count, invalid_names, missing_fields}`.
- `app/db.py:13,69-73` — `calls.earliest_available TEXT` column already exists
  and is migrated (added in an earlier session, currently unused).
- `app/bot.py` — full `/callaround` flow to mirror:
  intake (`ca_received`) → follow-up (`ca_followup`) → guided targets
  (`ca_targets_collect` + `ca_what_collect`) → pre-flight plan (`_preflight_and_ask`)
  → clarify loop (`ca_clarify`) → confirm card (`_ask_ca_confirm` + `_ca_confirm_text`)
  → quota check → `_launch_multi_call` → `_ca_single_call` → standard poller
  edits each row's `status_message_id` into a per-clinic result message.
- `app/bot.py:1690` — `_ca_single_call` currently hardcodes
  `task_input = f"Call {name} at {phone}. {what}. {SAFETY_SUFFIX}"`. This is
  the **only** line that must be parameterised so the same function can be
  reused for `/earliest`.
- `app/bot.py:1983` — standard `poll_loop` calls `db.get_running_calls()` with
  no batch filter; per Session 10 it already edits every batch row's thread.

## Decisions resolved

- **Launch path:** reuse `_launch_multi_call` + `_ca_single_call` (zero new
  launch code, zero new poller code). Per-clinic reporting is exactly Mode 1
  (each clinic gets its own 📞 thread edited into a 🏥-labelled friendly
  summary by the existing poller).
- **Task text wording** (final, mirrors CALL-E's own display_goal style from
  earlier test runs — no booking, just gather the answer):

  > `Ask {name} at {phone} when their earliest available appointment slot is
  > for {what}. Do not book — just report the earliest available date and
  > time back. {SAFETY_SUFFIX}`

- **Limits:** reuse `MIN_CA_TARGETS = 2` and `MAX_CA_TARGETS = 5` (already
  imported at module scope — no new constants).
- **Daily quota:** same gate as `/callaround` — each clinic call is one quota
  unit via `db.bump_usage` inside `_ca_single_call`.
- **Batch id prefix:** `"earliest_" + uuid4().hex[:12]` so the watcher can
  scope its queries to `/earliest` batches (leaves `/callaround` untouched).
- **Date parsing:** `dateparser.parse` (already a project dependency,
  imported in `bot.py:10`). `PREFER_DATES_FROM: "future"`, settings= same as
  `_parse_when`. Always parse against a base date of `date.today()` (server
  local) so `2026-08-30` ranks after `2026-08-29`.
- **Unparseable/failed clinics** go to a separate trailing "Couldn't get a
  date from" list — never mixed into the sorted ranking.

## Implementation steps (in dependency order)

### 1. `app/bot.py` — add a `mode` flag to `_ca_single_call` and `_launch_multi_call`

Both currently have the signature
`_ca_single_call(bot, chat_id, batch_id, target, what, language)`. Add a
`mode: str = "book"` keyword and a small dispatch:

```python
def _compose_task_input(mode: str, name: str, phone: str, what: str) -> str:
    if mode == "earliest":
        return (
            f"Ask {name} at {phone} when their earliest available appointment "
            f"slot is for {what}. Do not book — just report the earliest "
            f"available date and time back. {SAFETY_SUFFIX}"
        )
    return f"Call {name} at {phone}. {what}. {SAFETY_SUFFIX}"
```

Replace the hardcoded line in `_ca_single_call` (currently
`task_input = f"Call {name} at {phone}. {what}. {SAFETY_SUFFIX}"`) with
`task_input = _compose_task_input(mode, name, phone, what)`. Thread `mode`
through `_launch_multi_call(... targets, what, language, mode="book")`.

### 2. `app/bot.py` — new conversation states

```python
EARLY_FIRST, EARLY_FOLLOWUP, EARLY_TARGETS, EARLY_WHAT = range(19, 23)
EARLY_CLARIFY, EARLY_CONFIRM, LANG_EARLY = range(23, 26)
```

### 3. `app/bot.py` — new handlers (mirroring `ca_*` exactly)

- `early_entry` — same prompt as `ca_entry` but framed for "earliest slot",
  with `context.user_data["mode"] = "earliest"`. Returns `EARLY_FIRST`.
- `early_received` — calls `groq_client.extract_earliest_details(text)`
  instead of `extract_call_around`. Rest mirrors `ca_received`
  (valid/invalid gate, MIN/MAX cap, single-clinic → redirect to `/call`,
  follow-up on problems, fall through to `_preflight_and_ask`).
- `early_followup` — mirror of `ca_followup` calling
  `extract_earliest_details(combined)`.
- `early_targets_collect` / `early_what_collect` — direct mirrors of
  `ca_targets_collect` / `ca_what_collect` (guided fallback path).
- `early_clarify` — direct mirror of `ca_clarify` (same `sched_clarify_rounds`
  key reuse is fine; both flows share the cap).
- `early_confirm` callback — handles `early_yes` / `early_no` / `early_reduce`.
  On yes: build `batch_id = "earliest_" + uuid.uuid4().hex[:12]`, cap to
  remaining quota, fire
  `asyncio.create_task(_launch_multi_call(bot, chat_id, batch_id, targets, what, language, mode="earliest"))`.
- `on_early_language_selected` — same as the existing `on_language_selected`
  but routes to `EARLY_CONFIRM` state via `pending_confirm["state"]`. Reuse
  `on_language_selected` is fine if it can route on `pending_confirm["state"]`
  (it already does — set the state on the pending payload).

### 4. `app/bot.py` — register the new states and entry point

In `main()`'s `conv_handler`:

- `entry_points`: add `CommandHandler("earliest", early_entry)`.
- `states`: add the new state → handler mappings (mirroring the CA block).
- `LANG_EARLY`: add the language callback handler (same `on_language_selected`
  works because it reads `pending_confirm["state"]`).

Update `HELP_TEXT` to describe `/earliest`.

### 5. `app/bot.py` — batch-completion watcher

Add a second long-running asyncio task started in `post_init` and cancelled
in `post_shutdown`:

```python
async def earliest_watcher_loop(application) -> None:
    bot = application.bot
    logger.info("earliest-batch watcher started")
    while True:
        try:
            await _earliest_watcher_tick(bot)
        except Exception:
            logger.exception("earliest_watcher_tick crashed")
        await asyncio.sleep(POLL_ACTIVE_SECONDS)
```

`_earliest_watcher_tick(bot)`:

1. Query distinct `batch_id`s where `batch_id LIKE 'earliest_%' AND status
   = 'running'` (or status NOT IN terminal set). Add a small DB helper
   `db.get_open_earliest_batches()` that returns `[{batch_id, chat_id}]`.
2. For each open batch, fetch all its rows (a new
   `db.get_batch_calls(batch_id)` helper returns the full row list).
3. If every row is terminal (`status in TERMINAL_STATUSES ∪ {POLL_ERROR}`):
   - For each row, if `status == "COMPLETED"`, attempt to extract
     earliest-available info (see step 6 below) and `dateparser.parse` it.
   - Partition into `(ranked, unranked)` lists.
   - Sort `ranked` by parsed datetime ascending; tiebreak by clinic name.
   - Build a single summary message (max 4000 chars to stay under Telegram's
     limit):

     ```
     📊 Earliest availability results ({n_ranked} ranked, {n_unranked} unranked):

     1. 🏥 Dr Sharma Dental — Mon 1 Sep, 10:00am
        "first opening next Monday at 10am"
     2. 🏥 Vision Care — Wed 3 Sep, 2:30pm
        "Wednesday afternoon is the earliest we have"

     Couldn't get a date from:
     • 🏥 Bright Smiles (no answer)
     • 🏥 Family Dental (declined, no slot given)
     ```

   - `await _safe_send(bot, chat_id, summary)`.
   - Mark the batch closed by inserting a sentinel row or by relying on the
     fact that every child row is now terminal — to avoid re-summarising,
     set a `summarised_at` on each row (cheap `UPDATE calls SET ... WHERE
     batch_id = ?` after sending). Add a column
     `earliest_batch_summarised INTEGER` (default 0) in the migration in
     `app/db.py` (`_migrate`), used as the "haven't summarised yet" gate.
     Re-querying: only summarise batches where at least one row still has
     `earliest_batch_summarised = 0` AND every row is terminal; then update
     that flag to 1 atomically.

### 6. `app/bot.py` — earliest extraction helper

```python
def _extract_earliest_from_result(sc: dict) -> tuple[str | None, str | None]:
    """Return (raw_quoted_text, parsed_datetime) — either may be None."""
    # 1) Prefer structured fields the agent is known to surface:
    #    result.extracted.appointment_date, earliest_available,
    #    earliest_slot, available_at, available_date, next_available.
    # 2) Fall back to scanning result.summary (and sc.summary / post_summary)
    #    with dateparser.parse on the first substring that contains a
    #    plausible date or weekday.
    # Return the raw quoted text for display and the parsed datetime (or
    # None if unparseable) for sorting.
```

Probe order (in priority), since live JSON shape is unknown:
1. `sc["result"]["extracted"]["earliest_available"]`
2. `sc["result"]["extracted"]["earliest_slot"]`
3. `sc["result"]["extracted"]["next_available"]`
4. `sc["result"]["extracted"]["appointment_date"]`
5. `sc["result"]["extracted"]["available_date"]`
6. `sc["result"]["summary"]` / `sc["result"]["post_summary"]` / `sc["summary"]`
   → first substring parseable by `dateparser.parse(..., settings={...})`.

For step 6, use the same dateparser settings as `_parse_when`
(`PREFER_DATES_FROM: "future"`, `RETURN_AS_TIMEZONE_AWARE: True`) but force
`RELATIVE_BASE: datetime.now()` so weekday words like "Monday" land on the
next Monday from today (server local).

**Flag for the live test:** if CALL-E consistently returns the date in a
field other than the ones above (e.g. `available_from`, `slot_at`), update
the probe list in step 6. The plan deliberately over-probes rather than
under-probes so the live test almost certainly surfaces a usable value.

### 7. `app/db.py` — small additions

- New column `earliest_batch_summarised INTEGER NOT NULL DEFAULT 0` on
  `calls`, added in `_migrate` with the same idempotent ALTER pattern as
  the other columns.
- New helper `db.get_open_earliest_batches() -> list[dict]` returning
  `[{batch_id, chat_id}]` for any batch where
  `batch_id LIKE 'earliest_%' AND earliest_batch_summarised = 0`.
- New helper `db.get_batch_calls(batch_id) -> list[dict]` returning the
  full row set (including `clinic_name`, `status`, `earliest_available`).
- New helper `db.mark_earliest_batch_summarised(batch_id) -> None` to
  set `earliest_batch_summarised = 1` on every row in the batch.
- New helper `db.set_earliest_available(run_id, raw_text) -> None` to
  persist a row's earliest-available string (populated lazily by the
  watcher, not by the launch path).

The existing `earliest_available TEXT` column on `calls` (line 13) is the
storage target for the raw text.

### 8. `app/bot.py` — wire watcher into lifecycle

- `post_init`: `application.bot_data["earliest_watcher_task"] =
  asyncio.create_task(earliest_watcher_loop(application))`.
- `post_shutdown`: cancel + await that task the same way the poller is
  handled today.

### 9. Quota accounting

No new accounting — `_ca_single_call` already calls
`db.bump_usage(chat_id, _today_key())` immediately after a successful
`run_call`. The same call runs for both modes, so `/earliest` and
`/callaround` share the daily cap (consistent with README's claim that
the limit is shared across `/call` and `/schedule` — the README will be
amended to mention `/earliest` shares it too).

### 10. Documentation

- `README.md`: add a `/earliest` section after `/callaround` (one short
  example + a "results are not booked" line + the Known-limitation about
  CALL-E not supporting in-flight cancellation — which doesn't apply
  here but is worth a brief "every clinic's call still runs to its end
  and is reported individually" line). Update the "Other commands" /
  "Layout" / env-table sections as appropriate.
- `progress.md`: add Session 11 entry at the top of the Session Log
  following the established pattern (Built / Broke / Fix / Verified /
  Next up).

## Open question for the live test (flagged in plan, deferred from implementation)

The composed task wording (step 1) and the extraction probe list (step 6)
both assume a particular CALL-E response shape that has not been observed
for an "earliest availability" call. **Before declaring Phase 8 done** the
implementer must run a real `/earliest` against at least two test
clinics, capture the raw `result.structuredContent` (🔍 Show raw details
button still works since it just dumps the JSON), and verify:

- The composed task string is interpreted by CALL-E as a clear
  "do not book, just check availability and report earliest slot" request.
  If clinics respond with "I can book you Tuesday 10am" instead of "our
  earliest slot is Tuesday 10am", reword the task to be more explicit
  (e.g. add "tell me only the earliest available date and time — do not
  confirm a booking").
- The `earliest_available` value lives in one of the probed fields, or
  is contained in `result.summary` text that dateparser can extract. If
  not, add the new field name to the probe list and re-run.

Both adjustments are local to the one file (`app/bot.py`).

## Validation

- **Offline stub suite** (extend the existing offline harness style): mock
  `calle_client.plan_call` and `calle_client.run_call` to return a
  controlled set of `run_id`s; mock `calle_client.get_call_status` to
  return three deterministic terminal payloads (one with
  `result.extracted.earliest_available = "Monday 9am"`, one with
  `result.summary = "earliest is Wednesday afternoon"`, one with
  `result.summary = "no openings this week"`); assert the watcher:
  1. Detects the batch is fully terminal.
  2. Parses dates correctly (`Monday 9am` → next Monday 09:00; `Wednesday
     afternoon` → next Wednesday 14:00 default).
  3. Sorts ranked clinics soonest-first.
  4. Lists the unparseable clinic under "Couldn't get a date from".
  5. Sends exactly one summary message.
  6. Does not re-summarise on the next tick (idempotent via
     `earliest_batch_summarised`).
- **Live single-instance test** (per the project rule — always verify one
  instance before testing): run `/earliest` with two real test clinics;
  confirm (a) each clinic's per-clinic result is reported as its own
  message edited by the standard poller, (b) the final summary appears
  once with the right sort order, (c) the "Couldn't get a date from"
  list is populated for any clinic that didn't return a parseable date.
- **Daily-quota test**: with `MAX_CALLS_PER_DAY` set so that remaining
  quota is 1 but N=2 clinics, confirm the standard "Try first 1 clinic"
  flow from `_ask_ca_confirm` runs unmodified (since we reuse the
  confirm-card logic).
- **Bot restart resilience**: confirm the watcher recovers correctly
  after `run_bot_supervised.sh` restarts the process mid-batch (the
  `earliest_batch_summarised` flag must persist in `atc.db`).

## Files touched

- `app/bot.py` — new states, new handlers, parameterise launch, new
  watcher, HELP_TEXT entry, command registration, lifecycle wiring.
- `app/db.py` — one new column migration, three new helpers.
- `README.md` — add `/earliest` section, update command list, mention
  shared quota.
- `progress.md` — Session 11 entry.

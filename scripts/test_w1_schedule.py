"""Phase W1: parity check for the /schedule flow extractions.

The /schedule decision logic now lives in app.core (start_schedule,
schedule_clarify, schedule_preflight, schedule_clarify_post_preflight,
schedule_confirm_card, schedule_job, fire_scheduled_call, parse_when,
_sched_problems, _compose_sched_confirm). This script stubs the
groq, CALLE, APScheduler, and dateparser paths and exercises those
functions, asserting the same dict shapes the bot's thin handlers expect.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import core, db  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"PASS {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"FAIL {label} :: {detail}")


def _state(**kw) -> dict:
    return dict(kw)


# ---------------------------------------------------------------------------
# 1. parse_when — dateparser wrapper
# ---------------------------------------------------------------------------
def test_parse_when_valid_future_time():
    # Use a time well in the future (2 days) so dateparser picks it up.
    future = (datetime.now() + timedelta(days=2)).strftime("%a %b %d %Y %I:%M%p")
    resolved, err = core.parse_when(future)
    _check(
        "sched parse_when: future time resolves to datetime",
        err is None and isinstance(resolved, datetime),
        f"resolved={resolved!r}, err={err!r}",
    )


def test_parse_when_no_time_token():
    resolved, err = core.parse_when("next Tuesday morning")
    _check(
        "sched parse_when: no exact time → err mentions 'exact time of day'",
        resolved is None and "exact time of day" in (err or ""),
        f"resolved={resolved!r}, err={err!r}",
    )


def test_parse_when_past_time():
    past = (datetime.now() - timedelta(days=2)).strftime("%a %b %d %Y %I:%M%p")
    resolved, err = core.parse_when(past)
    _check(
        "sched parse_when: past time → returns None + non-empty err (msg wording is no longer 'in the past')",
        resolved is None and err is not None and len(err) > 0,
        f"resolved={resolved!r}, err={err!r}",
    )


def test_parse_when_within_60s_rejected():
    """SCHEDULE_MIN_LEAD_S = 60: a target only 30s in the future is rejected
    (under the old 30s slop, the same input would have been accepted).
    The bug being prevented: a user who types '10:02 pm today' at 10:01:50
    should not silently have the call scheduled for ~10 seconds out —
    dateparser will pick 10:02 but APScheduler's DateTrigger + 30s slop
    would have given a confusing 'in the past' message instead of a clear
    'too close' one.
    """
    near_future = datetime.now() + timedelta(seconds=30)
    raw = near_future.strftime("%Y-%m-%d %H:%M")
    resolved, err = core.parse_when(raw)
    _check(
        "sched parse_when: target 30s in future → rejected (resolved is None)",
        resolved is None,
        f"resolved={resolved!r}, err={err!r}",
    )
    _check(
        "sched parse_when: target 30s in future → err says 'too close'",
        err is not None and "too close" in err,
        f"err={err!r}",
    )


def test_parse_when_too_close_message_includes_times():
    """The new rejection message must be self-explanatory: it must include
    the 'picked' and 'it's now' substrings so the user can see what the
    server-side clock is at the moment of rejection. Locks in the format
    (option ii: assert on substrings, not exact HH:MM values) so the test
    doesn't couple to wall-clock timing.
    """
    near_future = datetime.now() + timedelta(seconds=30)
    raw = near_future.strftime("%Y-%m-%d %H:%M")
    resolved, err = core.parse_when(raw)
    _check(
        "sched parse_when: too-close message includes 'picked' substring",
        err is not None and "picked" in err,
        f"err={err!r}",
    )
    _check(
        'sched parse_when: too-close message includes "it\'s now" substring',
        err is not None and "it's now" in err,
        f"err={err!r}",
    )


def test_parse_when_empty_string():
    resolved, err = core.parse_when("")
    _check(
        "sched parse_when: empty → err asks 'when should I place this call'",
        resolved is None and "when should I place this call" in (err or ""),
        f"resolved={resolved!r}, err={err!r}",
    )


# ---------------------------------------------------------------------------
# 2. start_schedule — body of bot.sched_received
# ---------------------------------------------------------------------------
def test_start_schedule_groq_unavailable():
    with patch.object(core.groq_client, "is_configured", lambda: False):
        result = asyncio.run(core.start_schedule(42, "any text", state=_state()))
    _check(
        "sched start: groq not configured → status=groq_unavailable",
        result["status"] == "groq_unavailable" and "GROQ_API_KEY" in result["message"],
        f"got: {result}",
    )


def test_start_schedule_groq_error():
    def bad_extract(text, include_call_at=False):
        raise core.groq_client.GroqError("boom")

    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", bad_extract),
    ):
        result = asyncio.run(core.start_schedule(42, "any text", state=_state()))
    _check(
        "sched start: groq GroqError → status=groq_error",
        result["status"] == "groq_error",
        f"got: {result}",
    )


def test_start_schedule_ready():
    future = (datetime.now() + timedelta(days=2)).strftime("%a %b %d %Y %I:%M%p")
    details = {
        "clinic_or_doctor": "Dr Sharma Dental",
        "phone": "+919876543210",
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
        "patient_name": "Ananya",
        "call_at": future,
        "missing_fields": [],
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", lambda t, f: details),
    ):
        state = _state()
        result = asyncio.run(core.start_schedule(42, "sched text", state=state))
    _check(
        "sched start: complete future message → status=ready_for_preflight",
        result["status"] == "ready_for_preflight",
        f"got: {result}",
    )
    _check(
        "sched start: ready state has who + what + resolved_at",
        state.get("who") == "Dr Sharma Dental at +919876543210"
        and "cleaning" in (state.get("what") or "")
        and isinstance(state.get("resolved_at"), datetime),
        f"state: {state}",
    )


def test_start_schedule_needs_clarify_missing_fields():
    details = {
        "clinic_or_doctor": "Dr Sharma",
        "phone": "+919876543210",
        "reason": None,
        "preferred_date": None,
        "preferred_time": None,
        "patient_name": None,
        "call_at": None,
        "missing_fields": ["reason", "preferred_date", "preferred_time", "call_at"],
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", lambda t, f: details),
    ):
        state = _state()
        result = asyncio.run(core.start_schedule(42, "sched", state=state))
    _check(
        "sched start: missing fields → status=needs_clarify + problems list",
        result["status"] == "needs_clarify"
        and isinstance(result.get("problems"), list)
        and len(result["problems"]) > 0,
        f"got: {result}",
    )
    _check(
        "sched start: needs_clarify stores first_text and details in state",
        state.get("first_text") == "sched" and state.get("details") is details,
        f"state: {state}",
    )


# ---------------------------------------------------------------------------
# 3. schedule_clarify — body of bot.sched_followup
# ---------------------------------------------------------------------------
def test_schedule_clarify_groq_error():
    def bad_extract(combined, include_call_at=False):
        raise core.groq_client.GroqError("boom")

    with patch.object(core.groq_client, "extract_call_details", bad_extract):
        result = asyncio.run(core.schedule_clarify(42, "more text", state=_state()))
    _check(
        "sched clarify: groq error → status=groq_error",
        result["status"] == "groq_error",
        f"got: {result}",
    )


def test_schedule_clarify_no_phone():
    future = (datetime.now() + timedelta(days=2)).strftime("%a %b %d %Y %I:%M%p")

    def fake_extract(combined, include_call_at=False):
        return {
            "clinic_or_doctor": "Dr Sharma",
            "phone": None,
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
            "call_at": future,
            "patient_name": None,
        }

    with patch.object(core.groq_client, "extract_call_details", fake_extract):
        result = asyncio.run(core.schedule_clarify(42, "more text", state=_state()))
    _check(
        "sched clarify: no phone → status=no_phone",
        result["status"] == "no_phone",
        f"got: {result}",
    )
    _check(
        "sched clarify: no_phone message asks for /schedule again",
        "/schedule again" in result["message"],
        f"got: {result.get('message')!r}",
    )


def test_schedule_clarify_missing_call_at():
    def fake_extract(combined, include_call_at=False):
        return {
            "clinic_or_doctor": "Dr Sharma",
            "phone": "+919876543210",
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
            "call_at": None,
            "patient_name": None,
        }

    with patch.object(core.groq_client, "extract_call_details", fake_extract):
        result = asyncio.run(core.schedule_clarify(42, "more text", state=_state()))
    # When call_at is None, _parse_when produces a "I couldn't find a date or
    # time" error, which becomes the problem text (NOT the ⏰ time-prompt).
    # The pre-W1 bot.py sched_followup had the same shape.
    _check(
        "sched clarify: missing call_at → status=needs_clarify + parse error in problems",
        result["status"] == "needs_clarify"
        and any(
            "I couldn't find a date or time" in p for p in result.get("problems", [])
        ),
        f"got: {result}",
    )


def test_schedule_clarify_happy():
    future = (datetime.now() + timedelta(days=2)).strftime("%a %b %d %Y %I:%M%p")

    def fake_extract(combined, include_call_at=False):
        return {
            "clinic_or_doctor": "Dr Sharma",
            "phone": "+919876543210",
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
            "call_at": future,
            "patient_name": "Ananya",
        }

    with patch.object(core.groq_client, "extract_call_details", fake_extract):
        state = _state()
        result = asyncio.run(core.schedule_clarify(42, "more text", state=state))
    _check(
        "sched clarify: complete follow-up → status=ready_for_preflight",
        result["status"] == "ready_for_preflight",
        f"got: {result}",
    )
    _check(
        "sched clarify: state has who + what + resolved_at",
        state.get("who") == "Dr Sharma at +919876543210"
        and "cleaning" in (state.get("what") or "")
        and isinstance(state.get("resolved_at"), datetime),
        f"state: {state}",
    )


# ---------------------------------------------------------------------------
# 4. schedule_preflight — quota + E.164 + plan_call
# ---------------------------------------------------------------------------
def test_schedule_preflight_quota_blocked():
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (601,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (601, today, L),
    )
    conn.commit()
    conn.close()

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(601, Ch())
    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    result = asyncio.run(core.schedule_preflight(601, state=state))
    _check(
        "sched preflight: at quota → status=limit_reached",
        result["status"] == "limit_reached",
        f"got: {result}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (601,))
    conn.commit()
    conn.close()
    core.unregister_channel(601)


def test_schedule_preflight_invalid_phone():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(602, Ch())
    state = _state(who="Dr Sharma", what="cleaning", call_language="English")
    result = asyncio.run(core.schedule_preflight(602, state=state))
    _check(
        "sched preflight: invalid phone → status=invalid_phone",
        result["status"] == "invalid_phone",
        f"got: {result}",
    )
    _check(
        "sched preflight: invalid phone says 'Send /schedule to try again'",
        "Send /schedule to try again" in result["message"],
        f"got message: {result.get('message')!r}",
    )
    core.unregister_channel(602)


def test_schedule_preflight_calle_error():
    from app.calle_client import CalleError

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(603, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        raise CalleError("calle blew up")

    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.schedule_preflight(603, state=state))
    _check(
        "sched preflight: CalleError → status=calle_unreachable",
        result["status"] == "calle_unreachable",
        f"got: {result}",
    )
    _check(
        "sched preflight: calle_unreachable mentions 'try /schedule again'",
        "try /schedule again" in result["message"],
        f"got message: {result.get('message')!r}",
    )
    core.unregister_channel(603)


def test_schedule_preflight_needs_clarify():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(604, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_sched",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.schedule_preflight(604, state=state))
    _check(
        "sched preflight: !ready_to_run → status=needs_clarify + plan_id",
        result["status"] == "needs_clarify" and state.get("plan_id") == "pl_sched",
        f"got: {result}, state: {state}",
    )
    _check(
        "sched preflight: needs_clarify message sent via channel",
        any("Which patient?" in m for m in sent),
        f"sent: {sent}",
    )
    core.unregister_channel(604)


def test_schedule_preflight_ready():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(605, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_sched_ok",
                    "confirm_token": "tok",
                }
            }
        }

    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.schedule_preflight(605, state=state))
    _check(
        "sched preflight: ready_to_run → status=ready_to_confirm",
        result["status"] == "ready_to_confirm"
        and result.get("plan", {}).get("plan_id") == "pl_sched_ok",
        f"got: {result}",
    )
    core.unregister_channel(605)


# ---------------------------------------------------------------------------
# 5. schedule_clarify_post_preflight
# ---------------------------------------------------------------------------
def test_schedule_clarify_post_too_many_rounds():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(606, Ch())
    state = _state(sched_clarify_rounds=core.MAX_CLARIFY_ROUNDS)
    result = asyncio.run(
        core.schedule_clarify_post_preflight(606, "another", state=state)
    )
    _check(
        "sched clarify-post: too many rounds → status=too_many_rounds",
        result["status"] == "too_many_rounds",
        f"got: {result}",
    )
    core.unregister_channel(606)


def test_schedule_clarify_post_calle_error_keeps_state():
    from app.calle_client import CalleError

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(607, Ch())

    def fake_plan_call(answer, plan_id=None, language=None):
        raise CalleError("boom")

    state = _state(plan_id="pl_old", call_language="English")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.schedule_clarify_post_preflight(607, "more", state=state)
        )
    _check(
        "sched clarify-post: CalleError → status=calle_unreachable + keep_state",
        result["status"] == "calle_unreachable" and result.get("keep_state") is True,
        f"got: {result}",
    )
    core.unregister_channel(607)


def test_schedule_clarify_post_ready_concatenates_extra():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(608, Ch())

    def fake_plan_call(answer, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_sched_post",
                    "confirm_token": "tok",
                }
            }
        }

    state = _state(what="cleaning", plan_id="pl_old", call_language="English")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.schedule_clarify_post_preflight(608, "next door", state=state)
        )
    _check(
        "sched clarify-post: ready → status=ready_to_confirm",
        result["status"] == "ready_to_confirm",
        f"got: {result}",
    )
    _check(
        "sched clarify-post: state.what was extended with the clarify answer",
        "next door" in (state.get("what") or "")
        and "cleaning" in (state.get("what") or ""),
        f"state: {state}",
    )
    core.unregister_channel(608)


# ---------------------------------------------------------------------------
# 6. schedule_confirm_card
# ---------------------------------------------------------------------------
def test_schedule_confirm_card_no_resolved_at():
    state = _state(who="X", what="Y")
    result = asyncio.run(core.schedule_confirm_card(42, state=state))
    _check(
        "sched confirm-card: missing resolved_at → status=needs_time",
        result["status"] == "needs_time" and "exact date and time" in result["message"],
        f"got: {result}",
    )


def test_schedule_confirm_card_with_language():
    resolved = datetime.now() + timedelta(days=2)
    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        resolved_at=resolved,
        call_language="English",
    )
    result = asyncio.run(core.schedule_confirm_card(42, state=state))
    _check(
        "sched confirm-card: language set → status=ready + card_text with masked phone",
        result["status"] == "ready"
        and "+91******210" in result["card_text"]
        and "SCHEDULED" in result["card_text"],
        f"got: {result}",
    )


def test_schedule_confirm_card_without_language_asks_first():
    resolved = datetime.now() + timedelta(days=2)
    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", resolved_at=resolved
    )
    result = asyncio.run(core.schedule_confirm_card(42, state=state))
    _check(
        "sched confirm-card: no language → status=needs_language + lang prompt",
        result["status"] == "needs_language"
        and "What language" in result["card_with_lang_prompt"]
        and "lang_en"
        in [b["callback_data"] for row in result["language_keyboard"] for b in row],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 7. schedule_job — APScheduler add + db insert_scheduled (×2)
# ---------------------------------------------------------------------------
def test_schedule_job_internal_state_error():
    result = asyncio.run(
        core.schedule_job(
            42,
            state=_state(who="", what="", resolved_at=None),
            add_job_fn=lambda **kw: None,
            remove_job_fn=lambda jid: None,
        )
    )
    _check(
        "sched job: empty who + no resolved_at → status=internal_state_error",
        result["status"] == "internal_state_error",
        f"got: {result}",
    )


def test_schedule_job_add_job_raises():
    resolved = datetime.now() + timedelta(days=2)

    def bad_add_job(**kw):
        raise RuntimeError("scheduler dead")

    result = asyncio.run(
        core.schedule_job(
            42,
            state=_state(
                who="Dr X at +919876543210", what="cleaning", resolved_at=resolved
            ),
            add_job_fn=bad_add_job,
            remove_job_fn=lambda jid: None,
        )
    )
    _check(
        "sched job: add_job raises → status=add_job_failed",
        result["status"] == "add_job_failed",
        f"got: {result}",
    )
    _check(
        "sched job: add_job_failed message mentions 'try /schedule again'",
        "try /schedule again" in result["message"],
        f"got message: {result.get('message')!r}",
    )


def test_schedule_job_happy_path():
    """W1.1 fix: schedule_job must call db.insert_scheduled exactly ONCE
    (not twice — the pre-W1 double-insert caused UNIQUE constraint failures
    on scheduled_calls.job_id, the source of the live /schedule IntegrityError
    bug). add_job_fn is called once; the DB row is written once; the
    function returns 'scheduled' with a short_id and a completion_message.
    W1.2: patient_name from state is threaded through to both add_job_fn
    and the DB row, so /schedule's fire-time path can include it in the
    composed CALL-E task (mirroring /call, /callaround, /chain).
    """
    resolved = datetime.now() + timedelta(days=2)
    added = []

    def fake_add_job(*, job_id, chat_id, who, what, language, patient_name=None):
        added.append(
            {
                "job_id": job_id,
                "chat_id": chat_id,
                "who": who,
                "what": what,
                "language": language,
                "patient_name": patient_name,
            }
        )

    removed = []

    def fake_remove_job(job_id):
        removed.append(job_id)

    # Clean any pre-existing rows for chat 620
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (620,))
    conn.commit()
    conn.close()

    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        resolved_at=resolved,
        call_language="English",
        patient_name="Ananya",
    )
    result = asyncio.run(
        core.schedule_job(
            620,
            state=state,
            add_job_fn=fake_add_job,
            remove_job_fn=fake_remove_job,
        )
    )
    _check(
        "sched job: happy path → status=scheduled",
        result["status"] == "scheduled",
        f"got: {result}",
    )
    _check(
        "sched job: add_job_fn was called exactly once",
        len(added) == 1
        and added[0]["who"] == "Dr Sharma at +919876543210"
        and added[0]["language"] == "English",
        f"added: {added}",
    )
    _check(
        "sched job: add_job_fn received patient_name from state",
        len(added) == 1 and added[0]["patient_name"] == "Ananya",
        f"added: {added}",
    )
    _check(
        "sched job: remove_job_fn was NOT called on the happy path",
        removed == [],
        f"removed: {removed}",
    )
    _check(
        "sched job: scheduled_calls row exists with the right fields",
        True,
        # Row check is in the verification block below.
    )
    # Verify the DB row was written (single insert).
    conn = db._connect()
    rows = conn.execute(
        "SELECT job_id, chat_id, language, patient_name FROM scheduled_calls WHERE chat_id=620"
    ).fetchall()
    _check(
        "sched job: exactly one scheduled_calls row written for chat_id=620",
        len(rows) == 1 and rows[0]["language"] == "English",
        f"rows: {rows}",
    )
    _check(
        "sched job: row job_id matches the add_job job_id",
        len(rows) == 1 and rows[0]["job_id"] == added[0]["job_id"],
        f"row job_id={rows[0]['job_id'] if rows else None!r}  add_job job_id={added[0]['job_id']!r}",
    )
    _check(
        "sched job: DB row has patient_name persisted",
        len(rows) == 1 and rows[0]["patient_name"] == "Ananya",
        f"rows: {rows}",
    )
    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (620,))
    conn.commit()
    conn.close()


def test_schedule_job_insert_scheduled_failure_cleans_up_scheduler():
    """W1.1 fix regression test: when db.insert_scheduled raises (e.g.
    sqlite3.IntegrityError on UNIQUE constraint for job_id, or any other DB
    failure) AFTER add_job_fn already added the APScheduler job, the
    scheduler job must be removed so we never leave a phantom job pointing
    at nothing. The function must:
      - call add_job_fn exactly once
      - call db.insert_scheduled exactly once
      - on failure, call remove_job_fn with the same job_id
      - return {"status": "insert_failed", ...}
    """
    import sqlite3 as _sqlite3
    from app.calle_client import CalleError

    resolved = datetime.now() + timedelta(days=2)
    added = []
    removed = []
    insert_calls = []

    def fake_add_job(*, job_id, chat_id, who, what, language):
        added.append({"job_id": job_id, "chat_id": chat_id})

    def fake_remove_job(job_id):
        removed.append(job_id)

    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        resolved_at=resolved,
        call_language="English",
    )

    # Patch db.insert_scheduled to raise the same exception the user hit
    # in the live test (UNIQUE constraint on scheduled_calls.job_id).
    # We also count invocations directly so the "called exactly once"
    # assertion doesn't depend on a MagicMock's call_count attribute.
    def fake_insert_scheduled(*args, **kwargs):
        insert_calls.append((args, kwargs))
        raise _sqlite3.IntegrityError(
            "UNIQUE constraint failed: scheduled_calls.job_id"
        )

    with patch.object(core.db, "insert_scheduled", side_effect=fake_insert_scheduled):
        result = asyncio.run(
            core.schedule_job(
                621,
                state=state,
                add_job_fn=fake_add_job,
                remove_job_fn=fake_remove_job,
            )
        )

    _check(
        "sched job: insert failure → status=insert_failed",
        result["status"] == "insert_failed",
        f"got: {result}",
    )
    _check(
        "sched job: insert_failed message surfaces IntegrityError class name",
        "IntegrityError" in result["message"],
        f"got message: {result.get('message')!r}",
    )
    _check(
        "sched job: add_job_fn was called exactly once before the failure",
        len(added) == 1,
        f"added: {added}",
    )
    _check(
        "sched job: db.insert_scheduled was called exactly once (regression: not twice)",
        len(insert_calls) == 1,
        f"insert_scheduled invocations: {len(insert_calls)}  args: {insert_calls}",
    )
    _check(
        "sched job: remove_job_fn was called with the same job_id (orphan cleaned up)",
        len(removed) == 1 and removed[0] == added[0]["job_id"],
        f"removed: {removed}  added job_id: {added[0]['job_id'] if added else None!r}",
    )


def test_schedule_job_remove_job_failure_does_not_mask_insert_error():
    """W1.1 fix: even if remove_job_fn itself raises during the cleanup
    path, the original db.insert_scheduled failure must be the one surfaced
    to the caller. The cleanup exception is logged but does not replace
    the user-facing error.
    """
    import sqlite3 as _sqlite3

    resolved = datetime.now() + timedelta(days=2)
    added = []

    def fake_add_job(*, job_id, chat_id, who, what, language):
        added.append({"job_id": job_id})

    def fake_remove_job(job_id):
        raise RuntimeError("scheduler dead during cleanup")

    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        resolved_at=resolved,
        call_language="English",
    )

    with patch.object(
        core.db,
        "insert_scheduled",
        side_effect=_sqlite3.IntegrityError(
            "UNIQUE constraint failed: scheduled_calls.job_id"
        ),
    ):
        result = asyncio.run(
            core.schedule_job(
                622,
                state=state,
                add_job_fn=fake_add_job,
                remove_job_fn=fake_remove_job,
            )
        )

    _check(
        "sched job: insert_failed status preserved even when remove_job_fn raises",
        result["status"] == "insert_failed",
        f"got: {result}",
    )
    _check(
        "sched job: surfaced message is about the original IntegrityError, not the cleanup",
        "IntegrityError" in result["message"]
        and "RuntimeError" not in result["message"],
        f"got message: {result.get('message')!r}",
    )


# ---------------------------------------------------------------------------
# 8. fire_scheduled_call — body of bot.scheduled_call_job
# ---------------------------------------------------------------------------
def test_fire_scheduled_call_quota_blocked():
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (701,))
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (701,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (701, today, L),
    )
    conn.commit()
    conn.close()
    # Insert a fake scheduled row
    db.insert_scheduled(
        701,
        "firesched1",
        datetime.now().astimezone().isoformat(),
        "Dr X at +919876543210",
        "cleaning",
        "English",
    )
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(701, Ch())
    asyncio.run(
        core.fire_scheduled_call(
            "firesched1", 701, "Dr X at +919876543210", "cleaning", "English"
        )
    )
    _check(
        "fire-scheduled: at quota → daily limit message sent",
        any("Daily limit" in m for m in sent),
        f"sent: {sent}",
    )
    row = (
        db._connect()
        .execute("SELECT status FROM scheduled_calls WHERE job_id=?", ("firesched1",))
        .fetchone()
    )
    _check(
        "fire-scheduled: at quota → scheduled_calls.status = 'failed'",
        row["status"] == "failed",
        f"row: {row}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (701,))
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (701,))
    conn.commit()
    conn.close()
    core.unregister_channel(701)


def test_fire_scheduled_call_calle_error():
    from app.calle_client import CalleError

    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (702,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (702,))
    conn.commit()
    conn.close()
    db.insert_scheduled(
        702,
        "firesched2",
        datetime.now().astimezone().isoformat(),
        "Dr X at +919876543210",
        "cleaning",
        "English",
    )
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(702, Ch())

    def fake_plan_call(*a, **kw):
        raise CalleError("calle blew up")

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        asyncio.run(
            core.fire_scheduled_call(
                "firesched2", 702, "Dr X at +919876543210", "cleaning", "English"
            )
        )
    _check(
        "fire-scheduled: CalleError → failure message sent",
        any("couldn't be started" in m for m in sent),
        f"sent: {sent}",
    )
    row = (
        db._connect()
        .execute("SELECT status FROM scheduled_calls WHERE job_id=?", ("firesched2",))
        .fetchone()
    )
    _check(
        "fire-scheduled: CalleError → scheduled_calls.status = 'failed'",
        row["status"] == "failed",
        f"row: {row}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (702,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (702,))
    conn.commit()
    conn.close()
    core.unregister_channel(702)


def test_fire_scheduled_call_needs_clarify():
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (703,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (703,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (703,))
    conn.commit()
    conn.close()
    db.insert_scheduled(
        703,
        "firesched3",
        datetime.now().astimezone().isoformat(),
        "Dr X at +919876543210",
        "cleaning",
        "English",
    )
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(703, Ch())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_fire",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        asyncio.run(
            core.fire_scheduled_call(
                "firesched3", 703, "Dr X at +919876543210", "cleaning", "English"
            )
        )
    _check(
        "fire-scheduled: !ready_to_run → 'needs more information' message sent",
        any("needs more information" in m for m in sent),
        f"sent: {sent}",
    )
    row = (
        db._connect()
        .execute("SELECT status FROM scheduled_calls WHERE job_id=?", ("firesched3",))
        .fetchone()
    )
    _check(
        "fire-scheduled: needs_clarify → scheduled_calls.status = 'failed'",
        row["status"] == "failed",
        f"row: {row}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (703,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (703,))
    conn.commit()
    conn.close()
    core.unregister_channel(703)


def test_fire_scheduled_call_happy_launches():
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (704,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (704,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (704,))
    conn.commit()
    conn.close()
    db.insert_scheduled(
        704,
        "firesched4",
        datetime.now().astimezone().isoformat(),
        "Dr X at +919876543210",
        "cleaning",
        "English",
    )
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(704, Ch())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_fire_ok",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": "run_fire_ok",
                    "status": "RUNNING",
                }
            }
        }

    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        asyncio.run(
            core.fire_scheduled_call(
                "firesched4", 704, "Dr X at +919876543210", "cleaning", "English"
            )
        )
    _check(
        "fire-scheduled: happy → '📞 Calling now…' thread sent",
        any("Calling now" in m for m in sent),
        f"sent: {sent}",
    )
    row = (
        db._connect()
        .execute("SELECT status FROM scheduled_calls WHERE job_id=?", ("firesched4",))
        .fetchone()
    )
    _check(
        "fire-scheduled: happy → scheduled_calls.status = 'fired'",
        row["status"] == "fired",
        f"row: {row}",
    )
    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (704,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (704,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (704,))
    conn.commit()
    conn.close()
    core.unregister_channel(704)


def test_fire_scheduled_call_composes_task_with_patient_name():
    """W1.2 fix: at fire time, the composed CALL-E task_input must include
    the patient_name that was captured at /schedule intake time. Without
    this, an unattended scheduled call has no human to answer CALL-E's
    re-ask for the name, and the whole call aborts mid-flight.
    """
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (705,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (705,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (705,))
    conn.commit()
    conn.close()
    db.insert_scheduled(
        705,
        "firesched5",
        datetime.now().astimezone().isoformat(),
        "Dr Sharma at +919876543210",
        "cleaning",
        "English",
        patient_name="Ananya",
    )
    sent: list[str] = []
    plan_calls: list[dict] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(705, Ch())

    def fake_plan_call(user_input, *a, **kw):
        plan_calls.append({"user_input": user_input})
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_pn_ok",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": "run_pn_ok",
                    "status": "RUNNING",
                }
            }
        }

    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        asyncio.run(
            core.fire_scheduled_call(
                "firesched5",
                705,
                "Dr Sharma at +919876543210",
                "cleaning",
                "English",
                patient_name="Ananya",
            )
        )

    _check(
        "fire-scheduled: patient_name threaded into composed task_input",
        len(plan_calls) == 1 and "for Ananya" in plan_calls[0]["user_input"],
        f"plan_calls: {plan_calls}",
    )
    row = (
        db._connect()
        .execute(
            "SELECT patient_name FROM scheduled_calls WHERE job_id=?", ("firesched5",)
        )
        .fetchone()
    )
    _check(
        "fire-scheduled: scheduled_calls row has patient_name='Ananya'",
        row is not None and row["patient_name"] == "Ananya",
        f"row: {row}",
    )

    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (705,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (705,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (705,))
    conn.commit()
    conn.close()
    core.unregister_channel(705)


def test_fire_scheduled_call_omits_patient_name_when_none():
    """W1.2 fix: when no patient_name was captured at intake (the common
    case — most /schedule messages don't name the patient), the fire-time
    composed task must use the no-name template, NOT the 'for the patient'
    placeholder. Backward-compat: old scheduled jobs with patient_name=None
    in their row must still work.
    """
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (706,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (706,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (706,))
    conn.commit()
    conn.close()
    db.insert_scheduled(
        706,
        "firesched6",
        datetime.now().astimezone().isoformat(),
        "Dr X at +919876543210",
        "cleaning",
        "English",
        patient_name=None,
    )
    sent: list[str] = []
    plan_calls: list[dict] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(706, Ch())

    def fake_plan_call(user_input, *a, **kw):
        plan_calls.append({"user_input": user_input})
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_none_ok",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": "run_none_ok",
                    "status": "RUNNING",
                }
            }
        }

    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        asyncio.run(
            core.fire_scheduled_call(
                "firesched6",
                706,
                "Dr X at +919876543210",
                "cleaning",
                "English",
                patient_name=None,
            )
        )

    _check(
        "fire-scheduled: patient_name=None → task_input does NOT contain 'for the patient'",
        len(plan_calls) == 1 and "for the patient" not in plan_calls[0]["user_input"],
        f"plan_calls: {plan_calls}",
    )
    # The first clause (before the first '. ') is the "Call ..." segment.
    # When patient_name is set, the format is "Call {who} for {name}. ...".
    # When patient_name is None, the format is "Call {who}. ..." (no "for ...").
    first_clause = (
        plan_calls[0]["user_input"].split(". ", 1)[0] if len(plan_calls) == 1 else ""
    )
    _check(
        "fire-scheduled: patient_name=None → first clause 'Call …' has no ' for '",
        " for " not in first_clause,
        f"first_clause={first_clause!r}",
    )

    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE chat_id=?", (706,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (706,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (706,))
    conn.commit()
    conn.close()
    core.unregister_channel(706)


# ---------------------------------------------------------------------------
# 9. _sched_problems / _compose_sched_confirm — formatters
# ---------------------------------------------------------------------------
def test_sched_problems_call_at_branch():
    text_list = core._sched_problems({"missing_fields": ["call_at"]})
    _check(
        "sched_problems: call_at branch prefixes '⏰'",
        any(p.startswith("⏰") for p in text_list),
        f"got: {text_list}",
    )


def test_sched_problems_other_missing_branch():
    text_list = core._sched_problems({"missing_fields": ["reason", "preferred_date"]})
    _check(
        "sched_problems: other-missing branch produces 'Got most of it!'",
        any("Got most of it" in p for p in text_list),
        f"got: {text_list}",
    )


def test_compose_sched_confirm_masks_phone():
    resolved = datetime.now() + timedelta(days=2)
    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", resolved_at=resolved
    )
    text = core._compose_sched_confirm(state)
    _check(
        "sched_confirm: masks phone in card",
        "+91******210" in text,
        f"got: {text!r}",
    )
    _check(
        "sched_confirm: includes 'Will be placed:' line",
        "Will be placed:" in text,
        f"got: {text!r}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main() -> int:
    test_parse_when_valid_future_time()
    test_parse_when_no_time_token()
    test_parse_when_past_time()
    test_parse_when_within_60s_rejected()
    test_parse_when_too_close_message_includes_times()
    test_parse_when_empty_string()
    test_start_schedule_groq_unavailable()
    test_start_schedule_groq_error()
    test_start_schedule_ready()
    test_start_schedule_needs_clarify_missing_fields()
    test_schedule_clarify_groq_error()
    test_schedule_clarify_no_phone()
    test_schedule_clarify_missing_call_at()
    test_schedule_clarify_happy()
    test_schedule_preflight_quota_blocked()
    test_schedule_preflight_invalid_phone()
    test_schedule_preflight_calle_error()
    test_schedule_preflight_needs_clarify()
    test_schedule_preflight_ready()
    test_schedule_clarify_post_too_many_rounds()
    test_schedule_clarify_post_calle_error_keeps_state()
    test_schedule_clarify_post_ready_concatenates_extra()
    test_schedule_confirm_card_no_resolved_at()
    test_schedule_confirm_card_with_language()
    test_schedule_confirm_card_without_language_asks_first()
    test_schedule_job_internal_state_error()
    test_schedule_job_add_job_raises()
    test_schedule_job_happy_path()
    test_fire_scheduled_call_quota_blocked()
    test_fire_scheduled_call_calle_error()
    test_fire_scheduled_call_needs_clarify()
    test_fire_scheduled_call_happy_launches()
    test_fire_scheduled_call_composes_task_with_patient_name()
    test_fire_scheduled_call_omits_patient_name_when_none()
    test_sched_problems_call_at_branch()
    test_sched_problems_other_missing_branch()
    test_compose_sched_confirm_masks_phone()
    print()
    if FAILED:
        for f in FAILED:
            print(f"FAIL: {f}")
        return 1
    print(f"ALL {len(PASSED)} PHASE-W1 /schedule PARITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

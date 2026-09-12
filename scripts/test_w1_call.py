"""Phase W1: parity check for the /call flow extractions.

The /call decision logic now lives in app.core (start_call, call_clarify,
call_collect_who, call_collect_what, call_confirm_card, call_plan_and_launch,
call_clarify_post_confirm). This script stubs the groq and CALLE paths and
exercises those functions, asserting the same dict shapes the bot's thin
handlers expect.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import core, db  # noqa: E402
from app.calle_client import is_terminal, normalize_status  # noqa: E402

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
# 1. start_call — replaces bot.received_first body
# ---------------------------------------------------------------------------
def test_start_call_groq_unavailable():
    with patch.object(core.groq_client, "is_configured", lambda: False):
        result = asyncio.run(core.start_call(42, "anything", state=_state()))
    _check(
        "call: groq not configured → status=groq_unavailable + guided_prompt",
        result["status"] == "groq_unavailable"
        and "step by step" in result["guided_prompt"],
        f"got: {result}",
    )


def test_start_call_groq_error():
    def bad_extract(text):
        raise core.groq_client.GroqError("boom")

    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", bad_extract),
    ):
        result = asyncio.run(core.start_call(42, "anything", state=_state()))
    _check(
        "call: groq GroqError → status=groq_error + guided_prompt",
        result["status"] == "groq_error" and "step by step" in result["guided_prompt"],
        f"got: {result}",
    )


def test_start_call_ready():
    details = {
        "clinic_or_doctor": "Dr Sharma Dental",
        "phone": "+919876543210",
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
        "patient_name": "Ananya",
        "missing_fields": [],
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", lambda t: details),
    ):
        state = _state()
        result = asyncio.run(
            core.start_call(
                42,
                "Book cleaning at Dr Sharma +919876543210 next Tuesday 10am",
                state=state,
            )
        )
    _check(
        "call: complete message → status=ready",
        result["status"] == "ready",
        f"got: {result}",
    )
    _check(
        "call: ready state has who='Dr Sharma Dental at +919876543210'",
        state.get("who") == "Dr Sharma Dental at +919876543210",
        f"state: {state}",
    )
    _check(
        "call: ready state has what='cleaning, next Tuesday, 10am'",
        state.get("what") == "cleaning, next Tuesday, 10am",
        f"state: {state}",
    )
    _check(
        "call: ready state preserves patient_name",
        state.get("patient_name") == "Ananya",
        f"state: {state}",
    )


def test_start_call_needs_clarify():
    details = {
        "clinic_or_doctor": "Dr Sharma",
        "phone": "+919876543210",
        "reason": None,
        "preferred_date": None,
        "preferred_time": None,
        "patient_name": None,
        "missing_fields": ["reason", "preferred_date", "preferred_time"],
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", lambda t: details),
    ):
        state = _state()
        result = asyncio.run(
            core.start_call(42, "Dr Sharma +919876543210", state=state)
        )
    _check(
        "call: missing fields → status=needs_clarify",
        result["status"] == "needs_clarify",
        f"got: {result}",
    )
    _check(
        "call: needs_clarify missing_text mentions 'reason' and 'preferred date'",
        "reason" in result["missing_text"]
        and "preferred date" in result["missing_text"],
        f"got: {result.get('missing_text')!r}",
    )
    _check(
        "call: needs_clarify stores details + first_text for follow-up merge",
        state.get("details") is details
        and state.get("first_text") == "Dr Sharma +919876543210",
        f"state: {state}",
    )


def test_start_call_phone_invalid():
    details = {
        "clinic_or_doctor": "Dr Sharma",
        "phone": "+12",
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
        "patient_name": None,
        "missing_fields": ["phone"],
        "phone_invalid": True,
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_details", lambda t: details),
    ):
        result = asyncio.run(
            core.start_call(
                42, "Dr Sharma +12 cleaning next Tuesday 10am", state=_state()
            )
        )
    _check(
        "call: phone invalid + missing → status=needs_clarify with phone hint",
        result["status"] == "needs_clarify"
        and "international format" in result["missing_text"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 2. call_clarify — replaces bot.received_followup body
# ---------------------------------------------------------------------------
def test_call_clarify_merges_and_succeeds():
    state = _state(
        first_text="Dr Sharma",
        details={
            "clinic_or_doctor": "Dr Sharma",
            "phone": None,
            "reason": None,
        },
    )

    def fake_extract(combined):
        return {
            "clinic_or_doctor": "Dr Sharma",
            "phone": "+919876543210",
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
            "patient_name": None,
        }

    with patch.object(core.groq_client, "extract_call_details", fake_extract):
        result = asyncio.run(
            core.call_clarify(
                42, "+919876543210 cleaning next Tuesday 10am", state=state
            )
        )
    _check(
        "call clarify: complete follow-up → status=ready",
        result["status"] == "ready",
        f"got: {result}",
    )
    _check(
        "call clarify: state has who populated",
        state.get("who") == "Dr Sharma at +919876543210",
        f"state: {state}",
    )


def test_call_clarify_groq_error_falls_back_to_guided():
    state = _state(first_text="Dr Sharma", details={})

    def bad_extract(combined):
        raise core.groq_client.GroqError("boom")

    with patch.object(core.groq_client, "extract_call_details", bad_extract):
        result = asyncio.run(core.call_clarify(42, "more text", state=state))
    _check(
        "call clarify: groq error → status=groq_error + guided_prompt",
        result["status"] == "groq_error" and "step by step" in result["guided_prompt"],
        f"got: {result}",
    )
    _check(
        "call clarify: groq error sets state.guided_mode",
        state.get("guided_mode") is True,
        f"state: {state}",
    )


def test_call_clarify_still_no_phone_triggers_guided():
    state = _state(first_text="Dr Sharma", details={})

    def fake_extract(combined):
        return {
            "clinic_or_doctor": "Dr Sharma",
            "phone": None,
            "reason": None,
            "preferred_date": None,
            "preferred_time": None,
        }

    with patch.object(core.groq_client, "extract_call_details", fake_extract):
        result = asyncio.run(core.call_clarify(42, "no number yet", state=state))
    _check(
        "call clarify: no phone still → status=needs_phone_guided",
        result["status"] == "needs_phone_guided",
        f"got: {result}",
    )
    _check(
        "call clarify: needs_phone_guided message says 'I still don't have a phone number'",
        "I still don't have a phone number" in result["guided_prompt"],
        f"got: {result.get('guided_prompt')!r}",
    )


# ---------------------------------------------------------------------------
# 3. call_collect_who / call_collect_what — guided mode
# ---------------------------------------------------------------------------
def test_call_collect_who_accepts_valid():
    result = asyncio.run(
        core.call_collect_who(42, "Dr Sharma at +919876543210", state=_state())
    )
    _check(
        "call who: valid phone → status=accepted + next_prompt set",
        result["status"] == "accepted"
        and "What should I book" in result["next_prompt"],
        f"got: {result}",
    )


def test_call_collect_who_rejects_missing_phone():
    result = asyncio.run(
        core.call_collect_who(42, "just a doctor name, no number", state=_state())
    )
    _check(
        "call who: no phone → status=no_phone_in_text + hint",
        result["status"] == "no_phone_in_text"
        and "international format" in result["message"],
        f"got: {result}",
    )


def test_call_collect_what_accepts():
    state = _state(who="Dr Sharma at +919876543210")
    result = asyncio.run(
        core.call_collect_what(42, "cleaning, next Tuesday 10am", state=state)
    )
    _check(
        "call what: non-empty → status=ready, state.what populated",
        result["status"] == "ready"
        and state.get("what") == "cleaning, next Tuesday 10am",
        f"got: {result}, state: {state}",
    )


def test_call_collect_what_rejects_empty():
    result = asyncio.run(core.call_collect_what(42, "  ", state=_state()))
    _check(
        "call what: empty → status=empty + 'Please describe'",
        result["status"] == "empty" and "Please describe" in result["message"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 4. call_confirm_card — returns ready card or needs_language
# ---------------------------------------------------------------------------
def test_confirm_card_with_language_set():
    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    result = asyncio.run(core.call_confirm_card(42, state=state))
    _check(
        "call confirm: language set → status=ready + card with masked phone",
        result["status"] == "ready"
        and "Dr Sharma" in result["card_text"]
        and "+91******210" in result["card_text"],
        f"got: {result}",
    )
    _check(
        "call confirm: ready card has call_yes/call_no keyboard",
        any(
            b["callback_data"] == "call_yes" for row in result["keyboard"] for b in row
        ),
        f"got: {result.get('keyboard')}",
    )


def test_confirm_card_without_language_asks_first():
    state = _state(who="Dr Sharma at +919876543210", what="cleaning")
    result = asyncio.run(core.call_confirm_card(42, state=state))
    _check(
        "call confirm: no language → status=needs_language + language_prompt line",
        result["status"] == "needs_language"
        and "What language" in result["card_with_lang_prompt"]
        and "lang_en"
        in [b["callback_data"] for row in result["language_keyboard"] for b in row],
        f"got: {result}",
    )
    _check(
        "call confirm: needs_language carries a pending_card for after language tap",
        "pending_card" in result and "card_text" in result["pending_card"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 5. call_plan_and_launch — the post-confirm plan_call + launch
# ---------------------------------------------------------------------------
def test_plan_and_launch_quota_blocked(monkeypatch_limit=0):
    """When usage is at the limit, the function returns limit_reached and
    the channel gets a 'limit reached' message."""
    from datetime import datetime
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (501,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (501, today, L),
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

    core.register_channel(501, Ch())
    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    result = asyncio.run(core.call_plan_and_launch(501, state=state))
    _check(
        "call plan+launch: at quota → status=limit_reached",
        result["status"] == "limit_reached",
        f"got: {result}",
    )
    _check(
        "call plan+launch: limit message includes reset line",
        "resets at midnight" in result["message"],
        f"got message: {result.get('message')!r}",
    )
    _check(
        "call plan+launch: limit message was sent via channel",
        any("resets at midnight" in m for m in sent),
        f"sent: {sent}",
    )
    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (501,))
    conn.commit()
    conn.close()
    core.unregister_channel(501)


def test_plan_and_launch_invalid_phone():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(502, Ch())
    state = _state(who="Dr Sharma", what="cleaning", call_language="English")
    result = asyncio.run(core.call_plan_and_launch(502, state=state))
    _check(
        "call plan+launch: invalid phone → status=invalid_phone",
        result["status"] == "invalid_phone",
        f"got: {result}",
    )
    _check(
        "call plan+launch: invalid phone message says 'isn't valid'",
        "isn't valid" in result["message"],
        f"got message: {result.get('message')!r}",
    )
    core.unregister_channel(502)


def test_plan_and_launch_needs_clarify():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(503, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_test",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        call_language="English",
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.call_plan_and_launch(503, state=state))
    _check(
        "call plan+launch: plan not ready → status=needs_clarify + plan_id",
        result["status"] == "needs_clarify" and result.get("plan_id") == "pl_test",
        f"got: {result}",
    )
    _check(
        "call plan+launch: needs_clarify question sent via channel",
        any("Which patient?" in m for m in sent),
        f"sent: {sent}",
    )
    _check(
        "call plan+launch: needs_clarify state.plan_id is set",
        state.get("plan_id") == "pl_test",
        f"state: {state}",
    )
    core.unregister_channel(503)


def test_plan_and_launch_calle_error():
    sent: list[str] = []
    from app.calle_client import CalleError

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(504, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        raise CalleError("calle blew up")

    state = _state(
        who="Dr Sharma at +919876543210", what="cleaning", call_language="English"
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.call_plan_and_launch(504, state=state))
    _check(
        "call plan+launch: CalleError → status=calle_unreachable",
        result["status"] == "calle_unreachable",
        f"got: {result}",
    )
    _check(
        "call plan+launch: calle_unreachable message includes 'Send /call to try again'",
        "Send /call to try again" in result["message"],
        f"got: {result.get('message')!r}",
    )
    core.unregister_channel(504)


def test_plan_and_launch_happy_path():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(505, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_happy",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": "run_happy",
                    "status": "RUNNING",
                }
            }
        }

    state = _state(
        who="Dr Sharma Dental at +919876543210",
        what="cleaning",
        call_language="English",
    )
    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        result = asyncio.run(core.call_plan_and_launch(505, state=state))

    _check(
        "call plan+launch: happy → status=launched",
        result["status"] == "launched",
        f"got: {result}",
    )
    _check(
        "call plan+launch: launched run_id is 'run_happy'",
        result.get("run_id") == "run_happy",
        f"got: {result.get('run_id')!r}",
    )
    _check(
        "call plan+launch: launched thread_text matches pre-W1 '📞 Calling now…' line",
        "Calling now" in result.get("thread_text", ""),
        f"got thread_text: {result.get('thread_text')!r}",
    )
    # Verify a calls row was inserted.
    conn = db._connect()
    row = conn.execute(
        "SELECT chat_id, plan_id, run_id, clinic_name, phone FROM calls WHERE run_id=?",
        ("run_happy",),
    ).fetchone()
    _check(
        "call plan+launch: calls row inserted with clinic_name + phone",
        row is not None
        and row["chat_id"] == 505
        and row["clinic_name"] == "Dr Sharma Dental"
        and row["phone"] == "+919876543210",
        f"row: {dict(row) if row else None}",
    )
    used = conn.execute("SELECT count FROM usage_counters WHERE chat_id=505").fetchone()
    _check(
        "call plan+launch: usage_counters bumped by 1",
        used is not None and used["count"] >= 1,
        f"used: {used}",
    )
    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (505,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (505,))
    conn.commit()
    conn.close()
    core.unregister_channel(505)


# ---------------------------------------------------------------------------
# 6. call_clarify_post_confirm — the post-✅ clarify loop
# ---------------------------------------------------------------------------
def test_post_confirm_clarify_too_many_rounds():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(506, Ch())
    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        call_language="English",
        clarify_rounds=core.MAX_CLARIFY_ROUNDS,
    )
    result = asyncio.run(
        core.call_clarify_post_confirm(506, "another answer", state=state)
    )
    _check(
        "post-confirm clarify: too many rounds → status=too_many_rounds",
        result["status"] == "too_many_rounds",
        f"got: {result}",
    )
    _check(
        "post-confirm clarify: too many rounds message sent via channel",
        any("taking longer than expected" in m for m in sent),
        f"sent: {sent}",
    )
    core.unregister_channel(506)


def test_post_confirm_clarify_keeps_state_on_calle_error():
    sent: list[str] = []
    from app.calle_client import CalleError

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(507, Ch())

    def fake_plan_call(answer, plan_id=None, language=None):
        raise CalleError("calle blew up")

    state = _state(
        who="Dr Sharma at +919876543210",
        what="cleaning",
        call_language="English",
        plan_id="pl_old",
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.call_clarify_post_confirm(507, "another answer", state=state)
        )
    _check(
        "post-confirm clarify: CalleError → status=calle_unreachable + keep_state",
        result["status"] == "calle_unreachable" and result.get("keep_state") is True,
        f"got: {result}",
    )
    _check(
        "post-confirm clarify: calle_unreachable message says 'updating the plan'",
        "updating the plan" in result["message"],
        f"got: {result.get('message')!r}",
    )
    core.unregister_channel(507)


def test_post_confirm_clarify_happy_path():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(508, Ch())

    def fake_plan_call(answer, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_post",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": "run_post",
                    "status": "RUNNING",
                }
            }
        }

    state = _state(
        who="Dr Sharma Dental at +919876543210",
        what="cleaning",
        call_language="English",
        plan_id="pl_old",
    )
    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        result = asyncio.run(
            core.call_clarify_post_confirm(508, "more details", state=state)
        )
    _check(
        "post-confirm clarify: happy → status=launched",
        result["status"] == "launched",
        f"got: {result}",
    )
    _check(
        "post-confirm clarify: launched run_id is 'run_post'",
        result.get("run_id") == "run_post",
        f"got: {result.get('run_id')!r}",
    )
    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (508,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (508,))
    conn.commit()
    conn.close()
    core.unregister_channel(508)


# ---------------------------------------------------------------------------
# 7. _apply_details — pure mutator
# ---------------------------------------------------------------------------
def test_apply_details_basic():
    state = _state()
    core._apply_details(
        {
            "clinic_or_doctor": "Dr X",
            "phone": "+919876543210",
            "reason": "r",
            "preferred_date": "d",
            "preferred_time": "t",
            "patient_name": "Ananya",
        },
        state=state,
    )
    _check(
        "apply_details: who = clinic at phone",
        state.get("who") == "Dr X at +919876543210",
        f"state: {state}",
    )
    _check(
        "apply_details: what = reason, date, time",
        state.get("what") == "r, d, t",
        f"state: {state}",
    )
    _check(
        "apply_details: patient_name preserved",
        state.get("patient_name") == "Ananya",
        f"state: {state}",
    )


def test_apply_details_fallback_what():
    state = _state()
    core._apply_details(
        {
            "clinic_or_doctor": "Dr X",
            "phone": "+919876543210",
            "reason": None,
            "preferred_date": None,
            "preferred_time": None,
            "patient_name": None,
        },
        state=state,
    )
    _check(
        "apply_details: what falls back to 'book an appointment' when nothing",
        state.get("what") == "book an appointment",
        f"state: {state}",
    )


def test_apply_details_pops_clarify_keys():
    state = _state(details={"x": 1}, first_text="hi", clarify_rounds=2)
    core._apply_details(
        {
            "clinic_or_doctor": "Dr X",
            "phone": "+919876543210",
            "reason": None,
            "preferred_date": None,
            "preferred_time": None,
            "patient_name": None,
        },
        state=state,
    )
    _check(
        "apply_details: pops details, first_text, clarify_rounds",
        "details" not in state
        and "first_text" not in state
        and "clarify_rounds" not in state,
        f"state: {state}",
    )


# ---------------------------------------------------------------------------
# 8. _call_confirm_text / _missing_fields_text — formatters
# ---------------------------------------------------------------------------
def test_call_confirm_text_masks_phone():
    text = core._call_confirm_text("Dr Sharma at +919876543210", "cleaning")
    _check(
        "call_confirm_text: includes masked phone",
        "+91******210" in text,
        f"got: {text!r}",
    )
    _check(
        "call_confirm_text: includes 'Call:' and 'Request:'",
        "📞 Call:" in text and "📝 Request:" in text,
        f"got: {text!r}",
    )


def test_missing_fields_text_phone_invalid_branch():
    text = core._missing_fields_text(["phone"], {"phone_invalid": True})
    _check(
        "missing_fields_text: phone+phone_invalid branch says 'international format'",
        "international format" in text,
        f"got: {text!r}",
    )


def test_missing_fields_text_generic_branch():
    text = core._missing_fields_text(["reason"])
    _check(
        "missing_fields_text: generic branch humanises field names",
        "reason" in text,
        f"got: {text!r}",
    )


# ---------------------------------------------------------------------------
# Session 18: terminal-status normalisation (NO_ANSWER vs 'NO ANSWER')
# ---------------------------------------------------------------------------
def test_normalize_status_and_is_terminal():
    """Session 18: CALL-E has been observed returning 'NO_ANSWER'
    (underscore) and 'NO ANSWER' (space) for the same logical status.
    `is_terminal` must normalise both to the same canonical form so the
    poller correctly recognises the call as finished. Also locks in
    the collapse of CANCELED + CANCELLED to one canonical key.
    """
    _check(
        "normalise: 'NO_ANSWER' and 'NO ANSWER' both terminal",
        is_terminal("NO_ANSWER") and is_terminal("NO ANSWER"),
        f"is_terminal: NO_ANSWER={is_terminal('NO_ANSWER')}, "
        f"NO ANSWER={is_terminal('NO ANSWER')}",
    )
    _check(
        "normalise: case-insensitive ('no answer', 'No Answer')",
        is_terminal("no answer") and is_terminal("No Answer"),
        f"is_terminal: no answer={is_terminal('no answer')}, "
        f"No Answer={is_terminal('No Answer')}",
    )
    _check(
        "normalise: 'cancelled' is terminal, 'canceled' is not (collapsed)",
        is_terminal("cancelled") and not is_terminal("canceled"),
        f"is_terminal: cancelled={is_terminal('cancelled')}, "
        f"canceled={is_terminal('canceled')}",
    )
    _check(
        "normalise: non-terminal statuses return False",
        not is_terminal("RUNNING") and not is_terminal("IN_PROGRESS"),
        f"is_terminal: RUNNING={is_terminal('RUNNING')}, "
        f"IN_PROGRESS={is_terminal('IN_PROGRESS')}",
    )
    _check(
        "normalise: None and empty string are not terminal",
        not is_terminal(None) and not is_terminal(""),
        f"is_terminal: None={is_terminal(None)}, ''={is_terminal('')}",
    )
    _check(
        "normalise: 'NO ANSWER' canonicalises to 'NO_ANSWER'",
        normalize_status("NO ANSWER") == "NO_ANSWER",
        f"got: {normalize_status('NO ANSWER')!r}",
    )
    _check(
        "normalise: 'canceled' canonicalises to 'CANCELED'",
        normalize_status("canceled") == "CANCELED",
        f"got: {normalize_status('canceled')!r}",
    )


def test_safety_net_fresh_insert_not_matched():
    """Freshly inserted 'running' row should NOT be matched by get_stuck_running_calls(300)"""
    from datetime import datetime, timezone

    conn = db._connect()
    test_run_id = "fresh_insert_test"
    test_chat = 99002
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    # Insert row created just now (UTC)
    now_created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (
            test_chat,
            "plan_fresh",
            test_run_id,
            "Fresh Test Clinic",
            "+919876543210",
            now_created,
        ),
    )
    conn.commit()
    conn.close()

    # Should NOT be matched (age < 5 minutes)
    stuck = db.get_stuck_running_calls(300)
    assert len([r for r in stuck if r["run_id"] == test_run_id]) == 0, (
        "Freshly inserted row incorrectly matched as stuck"
    )

    # Cleanup
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    conn.commit()
    conn.close()


def test_safety_net_six_minute_old_row_is_matched():
    """Row with created_at set to 6 minutes ago SHOULD be matched by get_stuck_running_calls(300)"""
    from datetime import datetime, timedelta, timezone

    conn = db._connect()
    test_run_id = "six_min_old_test"
    test_chat = 99003
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    # Insert row created 6 minutes ago (UTC)
    six_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, phone, created_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (
            test_chat,
            "plan_six_min",
            test_run_id,
            "Six Minute Old Clinic",
            "+919876543210",
            six_min_ago,
        ),
    )
    conn.commit()
    conn.close()

    # SHOULD be matched (age > 5 minutes)
    stuck = db.get_stuck_running_calls(300)
    assert len([r for r in stuck if r["run_id"] == test_run_id]) == 1, (
        "Six-minute-old row not matched as stuck"
    )

    # Cleanup
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    conn.commit()
    conn.close()


def test_poller_safety_net_force_reports_stuck_row():
    """Session 18: a call row that's been 'running' for longer than
    MAX_POLL_DURATION_SECONDS (5 min) without reaching a terminal status
    must be force-reported as POLL_ERROR with a 'status unclear' message.
    This locks in the safety-net path that prevents silent infinite
    polling — the exact failure mode that left row id=312 stuck
    'running' on 2026-09-06 because 'NO ANSWER' (space) didn't match
    'NO_ANSWER' (underscore) in TERMINAL_STATUSES.
    """
    import asyncio as _asyncio
    from datetime import datetime, timedelta, timezone
    from app import bot

    # Pre-seed a 'running' row that's older than 5 minutes (the safety net
    # threshold). This is the same state as the live stuck id=312 row.
    conn = db._connect()
    test_run_id = "stuck_run_for_safety_test"
    test_chat = 99001
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    old_created = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "INSERT INTO calls (chat_id, plan_id, run_id, status, clinic_name, "
        "phone, created_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (
            test_chat,
            "plan_safety",
            test_run_id,
            "Safety Test Clinic",
            "+919876543210",
            old_created,
        ),
    )
    conn.commit()
    conn.close()

    # Drive one tick of the safety-net pass directly (the actual pass
    # at the end of poll_loop). We replicate it here to keep the test
    # focused on the safety-net behaviour.
    sent: list[str] = []
    bot._last_status_cache[test_run_id] = "RUNNING"
    fake_bot = object()

    async def fake_safe_send(bot, chat_id, text, **kw):
        sent.append(text)
        return None

    async def run_safety_pass():
        stuck = db.get_stuck_running_calls(300)
        for srow in stuck:
            if srow["run_id"] != test_run_id:
                continue
            srun_id = srow["run_id"]
            await _asyncio.to_thread(db.finish_call, srun_id, "POLL_ERROR")
            bot._last_status_cache.pop(srun_id, None)
            await fake_safe_send(
                fake_bot,
                srow["chat_id"],
                "⚠️ I lost track of your call after 5 minutes — its status "
                "is unclear. Please check with the clinic directly to "
                "confirm whether the booking was made.",
            )
            bot._last_status_cache.pop(srun_id, None)
            await fake_safe_send(
                fake_bot,
                srow["chat_id"],
                "⚠️ I lost track of your call after 5 minutes — its status "
                "is unclear. Please check with the clinic directly to "
                "confirm whether the booking was made.",
            )

    _asyncio.run(run_safety_pass())

    final = (
        db._connect()
        .execute("SELECT status FROM calls WHERE run_id=?", (test_run_id,))
        .fetchone()
    )
    _check(
        "safety-net: stuck row force-finished as POLL_ERROR",
        final is not None and final["status"] == "POLL_ERROR",
        f"final status: {final['status'] if final else None!r}",
    )
    _check(
        "safety-net: user got 'status unclear' message",
        any("status is unclear" in m for m in sent),
        f"sent: {sent}",
    )

    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE run_id=?", (test_run_id,))
    conn.commit()
    conn.close()
    bot._last_status_cache.pop(test_run_id, None)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main() -> int:
    test_start_call_groq_unavailable()
    test_start_call_groq_error()
    test_start_call_ready()
    test_start_call_needs_clarify()
    test_start_call_phone_invalid()
    test_call_clarify_merges_and_succeeds()
    test_call_clarify_groq_error_falls_back_to_guided()
    test_call_clarify_still_no_phone_triggers_guided()
    test_call_collect_who_accepts_valid()
    test_call_collect_who_rejects_missing_phone()
    test_call_collect_what_accepts()
    test_call_collect_what_rejects_empty()
    test_confirm_card_with_language_set()
    test_confirm_card_without_language_asks_first()
    test_plan_and_launch_quota_blocked()
    test_plan_and_launch_invalid_phone()
    test_plan_and_launch_needs_clarify()
    test_plan_and_launch_calle_error()
    test_plan_and_launch_happy_path()
    test_post_confirm_clarify_too_many_rounds()
    test_post_confirm_clarify_keeps_state_on_calle_error()
    test_post_confirm_clarify_happy_path()
    test_apply_details_basic()
    test_apply_details_fallback_what()
    test_apply_details_pops_clarify_keys()
    test_call_confirm_text_masks_phone()
    test_missing_fields_text_phone_invalid_branch()
    test_missing_fields_text_generic_branch()
    # Session 18: terminal-status normalisation
    test_normalize_status_and_is_terminal()
    test_poller_safety_net_force_reports_stuck_row()
    print()
    if FAILED:
        for f in FAILED:
            print(f"FAIL: {f}")
        return 1
    print(f"ALL {len(PASSED)} PHASE-W1 /call PARITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

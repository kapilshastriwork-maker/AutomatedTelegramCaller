"""Phase W1: parity check for the /callaround and /earliest flow extractions.

The /callaround and /earliest decision logic now lives in app.core
(start_multi_clinic, multi_clinic_clarify, multi_collect_target,
multi_collect_what, multi_preflight, multi_clarify_post_preflight,
multi_confirm_card, launch_multi_call). This script stubs the groq and
CALLE paths and exercises those functions for both `mode="book"` and
`mode="earliest"`, asserting the same dict shapes the bot's thin
handlers expect.
"""

import asyncio
import sys
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
# 1. start_multi_clinic — body of bot.ca_received / bot.early_received
# ---------------------------------------------------------------------------
def test_start_callaround_groq_unavailable():
    with patch.object(core.groq_client, "is_configured", lambda: False):
        result = asyncio.run(
            core.start_multi_clinic(42, "text", state=_state(), mode="book")
        )
    _check(
        "callaround start: groq not configured → status=groq_unavailable",
        result["status"] == "groq_unavailable" and "/callaround" in result["message"],
        f"got: {result}",
    )


def test_start_earliest_groq_unavailable():
    with patch.object(core.groq_client, "is_configured", lambda: False):
        result = asyncio.run(
            core.start_multi_clinic(42, "text", state=_state(), mode="earliest")
        )
    _check(
        "earliest start: groq not configured → status=groq_unavailable + /earliest msg",
        result["status"] == "groq_unavailable" and "/earliest" in result["message"],
        f"got: {result}",
    )


def test_start_callaround_groq_error():
    def bad_extract(text):
        raise core.groq_client.GroqError("boom")

    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_around", bad_extract),
    ):
        result = asyncio.run(
            core.start_multi_clinic(42, "text", state=_state(), mode="book")
        )
    _check(
        "callaround start: groq GroqError → status=groq_error + guided prompt",
        result["status"] == "groq_error" and "step by step" in result["guided_prompt"],
        f"got: {result}",
    )


def test_start_callaround_ready():
    details = {
        "targets": [
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        "invalid_names": [],
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
        "patient_name": None,
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_around", lambda t: details),
    ):
        result = asyncio.run(
            core.start_multi_clinic(
                42, "Dr A +91... and Dr B +91... cleaning", state=_state(), mode="book"
            )
        )
    _check(
        "callaround start: 2 valid targets → status=ready_for_preflight + 2 targets + what",
        result["status"] == "ready_for_preflight"
        and len(result["targets"]) == 2
        and "cleaning" in result["what"],
        f"got: {result}",
    )


def test_start_callaround_too_many():
    """More than MAX_CA_TARGETS (5) → truncated + dropped_message."""
    details = {
        "targets": [{"name": f"Dr {i}", "phone": f"+91987654321{i}"} for i in range(7)],
        "invalid_names": [],
        "reason": "cleaning",
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_around", lambda t: details),
    ):
        result = asyncio.run(
            core.start_multi_clinic(42, "many", state=_state(), mode="book")
        )
    _check(
        "callaround start: >5 targets → status=ready_for_preflight with 5 targets + dropped_message",
        result["status"] == "ready_for_preflight"
        and len(result["targets"]) == 5
        and "Dr 5" in (result.get("dropped_message") or "")
        and "Dr 6" in (result["dropped_message"]),
        f"got: {result}",
    )


def test_start_callaround_too_few():
    details = {
        "targets": [{"name": "Dr A", "phone": "+919876543210"}],
        "invalid_names": [],
        "reason": "cleaning",
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_call_around", lambda t: details),
    ):
        result = asyncio.run(
            core.start_multi_clinic(42, "one", state=_state(), mode="book")
        )
    _check(
        "callaround start: <2 valid → status=needs_clarify + 'try them together'",
        result["status"] == "needs_clarify"
        and any("try them together" in p for p in result["problems"]),
        f"got: {result}",
    )


def test_start_earliest_too_few():
    details = {
        "targets": [{"name": "Dr A", "phone": "+919876543210"}],
        "invalid_names": [],
        "reason": "cleaning",
    }
    with (
        patch.object(core.groq_client, "is_configured", lambda: True),
        patch.object(core.groq_client, "extract_earliest_details", lambda t: details),
    ):
        result = asyncio.run(
            core.start_multi_clinic(42, "one", state=_state(), mode="earliest")
        )
    _check(
        "earliest start: <2 valid → status=needs_clarify + 'compare them'",
        result["status"] == "needs_clarify"
        and any("compare them" in p for p in result["problems"]),
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 2. multi_clinic_clarify — body of bot.ca_followup / bot.early_followup
# ---------------------------------------------------------------------------
def test_multi_clarify_callaround_happy():
    details = {
        "targets": [
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        "invalid_names": [],
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
    }
    with patch.object(core.groq_client, "extract_call_around", lambda t: details):
        result = asyncio.run(
            core.multi_clinic_clarify(42, "more", state=_state(), mode="book")
        )
    _check(
        "callaround clarify: 2nd try complete → status=ready_for_preflight",
        result["status"] == "ready_for_preflight" and len(result["targets"]) == 2,
        f"got: {result}",
    )
    _check(
        "callaround clarify: ack message includes 'Got it.'",
        result.get("ack", "").startswith("Got it."),
        f"got: {result.get('ack')!r}",
    )


def test_multi_clarify_callaround_still_too_few():
    details = {
        "targets": [{"name": "Dr A", "phone": "+919876543210"}],
        "invalid_names": [],
        "reason": "cleaning",
    }
    with patch.object(core.groq_client, "extract_call_around", lambda t: details):
        result = asyncio.run(
            core.multi_clinic_clarify(42, "more", state=_state(), mode="book")
        )
    _check(
        "callaround clarify: still <2 valid → status=needs_clarify",
        result["status"] == "needs_clarify" and "Still short" in result["problems"][0],
        f"got: {result}",
    )


def test_multi_clarify_callaround_with_invalid():
    details = {
        "targets": [
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        "invalid_names": ["Dr Bogus +12"],
        "reason": "cleaning",
    }
    with patch.object(core.groq_client, "extract_call_around", lambda t: details):
        result = asyncio.run(
            core.multi_clinic_clarify(42, "more", state=_state(), mode="book")
        )
    _check(
        "callaround clarify: invalid_names → ack has 'Dropped unparseable entries'",
        result["status"] == "ready_for_preflight"
        and "Dropped unparseable entries" in result["ack"]
        and "Dr Bogus" in result["ack"],
        f"got ack: {result.get('ack')!r}",
    )


def test_multi_clarify_groq_error():
    def bad_extract(t):
        raise core.groq_client.GroqError("boom")

    with patch.object(core.groq_client, "extract_call_around", bad_extract):
        result = asyncio.run(
            core.multi_clinic_clarify(42, "more", state=_state(), mode="book")
        )
    _check(
        "callaround clarify: groq error → status=groq_error + /callagain msg",
        result["status"] == "groq_error" and "/callaround" in result["message"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 3. multi_collect_target — guided target entry
# ---------------------------------------------------------------------------
def test_collect_target_adds_valid():
    state = _state()
    result = asyncio.run(
        core.multi_collect_target(42, "Dr A +919876543210", state=state)
    )
    _check(
        "collect_target: valid → status=added + state.targets has 1 entry",
        result["status"] == "added" and len(state.get("targets", [])) == 1,
        f"got: {result}, state: {state}",
    )
    _check(
        "collect_target: next_prompt says 'Send clinic 2'",
        "Send clinic 2" in result["next_prompt"],
        f"got: {result.get('next_prompt')!r}",
    )


def test_collect_target_done_with_too_few():
    state = _state(targets=[{"name": "Dr A", "phone": "+919876543210"}])
    result = asyncio.run(core.multi_collect_target(42, "done", state=state))
    _check(
        "collect_target: 'done' with 1 target → status=need_more + 'I need at least 2'",
        result["status"] == "need_more" and "at least 2" in result["message"],
        f"got: {result}",
    )


def test_collect_target_done_with_enough_callaround():
    state = _state(
        mode="book",
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
    )
    result = asyncio.run(core.multi_collect_target(42, "done", state=state))
    _check(
        "collect_target: 'done' with 2 targets (mode=book) → status=ready_for_what + book prompt",
        result["status"] == "ready_for_what"
        and "Now, what should I book" in result["next_prompt"],
        f"got: {result}",
    )


def test_collect_target_done_with_enough_earliest():
    state = _state(
        mode="earliest",
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
    )
    result = asyncio.run(core.multi_collect_target(42, "done", state=state))
    _check(
        "collect_target: 'done' with 2 targets (mode=earliest) → status=ready_for_what + check-availability prompt",
        result["status"] == "ready_for_what"
        and "check availability" in result["next_prompt"],
        f"got: {result}",
    )


def test_collect_target_no_phone_in_text():
    state = _state()
    result = asyncio.run(core.multi_collect_target(42, "no number here", state=state))
    _check(
        "collect_target: no phone → status=need_more + 'international format'",
        result["status"] == "need_more" and "international format" in result["message"],
        f"got: {result}",
    )
    _check(
        "collect_target: no phone → state.targets is empty",
        state.get("targets", []) == [],
        f"state: {state}",
    )


def test_collect_target_max_reached():
    state = _state(
        targets=[{"name": f"D{i}", "phone": f"+9198765432{i}"} for i in range(5)]
    )
    result = asyncio.run(
        core.multi_collect_target(42, "Dr X +919876543299", state=state)
    )
    _check(
        "collect_target: 5 already → status=max_reached + 'Maximum is 5'",
        result["status"] == "max_reached" and "Maximum is 5" in result["message"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 4. multi_collect_what — guided what entry
# ---------------------------------------------------------------------------
def test_collect_what_accepts():
    state = _state(mode="book")
    result = asyncio.run(
        core.multi_collect_what(42, "cleaning next Tuesday", state=state)
    )
    _check(
        "collect_what: non-empty → status=ready_for_preflight + state.what populated",
        result["status"] == "ready_for_preflight"
        and state.get("what") == "cleaning next Tuesday",
        f"got: {result}, state: {state}",
    )


def test_collect_what_empty():
    state = _state(mode="book")
    result = asyncio.run(core.multi_collect_what(42, "  ", state=state))
    _check(
        "collect_what: empty → status=empty + 'Please describe the booking' (mode=book)",
        result["status"] == "empty"
        and "Please describe the booking" in result["message"],
        f"got: {result}",
    )


def test_collect_what_empty_earliest():
    state = _state(mode="earliest")
    result = asyncio.run(core.multi_collect_what(42, "  ", state=state))
    _check(
        "collect_what: empty → status=empty + 'Please describe the request' (mode=earliest)",
        result["status"] == "empty"
        and "Please describe the request" in result["message"],
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 5. multi_preflight
# ---------------------------------------------------------------------------
def test_preflight_calle_error():
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

    core.register_channel(801, Ch())

    def fake_plan_call(*a, **kw):
        raise CalleError("calle blew up")

    state = _state(what="cleaning", call_language="English")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.multi_preflight(801, state=state))
    _check(
        "preflight: CalleError → status=calle_unreachable + 'try /callaround again'",
        result["status"] == "calle_unreachable"
        and "try /callaround again" in result["message"],
        f"got: {result}",
    )
    core.unregister_channel(801)


def test_preflight_needs_clarify():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(802, Ch())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_multi",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    state = _state(what="cleaning", call_language="English")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.multi_preflight(802, state=state))
    _check(
        "preflight: !ready_to_run → status=needs_clarify + state.plan_id set",
        result["status"] == "needs_clarify" and state.get("plan_id") == "pl_multi",
        f"got: {result}",
    )
    _check(
        "preflight: needs_clarify sent via channel",
        any("Which patient?" in m for m in sent),
        f"sent: {sent}",
    )
    core.unregister_channel(802)


def test_preflight_ready():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(803, Ch())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_multi_ok",
                    "confirm_token": "tok",
                }
            }
        }

    state = _state(what="cleaning", call_language="English")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(core.multi_preflight(803, state=state))
    _check(
        "preflight: ready_to_run → status=ready_to_confirm",
        result["status"] == "ready_to_confirm"
        and result.get("plan", {}).get("plan_id") == "pl_multi_ok",
        f"got: {result}",
    )
    core.unregister_channel(803)


# ---------------------------------------------------------------------------
# 6. multi_clarify_post_preflight
# ---------------------------------------------------------------------------
def test_post_preflight_too_many_rounds():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(804, Ch())
    state = _state(sched_clarify_rounds=core.MAX_CLARIFY_ROUNDS, mode="book")
    result = asyncio.run(core.multi_clarify_post_preflight(804, "more", state=state))
    _check(
        "post-preflight clarify: too many rounds → status=too_many_rounds (callaround)",
        result["status"] == "too_many_rounds" and "/callaround" in result["message"],
        f"got: {result}",
    )
    core.unregister_channel(804)


def test_post_preflight_too_many_rounds_earliest():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(805, Ch())
    state = _state(sched_clarify_rounds=core.MAX_CLARIFY_ROUNDS, mode="earliest")
    result = asyncio.run(core.multi_clarify_post_preflight(805, "more", state=state))
    _check(
        "post-preflight clarify: too many rounds → /earliest (earliest mode)",
        result["status"] == "too_many_rounds" and "/earliest" in result["message"],
        f"got: {result}",
    )
    core.unregister_channel(805)


def test_post_preflight_calle_error_keeps_state():
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

    core.register_channel(806, Ch())

    def fake_plan_call(*a, **kw):
        raise CalleError("calle blew up")

    state = _state(plan_id="pl_old", call_language="English", mode="book")
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.multi_clarify_post_preflight(806, "more", state=state)
        )
    _check(
        "post-preflight clarify: CalleError → status=calle_unreachable + keep_state",
        result["status"] == "calle_unreachable" and result.get("keep_state") is True,
        f"got: {result}",
    )
    core.unregister_channel(806)


def test_post_preflight_ready_concatenates_extra():
    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)
            return None

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(807, Ch())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_post",
                    "confirm_token": "tok",
                }
            }
        }

    state = _state(
        what="cleaning", plan_id="pl_old", call_language="English", mode="book"
    )
    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.multi_clarify_post_preflight(807, "more details", state=state)
        )
    _check(
        "post-preflight clarify: ready → status=ready_to_confirm + state.what extended",
        result["status"] == "ready_to_confirm"
        and "more details" in (state.get("what") or "")
        and "cleaning" in (state.get("what") or ""),
        f"got: {result}, state: {state}",
    )
    core.unregister_channel(807)


# ---------------------------------------------------------------------------
# 7. multi_confirm_card
# ---------------------------------------------------------------------------
def test_confirm_card_quota_blocked():
    from datetime import datetime
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (901,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (901, today, L),
    )
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "A", "phone": "+919876543210"},
            {"name": "B", "phone": "+919876543211"},
        ],
        what="cleaning",
        call_language="English",
    )
    result = asyncio.run(core.multi_confirm_card(901, state=state))
    _check(
        "confirm-card: at quota → status=limit_reached",
        result["status"] == "limit_reached",
        f"got: {result}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (901,))
    conn.commit()
    conn.close()


def test_confirm_card_ready_callaround():
    from datetime import datetime

    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (902,))
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        what="cleaning",
        call_language="English",
        mode="book",
    )
    result = asyncio.run(core.multi_confirm_card(902, state=state))
    _check(
        "confirm-card: under quota + callaround → status=ready + ca_yes keyboard",
        result["status"] == "ready"
        and any(
            b["callback_data"] == "ca_yes" for row in result["keyboard"] for b in row
        ),
        f"got: {result}",
    )
    _check(
        "confirm-card: card_text masks phone",
        "+91******210" in result["card_text"],
        f"got card_text: {result.get('card_text', '')[:80]!r}",
    )


def test_confirm_card_ready_earliest():
    from datetime import datetime

    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (903,))
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        what="cleaning",
        call_language="English",
        mode="earliest",
    )
    result = asyncio.run(core.multi_confirm_card(903, state=state))
    _check(
        "confirm-card: under quota + earliest → status=ready + early_yes keyboard",
        result["status"] == "ready"
        and any(
            b["callback_data"] == "early_yes" for row in result["keyboard"] for b in row
        ),
        f"got: {result}",
    )


def test_confirm_card_partial_callaround():
    from datetime import datetime
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (904,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (904, today, L - 1),
    )  # only 1 left
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "A", "phone": "+919876543210"},
            {"name": "B", "phone": "+919876543211"},
            {"name": "C", "phone": "+919876543212"},
        ],
        what="cleaning",
        call_language="English",
        mode="book",
    )
    result = asyncio.run(core.multi_confirm_card(904, state=state))
    _check(
        "confirm-card: 1 left + 3 targets → status=partial with offered=1 + ca_reduce keyboard",
        result["status"] == "partial"
        and result.get("offered") == 1
        and any(
            b["callback_data"] == "ca_reduce" for row in result["keyboard"] for b in row
        ),
        f"got: {result}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (904,))
    conn.commit()
    conn.close()


def test_confirm_card_partial_earliest():
    from datetime import datetime
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (905,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (905, today, L - 1),
    )
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "A", "phone": "+919876543210"},
            {"name": "B", "phone": "+919876543211"},
        ],
        what="cleaning",
        call_language="English",
        mode="earliest",
    )
    result = asyncio.run(core.multi_confirm_card(905, state=state))
    _check(
        "confirm-card: 1 left + 2 targets (earliest) → early_reduce keyboard",
        result["status"] == "partial"
        and any(
            b["callback_data"] == "early_reduce"
            for row in result["keyboard"]
            for b in row
        ),
        f"got: {result}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (905,))
    conn.commit()
    conn.close()


def test_confirm_card_without_language_asks_first_callaround():
    from datetime import datetime

    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (906,))
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        what="cleaning",
        mode="book",
    )
    result = asyncio.run(core.multi_confirm_card(906, state=state))
    _check(
        "confirm-card: no language + callaround → status=needs_language + ca_yes pending",
        result["status"] == "needs_language"
        and "What language" in result["card_with_lang_prompt"]
        and any(
            b["callback_data"] == "ca_yes"
            for row in result["pending_card"].get("keyboard", [])
            for b in row
        ),
        f"got: {result}",
    )


def test_confirm_card_without_language_asks_first_earliest():
    from datetime import datetime

    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (907,))
    conn.commit()
    conn.close()
    state = _state(
        targets=[
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": "+919876543211"},
        ],
        what="cleaning",
        mode="earliest",
    )
    result = asyncio.run(core.multi_confirm_card(907, state=state))
    _check(
        "confirm-card: no language + earliest → early_yes pending",
        result["status"] == "needs_language"
        and any(
            b["callback_data"] == "early_yes"
            for row in result["pending_card"].get("keyboard", [])
            for b in row
        ),
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 8. launch_multi_call — parallel per-clinic launches
# ---------------------------------------------------------------------------
def test_launch_multi_call_happy_path():
    """Plan + run for each clinic; both should land calls rows; the poller
    will edit the per-clinic threads at terminal (not exercised here)."""
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1001,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1001,))
    conn.commit()
    conn.close()

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)

            class M:
                message_id = 900 + len(sent)

            return M()

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(1001, Ch())

    plan_idx = {"i": 0}
    run_idx = {"i": 0}

    def fake_plan_call(user_input, plan_id=None, language=None):
        plan_idx["i"] += 1
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": f"pl_{plan_idx['i']}",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        run_idx["i"] += 1
        return {
            "result": {
                "structuredContent": {
                    "run_id": f"run_{run_idx['i']}",
                    "status": "RUNNING",
                }
            }
        }

    targets = [
        {"name": "Dr A", "phone": "+919876543210"},
        {"name": "Dr B", "phone": "+919876543211"},
    ]

    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        result = asyncio.run(
            core.launch_multi_call(
                1001, "batch_test", targets, "cleaning", language="English", mode="book"
            )
        )

    _check(
        "launch_multi: status=started, batch_id matches, 2 per_clinic",
        result["status"] == "started"
        and result["batch_id"] == "batch_test"
        and len(result["per_clinic"]) == 2,
        f"got: {result}",
    )
    _check(
        "launch_multi: both clinics have status=launched + run_ids",
        all(
            c["status"] == "launched" and c.get("run_id") for c in result["per_clinic"]
        ),
        f"got per_clinic: {result['per_clinic']}",
    )
    # Verify calls rows (order is non-deterministic because asyncio.gather)
    conn = db._connect()
    rows = conn.execute(
        "SELECT id, clinic_name, batch_id, run_id FROM calls WHERE chat_id=1001 ORDER BY id"
    ).fetchall()
    names = sorted([r["clinic_name"] for r in rows])
    _check(
        "launch_multi: 2 calls rows inserted, both with batch_id='batch_test'",
        len(rows) == 2
        and names == ["Dr A", "Dr B"]
        and all(r["batch_id"] == "batch_test" for r in rows)
        and all(r["run_id"] is not None for r in rows),
        f"rows: {[dict(r) for r in rows]}",
    )
    # Verify per-clinic '📞 Calling X at Y now…' thread was sent for each
    _check(
        "launch_multi: 2 '📞 Calling …' threads sent with masked phones",
        sum(1 for m in sent if "📞 Calling" in m) == 2
        and any("+91******210" in m for m in sent if "Dr A" in m),
        f"sent: {sent}",
    )
    # usage_counters bumped twice
    used = conn.execute(
        "SELECT count FROM usage_counters WHERE chat_id=1001"
    ).fetchone()
    _check(
        "launch_multi: usage_counters bumped twice",
        used["count"] == 2,
        f"used: {used}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1001,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1001,))
    conn.commit()
    conn.close()
    core.unregister_channel(1001)


def test_launch_multi_call_handles_plan_call_failure():
    """One clinic's plan_call raises → that clinic gets a failure message
    and a 'failed' row, the other clinic still launches."""
    from app.calle_client import CalleError

    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1002,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1002,))
    conn.commit()
    conn.close()

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)

            class M:
                message_id = 800 + len(sent)

            return M()

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(1002, Ch())

    plan_idx = {"i": 0}

    def fake_plan_call(user_input, plan_id=None, language=None):
        plan_idx["i"] += 1
        # First clinic (Dr A) fails; second (Dr B) succeeds.
        if "Dr A" in user_input:
            raise CalleError("calle blew up")
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": f"pl_{plan_idx['i']}",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        return {
            "result": {
                "structuredContent": {
                    "run_id": f"run_{plan_id}",
                    "status": "RUNNING",
                }
            }
        }

    targets = [
        {"name": "Dr A", "phone": "+919876543210"},
        {"name": "Dr B", "phone": "+919876543211"},
    ]

    with (
        patch.object(core.calle_client, "plan_call", fake_plan_call),
        patch.object(core.calle_client, "run_call", fake_run_call),
    ):
        result = asyncio.run(
            core.launch_multi_call(
                1002, "batch_fail", targets, "cleaning", language="English", mode="book"
            )
        )

    _check(
        "launch_multi (partial fail): Dr A status=failed/reason=plan_failed, Dr B launched",
        result["per_clinic"][0]["status"] == "failed"
        and result["per_clinic"][0]["reason"] == "plan_failed"
        and result["per_clinic"][1]["status"] == "launched",
        f"got per_clinic: {result['per_clinic']}",
    )
    # Calls rows: 1 failed (Dr A) + 1 launched (Dr B) = 2 rows
    conn = db._connect()
    rows = conn.execute(
        "SELECT clinic_name, run_id, status FROM calls WHERE chat_id=1002 ORDER BY id"
    ).fetchall()
    _check(
        "launch_multi (partial fail): both calls rows present (one failed, one running)",
        len(rows) == 2
        and rows[0]["clinic_name"] == "Dr A"
        and rows[0]["status"] == "failed"
        and rows[1]["clinic_name"] == "Dr B"
        and rows[1]["status"] == "running",
        f"rows: {rows}",
    )
    # Dr A failure message sent
    _check(
        "launch_multi (partial fail): Dr A failure message sent",
        any(m.startswith("❌ Dr A:") and "planning request failed" in m for m in sent),
        f"sent: {sent}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1002,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1002,))
    conn.commit()
    conn.close()
    core.unregister_channel(1002)


def test_launch_multi_call_needs_clarify():
    """plan not ready_to_run → that clinic is recorded as failed with
    the questions in the failure thread."""
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1003,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1003,))
    conn.commit()
    conn.close()

    sent: list[str] = []

    class Ch:
        async def send_text(self, text, **kw):
            sent.append(text)

            class M:
                message_id = 700 + len(sent)

            return M()

        async def edit_text(self, *a, **k):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(1003, Ch())

    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_nc",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    targets = [{"name": "Dr A", "phone": "+919876543210"}]

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.launch_multi_call(
                1003, "batch_nc", targets, "cleaning", language="English", mode="book"
            )
        )

    _check(
        "launch_multi (needs_clarify): status=failed, reason=needs_clarify",
        result["per_clinic"][0]["status"] == "failed"
        and result["per_clinic"][0]["reason"] == "needs_clarify",
        f"got: {result}",
    )
    # Dr A failure message includes the question
    _check(
        "launch_multi (needs_clarify): failure message includes the question",
        any(
            "Dr A" in m and "needs more information" in m and "Which patient?" in m
            for m in sent
        ),
        f"sent: {sent}",
    )
    # Verify a calls row was inserted (with plan_id but no run_id, status=failed)
    conn = db._connect()
    row = conn.execute(
        "SELECT plan_id, run_id, status FROM calls WHERE chat_id=1003"
    ).fetchone()
    _check(
        "launch_multi (needs_clarify): calls row inserted with status=failed + plan_id set",
        row is not None
        and row["status"] == "failed"
        and row["plan_id"] == "pl_nc"
        and row["run_id"] is None,
        f"row: {row}",
    )
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (1003,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (1003,))
    conn.commit()
    conn.close()
    core.unregister_channel(1003)


# ---------------------------------------------------------------------------
# 9. _clean_request_text / _ca_confirm_text / _compose_what / valid_ca_targets
# ---------------------------------------------------------------------------
def test_clean_request_text_strips_phones_and_names():
    cleaned = core._clean_request_text(
        "Book a cleaning at +919876543210 for Dr Sharma Dental",
        [{"name": "Dr Sharma Dental", "phone": "+919876543210"}],
    )
    _check(
        "clean_request_text: strips phone and name from what",
        "919876543210" not in cleaned
        and "Sharma" not in cleaned
        and "cleaning" in cleaned,
        f"got: {cleaned!r}",
    )


def test_ca_confirm_text_includes_clinics_and_request():
    text = core._ca_confirm_text(
        [{"name": "Dr A", "phone": "+919876543210"}],
        "Book a cleaning at +919876543210",
    )
    _check(
        "ca_confirm_text: includes '📞 Clinics (1):' header",
        "📞 Clinics (1):" in text,
        f"got: {text!r}",
    )
    _check(
        "ca_confirm_text: includes masked phone",
        "+91******210" in text,
        f"got: {text!r}",
    )
    _check(
        "ca_confirm_text: 'Request:' line is stripped of phone",
        "📝 Request: Book a cleaning" in text,
        f"got: {text!r}",
    )


def test_compose_what_falls_back():
    pieces = core.compose_what({})
    _check(
        "compose_what: empty details → 'book an appointment' fallback",
        pieces == "book an appointment",
        f"got: {pieces!r}",
    )


def test_compose_what_joins_pieces():
    pieces = core.compose_what(
        {"reason": "cleaning", "preferred_date": "Tue", "preferred_time": "10am"}
    )
    _check(
        "compose_what: joins reason+date+time with ', '",
        pieces == "cleaning, Tue, 10am",
        f"got: {pieces!r}",
    )


def test_valid_ca_targets_filters_invalid_and_missing_phone():
    details = {
        "targets": [
            {"name": "Dr A", "phone": "+919876543210"},
            {"name": "Dr B", "phone": None, "invalid": True},
            {"name": "Dr C", "phone": "+12", "invalid": True},
            {"name": "Dr D", "phone": "+919876543211", "invalid": False},
        ]
    }
    valid = core.valid_ca_targets(details)
    _check(
        "valid_ca_targets: filters out targets with no phone or invalid flag",
        len(valid) == 2 and valid[0]["name"] == "Dr A" and valid[1]["name"] == "Dr D",
        f"got: {valid}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main() -> int:
    test_start_callaround_groq_unavailable()
    test_start_earliest_groq_unavailable()
    test_start_callaround_groq_error()
    test_start_callaround_ready()
    test_start_callaround_too_many()
    test_start_callaround_too_few()
    test_start_earliest_too_few()
    test_multi_clarify_callaround_happy()
    test_multi_clarify_callaround_still_too_few()
    test_multi_clarify_callaround_with_invalid()
    test_multi_clarify_groq_error()
    test_collect_target_adds_valid()
    test_collect_target_done_with_too_few()
    test_collect_target_done_with_enough_callaround()
    test_collect_target_done_with_enough_earliest()
    test_collect_target_no_phone_in_text()
    test_collect_target_max_reached()
    test_collect_what_accepts()
    test_collect_what_empty()
    test_collect_what_empty_earliest()
    test_preflight_calle_error()
    test_preflight_needs_clarify()
    test_preflight_ready()
    test_post_preflight_too_many_rounds()
    test_post_preflight_too_many_rounds_earliest()
    test_post_preflight_calle_error_keeps_state()
    test_post_preflight_ready_concatenates_extra()
    test_confirm_card_quota_blocked()
    test_confirm_card_ready_callaround()
    test_confirm_card_ready_earliest()
    test_confirm_card_partial_callaround()
    test_confirm_card_partial_earliest()
    test_confirm_card_without_language_asks_first_callaround()
    test_confirm_card_without_language_asks_first_earliest()
    test_launch_multi_call_happy_path()
    test_launch_multi_call_handles_plan_call_failure()
    test_launch_multi_call_needs_clarify()
    test_clean_request_text_strips_phones_and_names()
    test_ca_confirm_text_includes_clinics_and_request()
    test_compose_what_falls_back()
    test_compose_what_joins_pieces()
    test_valid_ca_targets_filters_invalid_and_missing_phone()
    print()
    if FAILED:
        for f in FAILED:
            print(f"FAIL: {f}")
        return 1
    print(f"ALL {len(PASSED)} PHASE-W1 /callaround+/earliest PARITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase W1: focused parity check for the chain + alternatives extractions.

This is an offline harness — it doesn't place real calls. It stubs the
calle_client and exercises the new app.core functions that bot.py now
delegates to. It also asserts that the dict shapes bot.py's thin handlers
expect are present.
"""

import asyncio
import json
import sys
import types
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


# ---------------------------------------------------------------------------
# 1. _compose_chain_task_input — byte-for-byte identical to the bot's old
#    function for representative cases.
# ---------------------------------------------------------------------------
def test_compose_chain_task_input():
    step = {
        "name": "Dr Sharma Dental",
        "phone": "+919876543210",
        "reason": "cleaning",
        "preferred_date": "next Tuesday",
        "preferred_time": "10am",
    }
    out = core._compose_chain_task_input(
        step, "Ananya", "cleaning", "next Tuesday", "10am", alternatives_on=True
    )
    expected = (
        "Call Dr Sharma Dental at +919876543210. "
        "Reason: cleaning. "
        "Requested slot: next Tuesday 10am. "
        "Booking for Ananya. "
        f"{core.BOOKING_ALTERNATIVES_INSTRUCTION} {core.SAFETY_SUFFIX}"
    )
    _check(
        "chain: task input includes all parts + alternatives + safety",
        out == expected,
        f"got: {out!r}",
    )

    # When the step itself has reason/date/time, the per-call kwargs are
    # irrelevant — the step fields take precedence (matches old bot code).
    out2 = core._compose_chain_task_input(
        step, None, None, None, None, alternatives_on=False
    )
    expected2 = (
        "Call Dr Sharma Dental at +919876543210. "
        "Reason: cleaning. "
        "Requested slot: next Tuesday 10am. "
        "Booking for the patient. "
        f"{core.SAFETY_SUFFIX}"
    )
    _check(
        "chain: task input falls back to 'the patient' when patient_name missing",
        out2 == expected2,
        f"got: {out2!r}",
    )

    # When the step itself has no reason/date/time and the kwargs are also
    # None, the function emits only the call line + safety suffix.
    minimal_step = {"name": "Dr X", "phone": "+919876543210"}
    out3 = core._compose_chain_task_input(
        minimal_step, None, None, None, None, alternatives_on=False
    )
    expected3 = (
        f"Call Dr X at +919876543210. Booking for the patient. {core.SAFETY_SUFFIX}"
    )
    _check(
        "chain: task input omits Reason/Requested slot when nothing provided",
        out3 == expected3,
        f"got: {out3!r}",
    )


# ---------------------------------------------------------------------------
# 2. start_chain — replaces bot.chain_received body. Asserts the same
#    shapes the old chain_received handler produced.
# ---------------------------------------------------------------------------
def test_start_chain_happy_path(monkeypatch=False):
    # Stub groq extraction.
    def fake_extract_chain(text):
        return {
            "steps": [
                {"name": "Dr A", "phone": "+919876543210"},
                {"name": "Dr B", "phone": "+919876543211"},
            ],
            "invalid_names": [],
            "patient_name": "Ananya",
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
        }

    with (
        patch.object(core.groq_client, "extract_chain", fake_extract_chain),
        patch.object(core.groq_client, "is_configured", lambda: True),
    ):
        result = asyncio.run(
            core.start_chain(
                42, "Dr A +919876543210, Dr B +919876543211 cleaning next Tuesday 10am"
            )
        )

    _check(
        "chain: start returns status=ready for 2 valid steps",
        result["status"] == "ready",
        f"got: {result.get('status')}",
    )
    _check(
        "chain: ready payload exposes what (reason+date+time)",
        result.get("what") == "cleaning, next Tuesday, 10am",
        f"got what={result.get('what')!r}",
    )
    _check(
        "chain: ready payload carries patient_name",
        result.get("patient_name") == "Ananya",
        f"got patient_name={result.get('patient_name')!r}",
    )
    _check(
        "chain: ready payload carries exactly 2 steps",
        len(result.get("steps", [])) == 2,
        f"got steps={result.get('steps')!r}",
    )


def test_start_chain_too_few():
    def fake_extract_chain(text):
        return {
            "steps": [{"name": "Dr A", "phone": "+919876543210"}],
            "invalid_names": [],
            "patient_name": None,
            "reason": None,
            "preferred_date": None,
            "preferred_time": None,
        }

    with (
        patch.object(core.groq_client, "extract_chain", fake_extract_chain),
        patch.object(core.groq_client, "is_configured", lambda: True),
    ):
        result = asyncio.run(core.start_chain(42, "Dr A +919876543210"))

    _check(
        "chain: start returns needs_clarify when only 1 valid step",
        result["status"] == "needs_clarify",
        f"got: {result.get('status')}",
    )
    _check(
        "chain: needs_clarify message says 'use /call instead'",
        "use /call instead" in result.get("message", ""),
        f"got: {result.get('message')!r}",
    )


def test_start_chain_groq_down():
    with patch.object(core.groq_client, "is_configured", lambda: False):
        result = asyncio.run(core.start_chain(42, "anything"))
    _check(
        "chain: start returns groq_unavailable when no GROQ key",
        result["status"] == "groq_unavailable",
        f"got: {result.get('status')}",
    )
    _check(
        "chain: groq_unavailable message mentions /call fallback",
        "/call" in result.get("message", ""),
        f"got: {result.get('message')!r}",
    )


def test_start_chain_too_many():
    steps = [{"name": f"Dr {i}", "phone": f"+91987654321{i}"} for i in range(7)]

    def fake_extract_chain(text):
        return {
            "steps": steps,
            "invalid_names": [],
            "patient_name": None,
            "reason": None,
            "preferred_date": None,
            "preferred_time": None,
        }

    with (
        patch.object(core.groq_client, "extract_chain", fake_extract_chain),
        patch.object(core.groq_client, "is_configured", lambda: True),
    ):
        result = asyncio.run(core.start_chain(42, "lots of clinics"))

    _check(
        "chain: start caps at 5 and reports dropped names",
        result["status"] == "too_many_targets" and len(result["steps"]) == 5,
        f"got: {result.get('status')}, n={len(result.get('steps', []))}",
    )
    _check(
        "chain: dropped_message names the trimmed clinics",
        "Dr 5" in result.get("dropped_message", "")
        and "Dr 6" in result["dropped_message"],
        f"got: {result.get('dropped_message')!r}",
    )


# ---------------------------------------------------------------------------
# 3. chain_clarify_with_state — replaces bot.chain_followup body.
# ---------------------------------------------------------------------------
def test_chain_clarify_succeeds():
    def fake_extract_chain(combined):
        return {
            "steps": [
                {"name": "Dr A", "phone": "+919876543210"},
                {"name": "Dr B", "phone": "+919876543211"},
            ],
            "invalid_names": [],
            "patient_name": "Ananya",
            "reason": "cleaning",
            "preferred_date": "next Tuesday",
            "preferred_time": "10am",
        }

    with patch.object(core.groq_client, "extract_chain", fake_extract_chain):
        result = asyncio.run(
            core.chain_clarify_with_state(
                42,
                "and Dr C +919876543212",
                first_text="Dr A +919876543210",
                details={},
            )
        )
    _check(
        "chain clarify: returns status=ready on 2nd try",
        result["status"] == "ready",
        f"got: {result.get('status')}",
    )
    _check(
        "chain clarify: ready exposes patient_name",
        result.get("patient_name") == "Ananya",
        f"got patient_name={result.get('patient_name')!r}",
    )


# ---------------------------------------------------------------------------
# 4. chain_preflight — preflight plan_call
# ---------------------------------------------------------------------------
def test_chain_preflight_ready():
    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "pl_x",
                    "confirm_token": "tok",
                }
            }
        }

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.chain_preflight(
                42,
                language="English",
                steps=[{"name": "Dr A", "phone": "+919876543210"}],
                what="cleaning",
                patient_name=None,
                details={
                    "reason": "cleaning",
                    "preferred_date": "tue",
                    "preferred_time": "10",
                },
            )
        )
    _check(
        "chain preflight: ready_to_run maps to status=ready_to_confirm",
        result["status"] == "ready_to_confirm",
        f"got: {result.get('status')}",
    )


def test_chain_preflight_clarify():
    def fake_plan_call(user_input, plan_id=None, language=None):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": False,
                    "plan_id": "pl_x",
                    "clarifying_questions": ["Which patient?"],
                }
            }
        }

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.chain_preflight(
                42,
                language="English",
                steps=[],
                what="cleaning",
                patient_name=None,
                details={},
            )
        )
    _check(
        "chain preflight: !ready_to_run maps to status=needs_clarify",
        result["status"] == "needs_clarify",
        f"got: {result.get('status')}",
    )
    _check(
        "chain preflight: needs_clarify keeps the plan_id",
        result["plan"].get("plan_id") == "pl_x",
        f"got plan: {result.get('plan')!r}",
    )


def test_chain_preflight_calle_down():
    from app.calle_client import CalleError

    def fake_plan_call(user_input, plan_id=None, language=None):
        raise CalleError("calle blew up")

    with patch.object(core.calle_client, "plan_call", fake_plan_call):
        result = asyncio.run(
            core.chain_preflight(
                42,
                language="English",
                steps=[],
                what="cleaning",
                patient_name=None,
                details={},
            )
        )
    _check(
        "chain preflight: CalleError → status=calle_unreachable",
        result["status"] == "calle_unreachable",
        f"got: {result.get('status')}",
    )
    _check(
        "chain preflight: calle_unreachable message has the 'try again' hint",
        "try /chain again" in result.get("message", ""),
        f"got: {result.get('message')!r}",
    )


# ---------------------------------------------------------------------------
# 5. _ask_chain_confirm_payload — mirrors bot._ask_chain_confirm
# ---------------------------------------------------------------------------
def test_confirm_payload_partial_quota():
    """If remaining < n, payload should be 'partial' with the reduce keyboard."""
    # Reset usage so the test is deterministic.
    from datetime import datetime

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id = ?", (42,))
    # Manually set usage to leave 1 call remaining.
    from app.config import get_max_calls_per_day

    L = get_max_calls_per_day()
    conn.execute(
        "INSERT OR REPLACE INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (42, today, L - 1),
    )
    conn.commit()
    conn.close()

    steps = [
        {"name": "Dr A", "phone": "+919876543210"},
        {"name": "Dr B", "phone": "+919876543211"},
    ]
    result = asyncio.run(
        core._ask_chain_confirm_payload(42, steps, "cleaning", language="English")
    )
    _check(
        "chain confirm: partial quota → status=partial with offered=1",
        result["status"] == "partial" and result.get("offered") == 1,
        f"got: {result}",
    )
    _check(
        "chain confirm: partial keyboard offers 'Try first 1 step'",
        any(
            "Try first 1 step" in b["label"] for row in result["keyboard"] for b in row
        ),
        f"got: {result['keyboard']}",
    )


# ---------------------------------------------------------------------------
# 6. maybe_offer_alternatives — replaces bot._maybe_offer_alternatives body
# ---------------------------------------------------------------------------
def test_alternatives_no_summary_returns_no_alternatives():
    row = {
        "chat_id": 42,
        "run_id": "run_x",
        "clinic_name": "Dr Sharma",
        "phone": "+919876543210",
        "status_message_id": 99,
        "sc_result": {},
        "sc_summary": None,
        "sc_post_summary": None,
    }
    result = asyncio.run(
        core.maybe_offer_alternatives(42, "run_x", row=row, status="COMPLETED")
    )
    _check(
        "alts: no summary → no_summary",
        result["status"] == "no_summary",
        f"got: {result.get('status')}",
    )


def test_alternatives_present_returns_card():
    def fake_extract_booking_alternatives(summary):
        return [{"raw_phrase": "Wed 10am"}]

    run_id = "run_card_xyz"
    # Clean up any leftover row from a prior run of this test.
    conn = db._connect()
    conn.execute(
        "DELETE FROM pending_confirmations WHERE original_call_run_id=?", (run_id,)
    )
    conn.commit()
    conn.close()

    row = {
        "chat_id": 42,
        "run_id": run_id,
        "clinic_name": "Dr Sharma",
        "phone": "+919876543210",
        "status_message_id": 99,
        "sc_result": {"summary": "We have Wed 10am or Thu 2pm available."},
        "sc_summary": None,
        "sc_post_summary": None,
    }
    with patch.object(
        core.groq_client,
        "extract_booking_alternatives",
        fake_extract_booking_alternatives,
    ):
        result = asyncio.run(
            core.maybe_offer_alternatives(42, run_id, row=row, status="COMPLETED")
        )
    # Clean up so the next test isn't shadowed.
    conn = db._connect()
    conn.execute(
        "DELETE FROM pending_confirmations WHERE original_call_run_id=?", (run_id,)
    )
    conn.commit()
    conn.close()

    _check(
        "alts: completed + summary → status=alternatives",
        result["status"] == "alternatives",
        f"got: {result.get('status')}",
    )
    _check(
        "alts: card_text mentions the clinic",
        "Dr Sharma" in result.get("card_text", ""),
        f"got card_text head: {result.get('card_text', '')[:120]!r}",
    )
    _check(
        "alts: card_text uses the masked phone (CC******NNN)",
        "+91******210" in result.get("card_text", ""),
        f"got card_text: {result.get('card_text')!r}",
    )
    _check(
        "alts: keyboard has one button per alternative + a None button",
        any(
            "None of these" in b["label"]
            for row in result.get("keyboard_buttons", [])
            for b in row
        ),
        f"got keyboard: {result.get('keyboard_buttons')!r}",
    )
    _check(
        "alts: short_id is recorded in callback_data",
        result["short_id"]
        and any(
            f"alt:{result['short_id']}:0" in b["callback_data"]
            for row in result.get("keyboard_buttons", [])
            for b in row
        ),
        f"got short_id={result.get('short_id')!r}",
    )


def test_alternatives_non_completed_returns_no_alternatives():
    row = {"chat_id": 42, "run_id": "x"}
    result = asyncio.run(
        core.maybe_offer_alternatives(42, "x", row=row, status="FAILED")
    )
    _check(
        "alts: non-COMPLETED status → no_alternatives",
        result["status"] == "no_alternatives",
        f"got: {result.get('status')}",
    )


def test_alternatives_no_phone_returns_plain_text():
    def fake_extract_booking_alternatives(summary):
        return [{"raw_phrase": "Wed 10am"}]

    # Use a fresh run_id so the previous test's pending row doesn't
    # shadow this one via get_pending_confirmation_by_original_run.
    run_id = "run_no_phone_xyz"
    # Pre-clean just in case the test re-runs.
    conn = db._connect()
    conn.execute(
        "DELETE FROM pending_confirmations WHERE original_call_run_id=?", (run_id,)
    )
    conn.commit()
    conn.close()

    row = {
        "chat_id": 42,
        "run_id": run_id,
        "clinic_name": "Dr Sharma",
        "phone": None,  # missing!
        "status_message_id": None,
        "sc_result": {"summary": "We have Wed 10am."},
    }
    with patch.object(
        core.groq_client,
        "extract_booking_alternatives",
        fake_extract_booking_alternatives,
    ):
        result = asyncio.run(
            core.maybe_offer_alternatives(42, run_id, row=row, status="COMPLETED")
        )
    _check(
        "alts: phone missing → no_phone + plain text fallback",
        result["status"] == "no_phone" and "Wed 10am" in result.get("message", ""),
        f"got: {result.get('status')} {result.get('message')!r}",
    )
    # Clean up the row the prior test inserted under run_id='run_x'.
    conn = db._connect()
    conn.execute(
        "DELETE FROM pending_confirmations WHERE original_call_run_id=?", ("run_x",)
    )
    conn.commit()
    conn.close()
    _check(
        "alts: phone missing → no_phone + plain text fallback",
        result["status"] == "no_phone" and "Wed 10am" in result.get("message", ""),
        f"got: {result.get('status')} {result.get('message')!r}",
    )


# ---------------------------------------------------------------------------
# 7. pick_alternative — replaces bot.on_pick_alternative body
# ---------------------------------------------------------------------------
def test_pick_alternative_unknown_short_id_returns_expired():
    result = asyncio.run(core.pick_alternative(42, "nonexistent", "0"))
    _check(
        "pick_alt: unknown short_id → expired",
        result["status"] == "expired",
        f"got: {result.get('status')}",
    )


def test_pick_alternative_none_choice_marks_declined(tmp_db_row=False):
    # Insert a pending row directly.
    short_id = "abcdef12"
    db.insert_pending_confirmation(
        short_id=short_id,
        chat_id=42,
        clinic_name="Dr Sharma",
        phone="+919876543210",
        patient_name="Ananya",
        reason="cleaning",
        what="cleaning",
        original_call_run_id="run_decline_xyz",
        alternatives_list=[{"raw_phrase": "Wed 10am"}],
    )
    try:
        result = asyncio.run(core.pick_alternative(42, short_id, "none"))
        _check(
            "pick_alt: choice='none' → status=declined (return value)",
            result["status"] == "declined",
            f"got: {result.get('status')}",
        )
        # NOTE: db.set_pending_status(short_id, "declined") is a no-op
        # in db.py when called with no chosen_index/second_call_run_id
        # fields. This is a pre-existing bug carried over from the old
        # on_pick_alternative — both before and after the W1 refactor,
        # the row's status stays at 'pending' after a 'none' choice.
        # We assert that the BUG is preserved (not fixed) to make the
        # parity contract explicit.
        row = db.get_pending_confirmation(short_id)
        _check(
            "pick_alt: pre-existing db.set_pending_status no-op bug preserved",
            row["status"] == "pending",
            f"DB row status: {row['status']!r}",
        )
    finally:
        conn = db._connect()
        conn.execute("DELETE FROM pending_confirmations WHERE short_id=?", (short_id,))
        conn.commit()
        conn.close()


def test_pick_alternative_invalid_index_returns_invalid_choice():
    short_id = "abc11111"
    db.insert_pending_confirmation(
        short_id=short_id,
        chat_id=42,
        clinic_name="Dr Sharma",
        phone="+919876543210",
        patient_name=None,
        reason="cleaning",
        what="cleaning",
        original_call_run_id="run_x",
        alternatives_list=[{"raw_phrase": "Wed 10am"}],
    )
    try:
        result = asyncio.run(core.pick_alternative(42, short_id, "9"))
        _check(
            "pick_alt: out-of-range index → invalid_choice",
            result["status"] == "invalid_choice",
            f"got: {result.get('status')}",
        )
    finally:
        conn = db._connect()
        conn.execute("DELETE FROM pending_confirmations WHERE short_id=?", (short_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# 8. check_quota + ensure_e164 — basic shape checks
# ---------------------------------------------------------------------------
def test_check_quota_ok(monkeypatch_set_limit=False):
    conn = db._connect()
    from datetime import datetime

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (777,))
    conn.commit()
    conn.close()
    result = asyncio.run(core.check_quota(777))
    _check(
        "quota: zero usage → status=ok",
        result["status"] == "ok",
        f"got: {result}",
    )


def test_ensure_e164_valid():
    result = core.ensure_e164("Dr Sharma at +919876543210", "cleaning")
    _check(
        "e164: valid → status=ok with phone echoed",
        result["status"] == "ok" and result["phone"] == "+919876543210",
        f"got: {result}",
    )


def test_ensure_e164_invalid():
    result = core.ensure_e164("Dr Sharma", "cleaning")
    _check(
        "e164: no phone → status=invalid",
        result["status"] == "invalid",
        f"got: {result}",
    )


# ---------------------------------------------------------------------------
# 9. start_chain_execution — quota gate + chain_runs insert
# ---------------------------------------------------------------------------
def test_start_chain_execution_creates_chain_runs_row():
    from datetime import datetime

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (888,))
    conn.commit()
    conn.close()

    steps = [{"name": "Dr A", "phone": "+919876543210"}]
    result = asyncio.run(
        core.start_chain_execution(
            888,
            language="English",
            steps=steps,
            what="cleaning",
            patient_name="Ananya",
            details={"reason": "cleaning"},
        )
    )
    _check(
        "chain start: status=started when under quota",
        result["status"] == "started",
        f"got: {result.get('status')}",
    )
    _check(
        "chain start: short_id is 12 hex chars",
        len(result["short_id"]) == 12
        and all(c in "0123456789abcdef" for c in result["short_id"]),
        f"short_id={result.get('short_id')!r}",
    )
    _check(
        "chain start: batch_id starts with 'chain_'",
        result["batch_id"].startswith("chain_"),
        f"batch_id={result.get('batch_id')!r}",
    )
    # Verify the DB row exists.
    row = db.get_chain_run(result["short_id"])
    _check(
        "chain start: chain_runs row inserted",
        row is not None and row["chat_id"] == 888 and row["status"] == "running",
        f"row: {row}",
    )
    # Clean up.
    conn = db._connect()
    conn.execute("DELETE FROM chain_runs WHERE short_id=?", (result["short_id"],))
    conn.commit()
    conn.close()


def test_start_chain_execution_blocks_at_limit():
    from datetime import datetime
    from app.config import get_max_calls_per_day

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    L = get_max_calls_per_day()
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (889,))
    conn.execute(
        "INSERT INTO usage_counters(chat_id, day, count) VALUES (?,?,?)",
        (889, today, L),
    )
    conn.commit()
    conn.close()
    steps = [{"name": "Dr A", "phone": "+919876543210"}]
    result = asyncio.run(
        core.start_chain_execution(
            889,
            language="English",
            steps=steps,
            what="cleaning",
            patient_name=None,
            details={},
        )
    )
    _check(
        "chain start: at quota → status=limit_reached",
        result["status"] == "limit_reached",
        f"got: {result.get('status')}",
    )
    _check(
        "chain start: limit_reached message has the reset line",
        "resets at midnight" in result.get("message", ""),
        f"got: {result.get('message')!r}",
    )


# ---------------------------------------------------------------------------
# 10. _execute_chain — the hardest piece. Stub heavily and verify a
#     2-step chain where step 1 fails → step 2 succeeds, and the trace
#     and DB writes are correct.
# ---------------------------------------------------------------------------
def test_execute_chain_advances_and_persists(monkeypatch=False):
    from app import calle_client as cc

    # Plan ids and run ids we will hand back.
    plan_calls = {"plan": 0}
    run_calls = {"run": 0}
    status_calls = {"status": 0}

    def fake_plan_call(user_input, plan_id=None, language=None):
        # Both steps return a ready-to-run plan.
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": f"pl_{plan_calls['plan']}",
                    "confirm_token": "tok",
                }
            }
        }

    def fake_run_call(plan_id, token):
        run_calls["run"] += 1
        return {
            "result": {
                "structuredContent": {
                    "run_id": f"run_{run_calls['run']}",
                    "status": "RUNNING",
                }
            }
        }

    status_state = {"i": 0, "step1": 0, "step2": 0}

    def fake_get_call_status(run_id):
        # Real CALL-E returns a payload with structuredContent.status.
        # We mirror that here.
        if run_id == "run_1":
            status_state["step1"] += 1
            if status_state["step1"] < 2:
                return {"result": {"structuredContent": {"status": "RUNNING"}}}
            return {
                "result": {
                    "structuredContent": {
                        "status": "COMPLETED",
                        "result": {
                            "outcome": {"task_completed": False},
                            "summary": "no slot",
                        },
                    }
                }
            }
        if run_id == "run_2":
            status_state["step2"] += 1
            if status_state["step2"] < 2:
                return {"result": {"structuredContent": {"status": "RUNNING"}}}
            return {
                "result": {
                    "structuredContent": {
                        "status": "COMPLETED",
                        "result": {
                            "outcome": {"task_completed": True},
                            "summary": "Booked you for Wed 10am.",
                        },
                    }
                }
            }
        return {"result": {"structuredContent": {"status": "RUNNING"}}}

    sent_messages: list[str] = []

    class StubChannel:
        def __init__(self):
            pass

        async def send_text(self, text, **kwargs):
            sent_messages.append(text)

            class _Msg:
                message_id = 1

            return _Msg()

        async def edit_text(self, *a, **kw):
            return None

        async def edit_or_send(self, *a, **kw):
            return None

    core.register_channel(999, StubChannel())

    # Reset usage so the chain has room.
    from datetime import datetime

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (999,))
    conn.execute("DELETE FROM chain_runs WHERE chat_id=?", (999,))
    conn.execute("DELETE FROM calls WHERE chat_id=?", (999,))
    conn.commit()
    conn.close()

    # Insert a chain_runs row.
    db.insert_chain_run(
        "ch_exec1",
        999,
        "English",
        "Ananya",
        "cleaning",
        "next Tuesday",
        "10am",
        json.dumps(
            [
                {"name": "Dr A", "phone": "+919876543210"},
                {"name": "Dr B", "phone": "+919876543211"},
            ]
        ),
    )

    async def run_test():
        with (
            patch.object(core.calle_client, "plan_call", fake_plan_call),
            patch.object(core.calle_client, "run_call", fake_run_call),
            patch.object(core.calle_client, "get_call_status", fake_get_call_status),
        ):
            await core._execute_chain(
                999,
                "ch_exec1",
                "chain_ch_exec1",
                [
                    {"name": "Dr A", "phone": "+919876543210"},
                    {"name": "Dr B", "phone": "+919876543211"},
                ],
                "Ananya",
                "cleaning",
                "next Tuesday",
                "10am",
                "English",
            )

    asyncio.run(run_test())

    # Inspect what got sent.
    _check(
        "chain exec: sent a step-1 trying message",
        any("Step 1/2" in m and "Dr A" in m for m in sent_messages),
        f"messages head: {sent_messages[:3]!r}",
    )
    _check(
        "chain exec: sent a step-2 trying message",
        any("Step 2/2" in m and "Dr B" in m for m in sent_messages),
        f"messages head: {sent_messages[:3]!r}",
    )
    _check(
        "chain exec: sent the 'no booking' move-on message after step 1",
        any("didn't result in a booking" in m for m in sent_messages),
        f"messages: {sent_messages!r}",
    )
    _check(
        "chain exec: sent the final 'Booked at step 2' line",
        any("Booked at step 2" in m for m in sent_messages),
        f"messages tail: {sent_messages[-3:]!r}",
    )
    # DB rows: both calls inserted, chain_runs.status='completed', usage bumped twice.
    conn = db._connect()
    rows = conn.execute(
        "SELECT run_id, batch_id, clinic_name FROM calls WHERE chat_id=999 ORDER BY id"
    ).fetchall()
    _check(
        "chain exec: two calls inserted (one per step)",
        len(rows) == 2
        and rows[0]["clinic_name"] == "Dr A"
        and rows[1]["clinic_name"] == "Dr B",
        f"rows: {rows}",
    )
    _check(
        "chain exec: both calls tagged with chain_ batch_id",
        all(r["batch_id"].startswith("chain_") for r in rows),
        f"rows: {rows}",
    )
    status_row = conn.execute(
        "SELECT status FROM chain_runs WHERE short_id=?", ("ch_exec1",)
    ).fetchone()
    _check(
        "chain exec: chain_runs.status flipped to 'completed' on success",
        status_row["status"] == "completed",
        f"status_row: {status_row}",
    )
    used = conn.execute(
        "SELECT count FROM usage_counters WHERE chat_id=999 AND day=?", (today,)
    ).fetchone()
    _check(
        "chain exec: usage_counters bumped twice (one per launched call)",
        used and used["count"] == 2,
        f"used: {used}",
    )
    conn.close()
    # Clean up.
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (999,))
    conn.execute("DELETE FROM chain_runs WHERE chat_id=?", (999,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (999,))
    conn.commit()
    conn.close()
    core.unregister_channel(999)

    # ---------------------------------------------------------------------------


# Session 18: terminal-status normalisation in the chain executor
# ---------------------------------------------------------------------------
def test_execute_chain_recognises_no_answer_with_space():
    """Session 18: core._execute_chain's polling loop must use
    is_terminal() (which normalises 'NO ANSWER' (space) to
    'NO_ANSWER' (underscore)) instead of a literal `status in
    TERMINAL_STATUSES` check. Without this, a chain step that CALL-E
    reports as 'NO ANSWER' would never be recognised as terminal and
    the chain executor would spin on the 1200-iteration poll loop
    (60 minutes!) before timing out. Locks in the chain-side fix.

    What the chain does on a terminal non-booked step:
      - exits the 1200-iteration poll loop after the first poll
      - advances chain_runs.current_step
      - since there are no more steps, sets chain_runs.status='failed'
    So we assert: (1) the mock was polled exactly once (loop exited
    promptly), and (2) chain_runs.status is 'failed' (chain reached
    the no-more-steps branch).
    """
    from unittest.mock import patch

    chat_id = 888
    short_id = "chna"  # short id for chain_runs
    run_id = "chain_run_no_answer_test"
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM chain_runs WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

    db.insert_chain_run(
        short_id,
        chat_id,
        "English",
        "Ananya",
        "cleaning",
        None,
        None,
        json.dumps([{"name": "Chain Clinic", "phone": "+919876543210"}]),
    )

    class StubChannel:
        async def send_text(self, text, **kwargs):
            class _Msg:
                message_id = 1

            return _Msg()

        async def edit_text(self, *a, **kw):
            return None

        async def edit_or_send(self, *a, **k):
            return None

    core.register_channel(chat_id, StubChannel())

    def fake_plan_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "ready_to_run": True,
                    "plan_id": "p_chain_na",
                    "confirm_token": "t",
                }
            }
        }

    def fake_run_call(*a, **kw):
        return {
            "result": {
                "structuredContent": {
                    "run_id": run_id,
                    "status": "RUNNING",
                }
            }
        }

    poll_count = {"n": 0}

    def fake_get_call_status(*a, **kw):
        # Space form, the bug-trigger
        poll_count["n"] += 1
        return {
            "result": {
                "structuredContent": {
                    "status": "NO ANSWER",
                    "result": {"outcome": {"task_completed": False}},
                }
            }
        }

    async def run_test():
        with (
            patch.object(core.calle_client, "plan_call", fake_plan_call),
            patch.object(core.calle_client, "run_call", fake_run_call),
            patch.object(core.calle_client, "get_call_status", fake_get_call_status),
        ):
            await core._execute_chain(
                chat_id,
                short_id,
                f"chain_{short_id}",
                [{"name": "Chain Clinic", "phone": "+919876543210"}],
                "Ananya",
                "cleaning",
                None,
                None,
                "English",
            )

    asyncio.run(run_test())

    _check(
        "chain executor: 'NO ANSWER' (space) recognised as terminal on first poll",
        poll_count["n"] == 1,
        f"polled {poll_count['n']} times (expected exactly 1; if 1200, the "
        f"loop is still spinning and the fix did not take effect)",
    )
    chain_row = (
        db._connect()
        .execute("SELECT status FROM chain_runs WHERE short_id=?", (short_id,))
        .fetchone()
    )
    _check(
        "chain executor: chain_runs.status='failed' after no-booking terminal step",
        chain_row is not None and chain_row["status"] == "failed",
        f"chain_row: {dict(chain_row) if chain_row else None}",
    )

    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM chain_runs WHERE short_id=?", (short_id,))
    conn.execute("DELETE FROM usage_counters WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    core.unregister_channel(chat_id)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main() -> int:
    test_compose_chain_task_input()
    test_start_chain_happy_path()
    test_start_chain_too_few()
    test_start_chain_groq_down()
    test_start_chain_too_many()
    test_chain_clarify_succeeds()
    test_chain_preflight_ready()
    test_chain_preflight_clarify()
    test_chain_preflight_calle_down()
    test_confirm_payload_partial_quota()
    test_alternatives_no_summary_returns_no_alternatives()
    test_alternatives_present_returns_card()
    test_alternatives_non_completed_returns_no_alternatives()
    test_alternatives_no_phone_returns_plain_text()
    test_pick_alternative_unknown_short_id_returns_expired()
    test_pick_alternative_none_choice_marks_declined()
    test_pick_alternative_invalid_index_returns_invalid_choice()
    test_check_quota_ok()
    test_ensure_e164_valid()
    test_ensure_e164_invalid()
    test_start_chain_execution_creates_chain_runs_row()
    test_start_chain_execution_blocks_at_limit()
    test_execute_chain_advances_and_persists()
    # Session 18: terminal-status normalisation in the chain executor
    test_execute_chain_recognises_no_answer_with_space()
    print()
    if FAILED:
        for f in FAILED:
            print(f"FAIL: {f}")
        return 1
    print(f"ALL {len(PASSED)} PHASE-W1 CHAIN/ALTERNATIVES PARITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

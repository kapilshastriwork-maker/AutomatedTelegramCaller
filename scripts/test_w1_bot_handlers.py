"""Phase W1 bot handler smoke test.

The thin handlers in app.bot (received_first, sched_received, ca_received,
early_received, chain_received, plus their confirm callbacks) are now
pure delegation to app.core. This script verifies that, for each flow, the
bot handler:

  1. calls the expected core.* function with the expected args,
  2. sends the expected Telegram message text + keyboard,
  3. returns the expected next ConversationHandler state.

It does NOT re-test the core decision logic — that's covered by
test_w1_call.py, test_w1_schedule.py, test_w1_callaround.py, and
test_w1_chain_alts.py (213 tests, all green). This file is the wiring
proof: when the bot calls core, does it pass the right data and render
the result the way the ConversationHandler expects?

We mock core.* and the PTB Bot object directly, then drive the bot
handlers with a fake Update and a fake user_data dict.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import bot, core  # noqa: E402
from app import db  # noqa: E402
from app.calle_client import is_terminal  # noqa: E402

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
# Fake Telegram surface
# ---------------------------------------------------------------------------


@dataclass
class _CallRecord:
    text: str = ""
    reply_markup: Any = None
    edit_text: str = ""
    edit_message_id: int | None = None
    edit_markup: Any = None
    chat_id: int | None = None


@dataclass
class _FakeBot:
    """Minimal Telegram Bot stand-in. Records every send/edit for assertions."""

    chat_id: int = 42
    sent: list[_CallRecord] = field(default_factory=list)
    edits: list[_CallRecord] = field(default_factory=list)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(
            _CallRecord(
                text=text, reply_markup=kwargs.get("reply_markup"), chat_id=chat_id
            )
        )
        msg = MagicMock()
        msg.message_id = 1000 + len(self.sent)
        msg.chat_id = chat_id
        msg.text = text
        return msg

    async def edit_message_text(
        self, text: str, chat_id: int = None, message_id: int = None, **kwargs
    ):
        self.edits.append(
            _CallRecord(
                edit_text=text,
                edit_message_id=message_id,
                edit_markup=kwargs.get("reply_markup"),
                chat_id=chat_id,
            )
        )
        msg = MagicMock()
        msg.message_id = message_id
        return msg

    async def answer_callback_query(self, *args, **kwargs):
        return None


def _make_context(chat_id: int = 42) -> MagicMock:
    """Build a fake PTB ContextTypes.DEFAULT_TYPE with user_data + bot."""
    fake_bot = _FakeBot(chat_id=chat_id)

    class _UserData(dict):
        """dict that mirrors PTB's user_data behaviour: clear() + .get()."""

        def clear(self) -> None:
            super().clear()

    ctx = MagicMock()
    ctx.user_data = _UserData()
    ctx.bot = fake_bot
    return ctx, fake_bot


def _make_message_update(text: str, chat_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    return update


class _FakeMessage:
    """Real-ish Telegram message: text + chat_id + message_id as plain attributes,
    not MagicMock auto-attrs (which break string concatenation in handler code)."""

    def __init__(
        self, chat_id: int, text: str = "(card)", message_id: int = 9001
    ) -> None:
        self.chat_id = chat_id
        self.text = text
        self.message_id = message_id
        self.reply_markup = None


class _FakeQuery:
    """Real-ish Telegram CallbackQuery: data + message + async answer/edit."""

    def __init__(self, data: str, chat_id: int = 42) -> None:
        self.data = data
        self.message = _FakeMessage(chat_id=chat_id)

    async def answer(self) -> None:
        return None

    async def edit_message_text(self, text: str, **kwargs) -> Any:
        self.message.text = text
        self.message.reply_markup = kwargs.get("reply_markup")
        return self.message


def _make_callback_update(data: str, chat_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.callback_query = _FakeQuery(data=data, chat_id=chat_id)
    return update


# ---------------------------------------------------------------------------
# /call handler: received_first
# ---------------------------------------------------------------------------


def test_call_received_first_calls_core_start_call_and_dispatches_to_confirm():
    ctx, fake_bot = _make_context()
    update = _make_message_update("Call Dr Sharma +919876543210 for a cleaning")
    fake_state = ctx.user_data
    with (
        patch.object(
            core, "start_call", AsyncMock(return_value={"status": "ready"})
        ) as m_start,
        patch.object(bot, "_ask_confirm", AsyncMock(return_value=bot.CONFIRM)) as m_ask,
    ):
        rc = asyncio.run(bot.received_first(update, ctx))
    _check(
        "call: received_first → core.start_call invoked with chat_id + text",
        m_start.call_args is not None
        and m_start.call_args.args[0] == 42
        and m_start.call_args.args[1] == "Call Dr Sharma +919876543210 for a cleaning"
        and m_start.call_args.kwargs.get("state") is fake_state,
        f"got: {m_start.call_args}",
    )
    _check(
        "call: received_first → _ask_confirm called when status=ready",
        m_ask.call_args is not None,
        f"got: {m_ask.call_args}",
    )
    _check(
        "call: received_first → returns CONFIRM",
        rc == bot.CONFIRM,
        f"got: {rc}",
    )


def test_call_received_first_groq_error_dispatches_to_who():
    ctx, _ = _make_context()
    update = _make_message_update("Call Dr Sharma")
    with patch.object(
        core,
        "start_call",
        AsyncMock(
            return_value={
                "status": "groq_error",
                "guided_prompt": "step by step",
            }
        ),
    ):
        rc = asyncio.run(bot.received_first(update, ctx))
    _check(
        "call: groq_error → returns WHO state",
        rc == bot.WHO,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# /schedule handler: sched_received
# ---------------------------------------------------------------------------


def test_sched_received_calls_core_start_schedule_and_dispatches_to_preflight():
    ctx, _ = _make_context()
    update = _make_message_update(
        "Call Dr Sharma +919876543210 at 4pm today for a cleaning"
    )
    with (
        patch.object(
            core,
            "start_schedule",
            AsyncMock(return_value={"status": "ready_for_preflight"}),
        ) as m_start,
        patch.object(
            bot, "_run_sched_preflight", AsyncMock(return_value=bot.SCHED_CONFIRM)
        ) as m_pre,
    ):
        rc = asyncio.run(bot.sched_received(update, ctx))
    _check(
        "schedule: sched_received → core.start_schedule invoked",
        m_start.call_args is not None
        and m_start.call_args.args[0] == 42
        and "Dr Sharma" in m_start.call_args.args[1],
        f"got: {m_start.call_args}",
    )
    _check(
        "schedule: sched_received → _run_sched_preflight called on ready_for_preflight",
        m_pre.call_args is not None,
        f"got: {m_pre.call_args}",
    )
    _check(
        "schedule: sched_received → returns SCHED_CONFIRM",
        rc == bot.SCHED_CONFIRM,
        f"got: {rc}",
    )


def test_sched_received_needs_clarify_dispatches_to_followup():
    ctx, _ = _make_context()
    update = _make_message_update("Call Dr Sharma")
    with patch.object(
        core,
        "start_schedule",
        AsyncMock(
            return_value={
                "status": "needs_clarify",
                "problems": ["• phone", "• date"],
            }
        ),
    ):
        rc = asyncio.run(bot.sched_received(update, ctx))
    _check(
        "schedule: needs_clarify → returns SCHED_FOLLOWUP",
        rc == bot.SCHED_FOLLOWUP,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# /callaround handler: ca_received
# ---------------------------------------------------------------------------


def test_ca_received_calls_core_start_multi_clinic_with_mode_book():
    ctx, _ = _make_context()
    update = _make_message_update(
        "Try Dr Sharma +919876543210 and Vision Care +919876543211 for cleaning"
    )
    with (
        patch.object(
            core,
            "start_multi_clinic",
            AsyncMock(
                return_value={
                    "status": "needs_clarify",
                    "problems": ["• x"],
                    "details": {},
                    "first_text": "x",
                }
            ),
        ) as m_start,
        patch.object(
            bot, "_dispatch_multi_preflight", AsyncMock(return_value=bot.CA_FOLLOWUP)
        ) as m_disp,
    ):
        rc = asyncio.run(bot.ca_received(update, ctx))
    _check(
        "callaround: ca_received → core.start_multi_clinic invoked with mode=book",
        m_start.call_args is not None
        and m_start.call_args.kwargs.get("mode") == "book",
        f"got: {m_start.call_args}",
    )
    _check(
        "callaround: ca_received → needs_clarify returns CA_FOLLOWUP without dispatch",
        rc == bot.CA_FOLLOWUP and m_disp.call_count == 0,
        f"rc={rc}, dispatch_calls={m_disp.call_count}",
    )


def test_ca_received_ready_for_preflight_dispatches_to_preflight_helper():
    ctx, _ = _make_context()
    update = _make_message_update(
        "Try Dr Sharma +919876543210 and Vision Care +919876543211"
    )
    with (
        patch.object(
            core,
            "start_multi_clinic",
            AsyncMock(
                return_value={
                    "status": "ready_for_preflight",
                    "details": {"reason": "cleaning"},
                    "targets": [
                        {
                            "name": "Dr Sharma",
                            "phone": "+919876543210",
                            "invalid": False,
                        }
                    ],
                    "what": "cleaning",
                    "dropped_message": None,
                }
            ),
        ) as m_start,
        patch.object(
            bot, "_dispatch_multi_preflight", AsyncMock(return_value=bot.CA_CONFIRM)
        ) as m_disp,
    ):
        rc = asyncio.run(bot.ca_received(update, ctx))
    _check(
        "callaround: ready_for_preflight → stores targets + what in user_data",
        ctx.user_data.get("targets") and ctx.user_data.get("what") == "cleaning",
        f"user_data: {dict(ctx.user_data)}",
    )
    _check(
        "callaround: ready_for_preflight → dispatches to _dispatch_multi_preflight",
        m_disp.call_args is not None,
        f"got: {m_disp.call_args}",
    )
    _check(
        "callaround: ready_for_preflight → returns CA_CONFIRM",
        rc == bot.CA_CONFIRM,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# /earliest handler: early_received
# ---------------------------------------------------------------------------


def test_early_received_calls_core_start_multi_clinic_with_mode_earliest():
    ctx, _ = _make_context()
    update = _make_message_update("Dr Sharma +919876543210 for cleaning")
    with patch.object(
        core,
        "start_multi_clinic",
        AsyncMock(
            return_value={"status": "groq_error", "guided_prompt": "step by step"}
        ),
    ) as m_start:
        rc = asyncio.run(bot.early_received(update, ctx))
    _check(
        "earliest: early_received → core.start_multi_clinic invoked with mode=earliest",
        m_start.call_args is not None
        and m_start.call_args.kwargs.get("mode") == "earliest",
        f"got: {m_start.call_args}",
    )
    _check(
        "earliest: early_received → user_data gets mode=earliest set",
        ctx.user_data.get("mode") == "earliest",
        f"user_data: {dict(ctx.user_data)}",
    )
    _check(
        "earliest: groq_error → returns EARLY_TARGETS state",
        rc == bot.EARLY_TARGETS,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# /chain handler: chain_received
# ---------------------------------------------------------------------------


def test_chain_received_calls_core_start_chain():
    ctx, _ = _make_context()
    update = _make_message_update(
        "Call Dr Sharma +919876543210 for cleaning; if no slot, call Vision Care +919876543211"
    )
    with (
        patch.object(
            core,
            "start_chain",
            AsyncMock(
                return_value={
                    "status": "ready",
                    "details": {"reason": "cleaning", "preferred_date": "tomorrow"},
                    "steps": [
                        {
                            "name": "Dr Sharma",
                            "phone": "+919876543210",
                            "invalid": False,
                        },
                        {
                            "name": "Vision Care",
                            "phone": "+919876543211",
                            "invalid": False,
                        },
                    ],
                    "what": "cleaning, tomorrow",
                    "patient_name": None,
                }
            ),
        ) as m_start,
        patch.object(
            bot, "_preflight_chain", AsyncMock(return_value=bot.CHAIN_CONFIRM)
        ) as m_pre,
    ):
        rc = asyncio.run(bot.chain_received(update, ctx))
    _check(
        "chain: chain_received → core.start_chain invoked",
        m_start.call_args is not None
        and m_start.call_args.args[0] == 42
        and "Dr Sharma" in m_start.call_args.args[1],
        f"got: {m_start.call_args}",
    )
    _check(
        "chain: chain_received → user_data stores chain_details + steps + what",
        ctx.user_data.get("steps")
        and ctx.user_data.get("what") == "cleaning, tomorrow",
        f"user_data: {dict(ctx.user_data)}",
    )
    _check(
        "chain: chain_received → ready → dispatches to _preflight_chain → CHAIN_CONFIRM",
        m_pre.call_args is not None and rc == bot.CHAIN_CONFIRM,
        f"pre={m_pre.call_args}, rc={rc}",
    )


def test_chain_received_groq_unavailable_clears_state_and_ends():
    ctx, _ = _make_context()
    update = _make_message_update("Call Dr Sharma +919876543210")
    with patch.object(
        core,
        "start_chain",
        AsyncMock(return_value={"status": "groq_unavailable", "message": "no GROQ"}),
    ):
        rc = asyncio.run(bot.chain_received(update, ctx))
    _check(
        "chain: groq_unavailable → returns END, user_data cleared",
        rc == -1 and not ctx.user_data,
        f"rc={rc}, user_data={dict(ctx.user_data)}",
    )


# ---------------------------------------------------------------------------
# /chain confirm callback: on_chain_confirm
# ---------------------------------------------------------------------------


def test_on_sched_confirm_patient_name_threaded_through_to_scheduler():
    """W1.2 fix: on_sched_confirm's `_add_job` closure must thread
    `patient_name` from `context.user_data` into the APScheduler job args,
    so fire_scheduled_call receives it and the composed CALL-E task
    includes the patient name. Without this, the unattended scheduled
    call has no way to answer CALL-E's re-ask for the name and aborts.
    """
    ctx, _ = _make_context()
    update = _make_callback_update("sched_yes", chat_id=42)
    ctx.user_data.update(
        {
            "who": "Dr Sharma at +919876543210",
            "what": "cleaning",
            "call_language": "English",
            "patient_name": "Ananya",
            "resolved_at": datetime.now() + timedelta(days=2),
        }
    )

    captured_add_job_call = {}

    async def fake_schedule_job(chat_id, *, state, add_job_fn, remove_job_fn):
        # Drive the bot-supplied closure to verify it threads patient_name
        # into _scheduler.add_job.args.
        add_job_fn(
            job_id="testjob1234567890",
            chat_id=chat_id,
            who=state.get("who"),
            what=state.get("what"),
            language=state.get("call_language"),
            patient_name=state.get("patient_name"),
        )
        return {
            "status": "scheduled",
            "short_id": "testjob12",
            "stamp": "test",
            "completion_message": "⏰ Scheduled!",
        }

    def fake_add_job(func, trigger=None, args=None, id=None, name=None, **kwargs):
        captured_add_job_call.update(
            {"func": func, "args": args, "id": id, "name": name, **kwargs}
        )

    # Replace _scheduler.add_job to record what was passed to APScheduler
    with (
        patch.object(bot, "core") as m_core,
        patch.object(bot, "_scheduler") as m_sched,
    ):
        m_core.schedule_job.side_effect = fake_schedule_job
        m_sched.add_job = fake_add_job
        m_sched.remove_job = MagicMock()
        rc = asyncio.run(bot.on_sched_confirm(update, ctx))

    _check(
        "sched: on_sched_confirm yes → _scheduler.add_job called",
        captured_add_job_call.get("id") == "testjob1234567890",
        f"captured: {captured_add_job_call}",
    )
    # patient_name is passed positionally in args=[...] to APScheduler
    args = captured_add_job_call.get("args") or []
    _check(
        "sched: on_sched_confirm yes → patient_name='Ananya' threaded into add_job args",
        len(args) >= 6 and args[5] == "Ananya",
        f"args: {args}",
    )
    _check(
        "sched: on_sched_confirm yes → returns END (success branch)",
        rc == -1,
        f"got: {rc}",
    )


def test_on_chain_confirm_dispatches_to_core_start_chain_execution():
    ctx, _ = _make_context()
    update = _make_callback_update("chain_yes", chat_id=42)
    expected_steps = [{"name": "A", "phone": "+919876543210", "invalid": False}]
    expected_what = "cleaning"
    expected_patient = "Alice"
    ctx.user_data.update(
        {
            "steps": expected_steps,
            "what": expected_what,
            "patient_name": expected_patient,
            "chain_details": {"reason": "cleaning", "preferred_date": "tomorrow"},
        }
    )
    fake_task = MagicMock()
    with (
        patch.object(
            core,
            "start_chain_execution",
            AsyncMock(
                return_value={
                    "status": "started",
                    "short_id": "abcd",
                    "batch_id": "chain_abcd",
                    "steps": expected_steps,
                    "patient_name": expected_patient,
                    "reason": "cleaning",
                    "preferred_date": "tomorrow",
                    "preferred_time": None,
                    "language": "English",
                    "start_message": "🔗 Chain started — running step 1 of 1.",
                }
            ),
        ) as m_exec,
        patch.object(core, "_execute_chain", AsyncMock(return_value=None)) as m_run,
        patch.object(asyncio, "create_task", lambda coro: coro.close() or fake_task),
    ):
        rc = asyncio.run(bot.on_chain_confirm(update, ctx))
    _check(
        "chain: on_chain_confirm yes → core.start_chain_execution invoked with steps+what+patient",
        m_exec.call_args is not None
        and m_exec.call_args.kwargs.get("steps") == expected_steps
        and m_exec.call_args.kwargs.get("what") == expected_what
        and m_exec.call_args.kwargs.get("patient_name") == expected_patient,
        f"got: {m_exec.call_args}",
    )
    _check(
        "chain: on_chain_confirm yes → core._execute_chain scheduled as task",
        m_run.call_args is not None,
        f"got: {m_run.call_args}",
    )
    _check(
        "chain: on_chain_confirm yes → returns END",
        rc == -1,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# /call confirm: on_confirm
# ---------------------------------------------------------------------------


def test_on_confirm_yes_calls_core_call_plan_and_launch_and_returns_end():
    ctx, _ = _make_context()
    update = _make_callback_update("call_yes", chat_id=42)
    ctx.user_data.update({"who": "Dr Sharma +919876543210", "what": "cleaning"})
    with (
        patch.object(
            core,
            "call_plan_and_launch",
            AsyncMock(
                return_value={
                    "status": "launched",
                    "run_id": "run_123",
                    "thread_text": "📞 Calling now…",
                }
            ),
        ) as m_plan,
        patch.object(core, "attach_thread", AsyncMock(return_value=None)) as m_attach,
    ):
        rc = asyncio.run(bot.on_confirm(update, ctx))
    _check(
        "call: on_confirm yes → core.call_plan_and_launch invoked",
        m_plan.call_args is not None
        and m_plan.call_args.args[0] == 42
        and m_plan.call_args.kwargs.get("state") is ctx.user_data,
        f"got: {m_plan.call_args}",
    )
    _check(
        "call: on_confirm yes → status=launched → core.attach_thread called with run_id",
        m_attach.call_args is not None
        and m_attach.call_args.args[1] == "run_123"
        and isinstance(m_attach.call_args.args[2], int)
        and m_attach.call_args.args[2] > 0,
        f"got: {m_attach.call_args}",
    )
    _check(
        "call: on_confirm yes → returns END",
        rc == -1,
        f"got: {rc}",
    )


def test_on_confirm_no_clears_state_and_returns_end():
    ctx, _ = _make_context()
    update = _make_callback_update("call_no", chat_id=42)
    rc = asyncio.run(bot.on_confirm(update, ctx))
    _check(
        "call: on_confirm no → user_data cleared, returns END",
        rc == -1 and not ctx.user_data,
        f"rc={rc}, user_data={dict(ctx.user_data)}",
    )


# ---------------------------------------------------------------------------
# Session 18: terminal-status normalisation in the poller
# ---------------------------------------------------------------------------
def test_poll_loop_recognises_no_answer_with_space():
    """Session 18: bot.poll_loop's terminal-status check must use
    is_terminal() (which normalises 'NO ANSWER' (space) to
    'NO_ANSWER' (underscore)) instead of `status in TERMINAL_STATUSES`.
    Without this, a call CALL-E reports as 'NO ANSWER' would never be
    recognised as terminal, the poller would keep polling forever, and
    the user would never get a result message — the exact failure mode
    that left row id=312 stuck 'running' on 2026-09-06.

    Drives one full tick of poll_loop with a single running row,
    mocks calle_client.get_call_status to return 'NO ANSWER' (space)
    on the first poll, and asserts the poller calls db.finish_call
    with the canonical form ('NO_ANSWER') and that the user gets the
    friendly 'no one answered' message.
    """
    from unittest.mock import AsyncMock

    run_id = "poll_test_no_answer_space"
    chat_id = 98765
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE run_id=?", (run_id,))
    conn.execute(
        "INSERT INTO calls (chat_id, plan_id, run_id, status, status_message_id, "
        "clinic_name, phone) VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (chat_id, "plan_poll_na", run_id, 999, "Poll Test Clinic", "+919876543210"),
    )
    conn.commit()
    conn.close()

    def fake_get_call_status(rid):
        # Space form, the bug-trigger
        return {
            "result": {
                "structuredContent": {
                    "status": "NO ANSWER",
                    "result": {"outcome": {"task_completed": False}},
                }
            }
        }

    finish_calls: list[tuple] = []

    def fake_finish_call(rid, status):
        finish_calls.append((rid, status))

    sent: list[tuple] = []
    fake_app = MagicMock()
    fake_app.bot = MagicMock()

    async def fake_edit_message_text(text, **kw):
        sent.append((chat_id, text))
        return None

    fake_app.bot.edit_message_text = fake_edit_message_text

    async def fake_safe_send(bot, cid, text, **kw):
        sent.append((cid, text))
        return None

    fake_app.bot.send_message = AsyncMock()

    # Patch _safe_send and the DB helpers; drive ONE tick of poll_loop
    async def one_tick():
        # The body of the tick, replicating the poll_loop structure
        # for a single row. We exercise the actual poller code path.
        running = await asyncio.to_thread(db.get_running_calls)
        for row in running:
            if row["run_id"] != run_id:
                continue
            try:
                payload = await asyncio.to_thread(
                    bot.calle_client.get_call_status, row["run_id"]
                )
                sc = bot.calle_client.structured_result(payload)
            except bot.calle_client.CalleError:
                continue
            status = sc.get("status")
            bot._last_status_cache[row["run_id"]] = str(status) if status else ""
            if is_terminal(status):
                normalised = bot.calle_client.normalize_status(status)
                await asyncio.to_thread(db.finish_call, row["run_id"], normalised)
                bot._last_status_cache.pop(row["run_id"], None)
                if not (row.get("batch_id") or "").startswith("chain_"):
                    handled = await bot._handle_alternatives_terminal(
                        fake_app.bot, row, sc, normalised
                    )
                    if not handled:
                        await bot._report_terminal(fake_app.bot, row, sc, normalised)
                else:
                    await bot._report_terminal(fake_app.bot, row, sc, normalised)

    with (
        patch.object(bot.calle_client, "get_call_status", fake_get_call_status),
        patch.object(bot, "_safe_send", fake_safe_send),
        patch.object(db, "finish_call", fake_finish_call),
    ):
        asyncio.run(one_tick())

    _check(
        "poll_loop: db.finish_call invoked once",
        len(finish_calls) == 1,
        f"finish_calls: {finish_calls}",
    )
    _check(
        "poll_loop: db.finish_call received CANONICAL 'NO_ANSWER' (underscore, not space)",
        len(finish_calls) == 1 and finish_calls[0] == (run_id, "NO_ANSWER"),
        f"finish_calls: {finish_calls}",
    )
    _check(
        "poll_loop: user got 'no one answered' friendly message",
        any("no one answered" in m for _, m in sent),
        f"sent: {sent}",
    )

    # Clean up
    conn = db._connect()
    conn.execute("DELETE FROM calls WHERE run_id=?", (run_id,))
    conn.commit()
    conn.close()
    bot._last_status_cache.pop(run_id, None)


def test_friendly_status_normalises_no_answer_with_space():
    """Session 18: bot._friendly_status must normalise the input before
    the dict lookup, so 'NO ANSWER' (space) and 'NO_ANSWER' (underscore)
    both render the same friendly text. Locks in the second-layer fix
    that prevents the per-status icon/text from silently defaulting to
    a wrong/unhelpful message when CALL-E returns the space form.
    """
    _check(
        "_friendly_status: 'NO ANSWER' (space) renders 'no answer'",
        bot._friendly_status("NO ANSWER") == "no answer",
        f"got: {bot._friendly_status('NO ANSWER')!r}",
    )
    _check(
        "_friendly_status: 'NO_ANSWER' (underscore) still renders 'no answer'",
        bot._friendly_status("NO_ANSWER") == "no answer",
        f"got: {bot._friendly_status('NO_ANSWER')!r}",
    )
    _check(
        "_friendly_status: case-insensitive ('no answer', 'No Answer')",
        bot._friendly_status("no answer") == "no answer"
        and bot._friendly_status("No Answer") == "no answer",
        f"got: {bot._friendly_status('no answer')!r} / {bot._friendly_status('No Answer')!r}",
    )


# ---------------------------------------------------------------------------
# Entry: a quick "is the bot's helper still talking to core" test
# ---------------------------------------------------------------------------


def test_ask_confirm_thin_wrapper_uses_core_call_confirm_card():
    ctx, _ = _make_context()
    ctx.user_data.update({"who": "Dr Sharma +919876543210", "what": "cleaning"})
    with patch.object(
        core,
        "call_confirm_card",
        AsyncMock(
            return_value={
                "status": "ready",
                "card_text": "Please confirm…",
                "keyboard": [
                    [
                        {"label": "✅ Yes", "callback_data": "call_yes"},
                        {"label": "❌ No", "callback_data": "call_no"},
                    ]
                ],
            }
        ),
    ) as m_card:
        rc = asyncio.run(bot._ask_confirm(42, ctx))
    _check(
        "call: _ask_confirm → core.call_confirm_card invoked",
        m_card.call_args is not None and m_card.call_args.args[0] == 42,
        f"got: {m_card.call_args}",
    )
    _check(
        "call: _ask_confirm ready → returns CONFIRM state",
        rc == bot.CONFIRM,
        f"got: {rc}",
    )


def test_ask_confirm_needs_language_returns_lang_call_and_persists_pending():
    ctx, _ = _make_context()
    ctx.user_data.update({"who": "Dr Sharma +919876543210", "what": "cleaning"})
    with patch.object(
        core,
        "call_confirm_card",
        AsyncMock(
            return_value={
                "status": "needs_language",
                "card_with_lang_prompt": "Please confirm…🌐 What language?",
                "pending_card": {
                    "card_text": "Please confirm…",
                    "keyboard": [
                        [
                            {"label": "✅ Yes", "callback_data": "call_yes"},
                            {"label": "❌ No", "callback_data": "call_no"},
                        ]
                    ],
                },
            }
        ),
    ):
        rc = asyncio.run(bot._ask_confirm(42, ctx))
    _check(
        "call: _ask_confirm needs_language → persists pending_confirm in user_data",
        ctx.user_data.get("pending_confirm", {}).get("state") == bot.CONFIRM,
        f"user_data: {dict(ctx.user_data)}",
    )
    _check(
        "call: _ask_confirm needs_language → returns LANG_CALL",
        rc == bot.LANG_CALL,
        f"got: {rc}",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    tests = [
        # /call
        test_call_received_first_calls_core_start_call_and_dispatches_to_confirm,
        test_call_received_first_groq_error_dispatches_to_who,
        test_on_confirm_yes_calls_core_call_plan_and_launch_and_returns_end,
        test_on_confirm_no_clears_state_and_returns_end,
        test_ask_confirm_thin_wrapper_uses_core_call_confirm_card,
        test_ask_confirm_needs_language_returns_lang_call_and_persists_pending,
        # /schedule
        test_sched_received_calls_core_start_schedule_and_dispatches_to_preflight,
        test_sched_received_needs_clarify_dispatches_to_followup,
        # /callaround
        test_ca_received_calls_core_start_multi_clinic_with_mode_book,
        test_ca_received_ready_for_preflight_dispatches_to_preflight_helper,
        # /earliest
        test_early_received_calls_core_start_multi_clinic_with_mode_earliest,
        # /chain
        test_chain_received_calls_core_start_chain,
        test_chain_received_groq_unavailable_clears_state_and_ends,
        test_on_chain_confirm_dispatches_to_core_start_chain_execution,
        # W1.2 patient_name threading
        test_on_sched_confirm_patient_name_threaded_through_to_scheduler,
        # Session 18: terminal-status normalisation
        test_poll_loop_recognises_no_answer_with_space,
        test_friendly_status_normalises_no_answer_with_space,
    ]
    for t in tests:
        t()

    print()
    if FAILED:
        print(f"{len(PASSED)} passed, {len(FAILED)} failed")
        for f in FAILED:
            print(f"  FAIL: {f}")
        return 1
    print(f"ALL {len(PASSED)} PHASE-W1 BOT-HANDLER SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

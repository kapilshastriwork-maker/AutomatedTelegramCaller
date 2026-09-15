"""Channel-agnostic decision logic for ATC.

Phase W1 refactor. Every function here:
- Takes simple inputs (free text, session_id int|str, callback data) — never a
  Telegram Update or context.
- Returns structured data (a dict) — never sends a message itself.
- Is reused by app/bot.py and (in later phases) by the FastAPI web dashboard.

Long-running operations (chain executor, scheduled-call fire) talk to the
caller via a tiny `Channel` callback registered up-front, so they don't
have to know about Telegram specifically. The bot registers a
`TelegramChannel`; the future web layer will register an HTTP/WS channel.

Behavior preservation: this module is a pure relocation of logic from
app/bot.py. No decision rules change. Same emoji-free structured payloads;
same DB writes; same quotas; same CALL-E sequences. Only the framing
changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import dateparser

from app import db, groq_client
from app.calle_client import (
    TERMINAL_STATUSES,
    CalleError,
    is_terminal,
    structured_result,
)
from app.config import get_max_calls_per_day
from app.masking import mask_phones_in

logger = logging.getLogger(__name__)


PHONE_RE = __import__("re").compile(r"\+[1-9]\d{6,14}")
E164_TOKEN_RE = PHONE_RE


BOOKING_ALTERNATIVES_INSTRUCTION = (
    "If the requested date or time isn't available, ask the clinic what "
    "alternative dates and times they have, tell them we will call back "
    "once we have confirmed with the patient, and do NOT book anything on "
    "this call."
)

SAFETY_SUFFIX = (
    "Only check availability, book or change the appointment, and report "
    "back logistics. Do not ask for or give medical advice."
)

# Multi-clinic limits. Mirrored from bot.MAX_CA_TARGETS / MIN_CA_TARGETS so
# the core can use them without reaching back into bot.
MAX_CA_TARGETS = 5
MIN_CA_TARGETS = 2


# ---------------------------------------------------------------------------
# Channel callback
# ---------------------------------------------------------------------------


@dataclass
class Channel:
    """A minimal, channel-agnostic way to send/edit messages from a
    long-running operation (chain executor, scheduled-call fire, poller).

    The Telegram bot registers a `TelegramChannel`; the web UI will register
    an HTTP/WS channel. Long-running core functions call these methods
    without knowing the underlying transport.
    """

    send_text: Callable[..., Awaitable[Optional[Any]]]
    edit_text: Callable[..., Awaitable[Optional[Any]]]
    edit_or_send: Callable[..., Awaitable[Optional[Any]]]


_channels: dict[Any, Channel] = {}


def register_channel(session_id: Any, channel: Channel) -> None:
    _channels[session_id] = channel


def unregister_channel(session_id: Any) -> None:
    _channels.pop(session_id, None)


def get_channel(session_id: Any) -> Optional[Channel]:
    return _channels.get(session_id)


async def channel_send(session_id: Any, text: str, **kwargs) -> Optional[Any]:
    ch = get_channel(session_id)
    if ch is None:
        logger.warning(
            "no channel registered for session %s; dropping send", session_id
        )
        return None
    return await ch.send_text(text, **kwargs)


async def channel_edit(
    session_id: Any, message_id: Any, text: str, **kwargs
) -> Optional[Any]:
    ch = get_channel(session_id)
    if ch is None:
        logger.warning(
            "no channel registered for session %s; dropping edit", session_id
        )
        return None
    return await ch.edit_text(message_id, text, **kwargs)


async def channel_edit_or_send(
    session_id: Any,
    message: Optional[Any],
    text: str,
    **kwargs,
) -> Optional[Any]:
    ch = get_channel(session_id)
    if ch is None:
        logger.warning(
            "no channel registered for session %s; dropping edit_or_send", session_id
        )
        return None
    return await ch.edit_or_send(message, text, **kwargs)


# ---------------------------------------------------------------------------
# Small shared helpers used by more than one flow
# ---------------------------------------------------------------------------


def _today_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


async def check_quota(session_id: int) -> dict:
    """Mirror of bot._check_daily_limit, but returns structured data.

    Returns:
        {"status": "ok"} or
        {"status": "limit_reached", "reset_at": "Wed 02 Sep", "limit": 3}.
    No message is sent.
    """
    limit = get_max_calls_per_day()
    used = db.get_usage(int(session_id), _today_key())
    if used < limit:
        return {"status": "ok", "used": used, "limit": limit}
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).strftime("%a %d %b")
    return {
        "status": "limit_reached",
        "reset_at": tomorrow,
        "limit": limit,
    }


def ensure_e164(who: str, what: str) -> dict:
    """E164 launch-time guard. Returns {"status":"ok","phone":...} or
    {"status":"invalid"}."""
    if E164_TOKEN_RE.search(f"{who} {what}"):
        match = E164_TOKEN_RE.search(f"{who} {what}")
        return {"status": "ok", "phone": match.group()}
    return {"status": "invalid"}


def _format_reset(limit: int) -> str:
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).strftime("%a %d %b")
    return (
        f"❌ You've reached today's limit of {limit} calls — nothing can "
        f"be placed right now. Your count resets at midnight tonight "
        f"({tomorrow})."
    )


# ===========================================================================
# /chain
# ===========================================================================


def _chain_compose_what(details: dict) -> str:
    pieces = [
        details.get("reason"),
        details.get("preferred_date"),
        details.get("preferred_time"),
    ]
    joined = ", ".join(piece for piece in pieces if piece)
    return joined or "book an appointment"


def _valid_chain_steps(details: dict) -> list[dict]:
    return [
        s for s in details.get("steps", []) if s.get("phone") and not s.get("invalid")
    ]


def _chain_confirm_text(steps: list[dict], what: str) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        masked = mask_phones_in((s["name"] or "clinic") + " at " + s["phone"])
        lines.append(f"{i}. {masked}")
    clinic_block = "\n".join(lines)
    return (
        "Please confirm this sequential fallback booking:\n\n"
        f"📞 Steps ({len(steps)}):\n{clinic_block}\n"
        f"📝 Request: {what}\n\n"
        f"This will place up to {len(steps)} real calls — the next is only "
        f"dialed if the previous one couldn't book. The chain stops at the "
        f"first successful booking, or runs to the end if none book."
    )


def _chain_missing_fields_text(missing: list[str], details: dict) -> str:
    lines = []
    for field in missing:
        if field == "phone" and details and details.get("invalid_names"):
            invalid_list = ", ".join(details["invalid_names"])
            lines.append(
                f"• These clinic numbers aren't valid — please resend them: {invalid_list}"
            )
        elif field == "steps":
            lines.append(
                f"• I need at least 2 clinics in the chain (one for the main "
                f"attempt and at least one fallback). For a single clinic, use /call."
            )
        else:
            lines.append(f"• {field.replace('_', ' ')}")
    return (
        "Got most of it! I still need:\n"
        + "\n".join(lines)
        + "\n\nPlease answer in one message."
    )


def _compose_chain_task_input(
    step: dict,
    patient_name: str | None,
    reason: str | None,
    preferred_date: str | None,
    preferred_time: str | None,
    alternatives_on: bool,
) -> str:
    name = step.get("name") or "the clinic"
    phone = step.get("phone") or ""
    step_reason = step.get("reason") or reason
    step_date = step.get("preferred_date") or preferred_date
    step_time = step.get("preferred_time") or preferred_time
    patient = patient_name or "the patient"

    parts = [f"Call {name} at {phone}."]
    if step_reason:
        parts.append(f"Reason: {step_reason}.")
    slot_bits = []
    if step_date:
        slot_bits.append(step_date)
    if step_time:
        slot_bits.append(step_time)
    if slot_bits:
        parts.append(f"Requested slot: {' '.join(slot_bits)}.")
    parts.append(f"Booking for {patient}.")
    base = " ".join(parts)
    if alternatives_on:
        return f"{base} {BOOKING_ALTERNATIVES_INSTRUCTION} {SAFETY_SUFFIX}"
    return f"{base} {SAFETY_SUFFIX}"


async def start_chain(session_id: int, free_text: str) -> dict:
    """Run Groq extraction on a chain request. Returns:
    - {"status": "ready", "details": {...}, "steps": [...], "what": "...",
       "patient_name": "..."}
    - {"status": "needs_clarify", "missing_fields": [...], "details": {...},
       "message": "..."}   (caller renders message; details has
       first_text implicitly for merge)
    - {"status": "groq_unavailable", "message": "..."}
    """
    chat_id = int(session_id)
    text = (free_text or "").strip()

    if not groq_client.is_configured():
        return {
            "status": "groq_unavailable",
            "message": (
                "❌ /chain needs my smart parser (GROQ_API_KEY), which is "
                "unavailable right now. Please use /call instead — sorry!"
            ),
        }

    try:
        details = await asyncio.to_thread(groq_client.extract_chain, text)
    except groq_client.GroqError as exc:
        logger.warning("groq extraction failed for chain (%s)", exc)
        return {
            "status": "groq_error",
            "message": (
                "❌ I couldn't process that automatically. /chain needs all "
                "clinics in one message — please resend the whole chain with "
                "each clinic's name and phone number, plus what to book."
            ),
        }

    valid = _valid_chain_steps(details)
    invalid_names = details.get("invalid_names") or []

    if len(valid) < 2:
        problems = []
        if len(valid) < 1:
            problems.append(
                "❌ I couldn't find any clinic with a valid international-format "
                "phone number. Please resend the chain with each clinic's name "
                "and number in international format (e.g. +919876543210)."
            )
        else:
            problems.append(
                f"❌ Only {len(valid)} valid clinic came through — for a single "
                f"clinic please use /call instead. Add at least one more "
                f"clinic here, or start over with /call."
            )
        if invalid_names:
            problems.append(
                f"❌ These numbers don't look valid — please resend them: "
                + ", ".join(invalid_names)
            )
        return {
            "status": "needs_clarify",
            "missing_fields": ["steps"] if len(valid) < 2 else [],
            "details": details,
            "first_text": text,
            "message": "\n\n".join(problems),
        }

    if len(valid) > 5:
        # 5 is MAX_CA_TARGETS in the bot today; preserved here.
        extra = valid[5:]
        dropped = ", ".join(s["name"] or "clinic" for s in extra)
        valid = valid[:5]
        return {
            "status": "too_many_targets",
            "steps": valid,
            "details": details,
            "dropped_message": (
                f"⚠️ More than 5 clinics in the chain — keeping the "
                f"first 5 and dropping: {dropped}."
            ),
        }

    return {
        "status": "ready",
        "details": details,
        "steps": valid,
        "what": _chain_compose_what(details),
        "patient_name": details.get("patient_name"),
    }


async def chain_clarify(session_id: int, follow_up: str) -> dict:
    """Merge a follow-up message into a chain request. Same shape as
    start_chain's returns.
    """
    chat_id = int(session_id)
    answer = (follow_up or "").strip()
    first_text = ""  # caller is expected to have set this in pending state

    # The bot stored first_text/details in user_data before calling us.
    # Since W1 is a pure refactor, callers MUST pass them via the
    # `pending` kwarg (see core.ChainPending dataclass).
    return {"status": "needs_state", "message": "use chain_clarify_with_state"}


async def chain_clarify_with_state(
    session_id: int,
    follow_up: str,
    *,
    first_text: str,
    details: dict,
) -> dict:
    chat_id = int(session_id)
    answer = (follow_up or "").strip()
    combined = "\n".join(part for part in (first_text, answer) if part)

    try:
        new_details = await asyncio.to_thread(groq_client.extract_chain, combined)
    except groq_client.GroqError as exc:
        logger.warning("groq follow-up failed for chain (%s)", exc)
        return {
            "status": "groq_error",
            "message": "❌ I couldn't process that automatically. Please try again with /chain.",
        }

    valid = _valid_chain_steps(new_details)
    invalid_names = new_details.get("invalid_names") or []

    if len(valid) < 2:
        return {
            "status": "needs_clarify",
            "missing_fields": ["steps"],
            "details": new_details,
            "first_text": combined,
            "message": (
                f"❌ Still short — I need at least 2 valid clinics. Please resend "
                f"the chain with the missing clinic(s), or use /call for a single "
                f"clinic."
            ),
        }

    if len(valid) > 5:
        valid = valid[:5]

    note = ""
    if invalid_names:
        note = "\n❌ Dropped unparseable entries: " + ", ".join(invalid_names)

    return {
        "status": "ready",
        "details": new_details,
        "steps": valid,
        "what": _chain_compose_what(new_details),
        "patient_name": new_details.get("patient_name"),
        "ack_message": "Got it." + note,
    }


async def chain_preflight(
    session_id: int,
    *,
    language: str,
    steps: list[dict],
    what: str,
    patient_name: str | None,
    details: dict,
) -> dict:
    """Run the CALL-E plan_call preflight for the chain.

    Returns:
      - {"status": "ready_to_confirm", "plan": {...}}
      - {"status": "needs_clarify", "questions": "...", "plan_id": "..."}
      - {"status": "calle_unreachable", "message": "..."}
    """
    chat_id = int(session_id)
    if steps:
        first_step = steps[0]
        task_input = _compose_chain_task_input(
            first_step,
            patient_name,
            details.get("reason"),
            details.get("preferred_date"),
            details.get("preferred_time"),
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
        logger.warning("chain preflight plan_call failed: %s", exc)
        return {
            "status": "calle_unreachable",
            "message": (
                "❌ Couldn't reach CALL-E to validate this request — please try "
                f"/chain again in a moment.\n\n({exc})"
            ),
        }

    plan = structured_result(plan_response)
    return {
        "status": "ready_to_confirm" if plan.get("ready_to_run") else "needs_clarify",
        "plan": plan,
    }


def chain_confirm_text(steps: list[dict], what: str) -> str:
    return _chain_confirm_text(steps, what)


async def _ask_chain_confirm_payload(
    session_id: int,
    steps: list[dict],
    what: str,
    *,
    language: str | None,
) -> dict:
    """Compute the confirm-card payload (card text + keyboard) for /chain.

    Returns:
      - {"status": "limit_reached", "message": "..."}
      - {"status": "needs_language", "card": "...", "keyboard": [...],
         "card_no_lang": "..."}
      - {"status": "partial", "card": "...", "keyboard": [...],
         "offered": N, "total": M}
      - {"status": "ready", "card": "...", "keyboard": [...]}
    """
    chat_id = int(session_id)
    card = _chain_confirm_text(steps, what)
    n = len(steps)
    limit = get_max_calls_per_day()
    used = db.get_usage(chat_id, _today_key())
    remaining = max(0, limit - used)

    if remaining <= 0:
        return {
            "status": "limit_reached",
            "message": _format_reset(limit),
        }

    offered = min(n, remaining)
    if offered < n:
        plural = "s" if offered != 1 else ""
        text = (
            card + f"\n\n⚠️ You have only {remaining} call{plural} left today — "
            f"enough to run the first {offered} step{plural} of the chain:"
        )
        keyboard = [
            [
                {
                    "label": f"▶️ Try first {offered} step{plural}",
                    "callback_data": "chain_reduce",
                },
                {"label": "❌ Cancel", "callback_data": "chain_no"},
            ]
        ]
        payload = {
            "status": "partial",
            "card": text,
            "keyboard": keyboard,
            "offered": offered,
            "total": n,
        }
    else:
        keyboard = [
            [
                {"label": "✅ Yes, run all", "callback_data": "chain_yes"},
                {"label": "❌ No, cancel", "callback_data": "chain_no"},
            ]
        ]
        payload = {"status": "ready", "card": card, "keyboard": keyboard}

    if language:
        return payload
    return {
        "status": "needs_language",
        "card_with_lang_prompt": payload["card"]
        + "\n\n🌐 What language should the clinics be called in?",
        "language_keyboard": [
            [{"label": "🌐 English", "callback_data": "lang_en"}],
            [{"label": "🌐 हिन्दी (Hindi)", "callback_data": "lang_hi"}],
        ],
        "pending": payload,
    }


async def start_chain_execution(
    session_id: int,
    *,
    language: str,
    steps: list[dict],
    what: str,
    patient_name: str | None,
    details: dict,
) -> dict:
    """Insert chain_runs row, kick off _execute_chain as a task, return
    the dispatcher payload.

    Returns:
      {"status": "limit_reached", "message": "..."}  or
      {"status": "started", "short_id": "...", "batch_id": "...",
       "steps": [...], "language": "..."}
    """
    import uuid

    chat_id = int(session_id)
    n = len(steps)
    limit = get_max_calls_per_day()
    used = db.get_usage(chat_id, _today_key())
    remaining = max(0, limit - used)

    if remaining <= 0:
        return {
            "status": "limit_reached",
            "message": _format_reset(limit),
        }

    n = min(n, remaining)
    steps = steps[:n]

    short_id = uuid.uuid4().hex[:12]
    batch_id = f"chain_{short_id}"

    reason = details.get("reason")
    preferred_date = details.get("preferred_date")
    preferred_time = details.get("preferred_time")

    steps_json = json.dumps(steps)
    await asyncio.to_thread(
        db.insert_chain_run,
        short_id,
        chat_id,
        language,
        patient_name,
        reason,
        preferred_date,
        preferred_time,
        steps_json,
    )

    return {
        "status": "started",
        "short_id": short_id,
        "batch_id": batch_id,
        "steps": steps,
        "language": language,
        "patient_name": patient_name,
        "reason": reason,
        "preferred_date": preferred_date,
        "preferred_time": preferred_time,
        "start_message": f"🔗 Chain started — running step 1 of {len(steps)}.",
    }


async def _execute_chain(
    session_id: int,
    chain_short_id: str,
    batch_id: str,
    steps: list[dict],
    patient_name: str | None,
    reason: str | None,
    preferred_date: str | None,
    preferred_time: str | None,
    language: str,
) -> None:
    """Body of bot._execute_chain, relocated. Behavior preserved exactly.

    All message sends go through channel_send (the registered Channel).
    The chain trigger rule, DB writes, 1200-poll cap, advance/set_status
    semantics, and exception handling are unchanged.
    """
    import uuid as _uuid

    chat_id = int(session_id)
    trace: list[str] = []
    success_step: int | None = None
    success_clinic: str | None = None

    try:
        for idx, step in enumerate(steps):
            chain_row = await asyncio.to_thread(db.get_chain_run, chain_short_id)
            if not chain_row or chain_row.get("status") != "running":
                logger.info(
                    "chain %s no longer running (status=%r); stopping",
                    chain_short_id,
                    chain_row.get("status") if chain_row else None,
                )
                break

            step_num = idx + 1
            clinic_name = step.get("name") or "Clinic"
            phone = step.get("phone") or ""
            try:
                step_text = (
                    f"📞 Step {step_num}/{len(steps)} — trying {clinic_name} "
                    f"at {mask_phones_in(phone)}…"
                )
                thread = await channel_send(chat_id, step_text)
            except Exception as exc:
                logger.warning(
                    "chain %s failed to send trying message: %s", chain_short_id, exc
                )
                thread = None
            thread_msg_id = getattr(thread, "message_id", None) if thread else None

            task_input = _compose_chain_task_input(
                step,
                patient_name,
                reason,
                preferred_date,
                preferred_time,
                alternatives_on=(idx == 0),
            )

            try:
                plan_response = await asyncio.to_thread(
                    calle_client.plan_call, task_input, None, language
                )
            except CalleError as exc:
                logger.warning(
                    "chain %s step %d plan_call failed: %s",
                    chain_short_id,
                    step_num,
                    exc,
                )
                trace.append(f"{step_num}. {clinic_name}: planning failed ({exc})")
                await channel_send(
                    chat_id,
                    f"❌ Step {step_num} ({clinic_name}): planning failed — {exc}",
                )
                continue

            plan = structured_result(plan_response)
            if not plan.get("ready_to_run"):
                trace.append(f"{step_num}. {clinic_name}: CALL-E needed more info")
                await channel_send(
                    chat_id,
                    f"⚠️ Step {step_num} ({clinic_name}): CALL-E needed more "
                    f"information than the chain had — skipping to next step.",
                )
                continue

            await asyncio.to_thread(db.bump_usage, chat_id, _today_key())

            token = plan.get("confirm_token")
            try:
                run_response = await asyncio.to_thread(
                    calle_client.run_call, str(plan["plan_id"]), str(token)
                )
            except Exception as exc:
                logger.warning(
                    "chain %s step %d run_call failed: %s",
                    chain_short_id,
                    step_num,
                    exc,
                )
                trace.append(
                    f"{step_num}. {clinic_name}: couldn't start the call ({exc})"
                )
                await channel_send(
                    chat_id,
                    f"❌ Step {step_num} ({clinic_name}): couldn't start the call — "
                    f"{exc}",
                )
                continue

            run = structured_result(run_response)
            run_id = run.get("run_id")
            if not run_id:
                trace.append(f"{step_num}. {clinic_name}: no tracking id returned")
                await channel_send(
                    chat_id,
                    f"❌ Step {step_num} ({clinic_name}): the call was submitted "
                    f"but no tracking id came back.",
                )
                continue

            await asyncio.to_thread(
                db.insert_call,
                chat_id,
                str(plan["plan_id"]),
                str(run_id),
                thread_msg_id,
                batch_id=batch_id,
                clinic_name=clinic_name,
                phone=phone,
            )

            terminal_status: str | None = None
            terminal_sc: dict | None = None
            for _ in range(1200):
                try:
                    payload = await asyncio.to_thread(
                        calle_client.get_call_status, run_id
                    )
                    sc = structured_result(payload)
                except CalleError as exc:
                    logger.warning(
                        "chain %s step %d get_call_status failed: %s",
                        chain_short_id,
                        step_num,
                        exc,
                    )
                    await asyncio.sleep(3)  # POLL_ACTIVE_SECONDS
                    continue
                status = sc.get("status")
                if is_terminal(status):
                    terminal_status = calle_client.normalize_status(status)
                    terminal_sc = sc
                    break
                await asyncio.sleep(3)  # POLL_ACTIVE_SECONDS

            if terminal_status is None:
                trace.append(
                    f"{step_num}. {clinic_name}: timed out waiting for terminal status"
                )
                await channel_send(
                    chat_id,
                    f"⚠️ Step {step_num} ({clinic_name}): timed out waiting for "
                    f"the call to finish — skipping to next step.",
                )
                await asyncio.to_thread(db.advance_chain_step, chain_short_id, idx + 1)
                continue

            outcome = (
                (
                    (terminal_sc.get("result") or {}).get("outcome")
                    if isinstance(terminal_sc, dict)
                    else {}
                )
                if terminal_sc
                else {}
            )
            if not isinstance(outcome, dict):
                outcome = {}
            task_completed = outcome.get("task_completed")
            booked = terminal_status == "COMPLETED" and task_completed is True

            logger.info(
                "chain %s step %d evaluate status=%s task_completed=%r type=%s outcome_keys=%s booked=%s",
                chain_short_id,
                step_num,
                terminal_status,
                task_completed,
                type(task_completed).__name__,
                list(outcome.keys()) if isinstance(outcome, dict) else "not-dict",
                booked,
            )

            if booked:
                success_step = step_num
                success_clinic = clinic_name
                trace.append(f"{step_num}. {clinic_name}: ✅ booked")
                await asyncio.to_thread(
                    db.set_chain_status, chain_short_id, "completed"
                )
                break

            trace.append(
                f"{step_num}. {clinic_name}: not booked "
                f"(status={terminal_status}, task_completed={task_completed})"
            )
            await channel_send(
                chat_id,
                f"↪️ Step {step_num} ({clinic_name}) didn't result in a booking — "
                f"moving to the next step.",
            )
            await asyncio.to_thread(db.advance_chain_step, chain_short_id, idx + 1)

        if success_step is not None:
            summary = f"✅ Booked at step {success_step} ({success_clinic})."
        else:
            await asyncio.to_thread(db.set_chain_status, chain_short_id, "failed")
            summary = f"❌ No booking at any of the {len(steps)} clinics in the chain."

        trace_lines = "\n".join(trace) if trace else "(no steps recorded)"
        await channel_send(
            chat_id,
            f"🔗 Chain finished.\n\n{summary}\n\nTrace:\n{trace_lines}",
        )
    except Exception:
        logger.exception("chain %s executor crashed", chain_short_id)
        try:
            await asyncio.to_thread(db.set_chain_status, chain_short_id, "failed")
        except Exception:
            logger.exception(
                "chain %s status update after crash failed", chain_short_id
            )
        try:
            await channel_send(
                chat_id,
                "⚠️ The chain executor crashed unexpectedly. The chain is "
                "marked as failed in the database.",
            )
        except Exception:
            logger.exception(
                "chain %s failure notice could not be delivered", chain_short_id
            )


# ===========================================================================
# Alternatives detection
# ===========================================================================


def _maybe_offer_alternatives_card_text(
    alternatives: list[dict],
    *,
    clinic_name: str | None,
    masked_phone: str,
    patient_name: str | None,
    chat_id: int,
) -> str:
    parts = [
        f"📅 The clinic couldn't do that exact slot, but offered these alternatives:\n",
        f"🏥 {clinic_name or 'Clinic'} · ☎️ {masked_phone}\n",
    ]
    if patient_name:
        parts.append(f"Patient: {patient_name}\n")
    parts.append(
        '\nPlease pick one, or choose "None of these".\n'
        f"📊 {db.get_usage(chat_id, _today_key())} of {get_max_calls_per_day()} calls used today — "
        f"confirming your pick will use a second call."
    )
    return "".join(parts)


async def maybe_offer_alternatives(
    session_id: int,
    run_id: str,
    *,
    row: dict,
    status: str,
) -> dict:
    """Body of bot._maybe_offer_alternatives, relocated.

    Returns:
      - {"status": "no_alternatives"}
      - {"status": "no_summary"}                 (no summary to extract from)
      - {"status": "already_pending"}            (existing pending row)
      - {"status": "no_phone", "message": "..."} (no phone in row; plain text)
      - {"status": "alternatives",
         "card_text": "...",
         "keyboard_buttons": [{"label","callback_data"}, ...],
         "thread_message_id": int | None,
         "chat_id": int}
    """
    chat_id = int(session_id)
    if status != "COMPLETED":
        return {"status": "no_alternatives"}

    result = row.get("sc_result") or {}
    summary = (
        result.get("summary")
        or result.get("post_summary")
        or row.get("sc_summary")
        or row.get("sc_post_summary")
        or ""
    )
    if not summary:
        return {"status": "no_summary"}

    try:
        alternatives = await asyncio.to_thread(
            groq_client.extract_booking_alternatives, summary
        )
    except Exception as exc:
        logger.exception(
            "Groq alternatives extraction failed for run %s: %s", run_id, exc
        )
        alternatives = []
    if not alternatives:
        return {"status": "no_alternatives"}

    existing = db.get_pending_confirmation_by_original_run(run_id)
    if existing and existing["status"] == "pending":
        return {"status": "already_pending", "short_id": existing["short_id"]}

    clinic_name = row.get("clinic_name")
    phone = row.get("phone")
    if not phone:
        logger.warning(
            "Alternatives offered for run_id=%s but phone is missing from calls row; "
            "falling back to plain-text message",
            run_id,
        )
        return {
            "status": "no_phone",
            "message": (
                f"📅 The clinic couldn't do that exact slot, but offered these alternatives:\n"
                f"{', '.join(a['raw_phrase'] for a in alternatives)}"
            ),
        }

    patient_name = row.get("patient_name")
    clinic_name_s = str(clinic_name) if clinic_name is not None else ""
    patient_name_s = str(patient_name) if patient_name is not None else ""

    import uuid as _uuid

    short_id = _uuid.uuid4().hex[:8]
    db.insert_pending_confirmation(
        short_id=short_id,
        chat_id=chat_id,
        clinic_name=clinic_name_s,
        phone=phone,
        patient_name=patient_name_s,
        reason=summary,
        what=summary,
        original_call_run_id=run_id,
        alternatives_list=alternatives,
    )

    keyboard_buttons: list[list[dict]] = []
    for idx, alt in enumerate(alternatives):
        label = alt["raw_phrase"]
        if len(label) > 40:
            label = label[:37] + "..."
        keyboard_buttons.append(
            [{"label": label, "callback_data": f"alt:{short_id}:{idx}"}]
        )
    keyboard_buttons.append(
        [
            {"label": "❌ None of these", "callback_data": f"alt:{short_id}:none"},
            {"label": "🔍 Show raw details", "callback_data": f"details:{run_id}"},
        ]
    )

    masked_phone = mask_phones_in(phone) if phone else "Unknown"
    card_text = _maybe_offer_alternatives_card_text(
        alternatives,
        clinic_name=clinic_name_s,
        masked_phone=masked_phone,
        patient_name=patient_name_s,
        chat_id=chat_id,
    )

    return {
        "status": "alternatives",
        "card_text": card_text,
        "keyboard_buttons": keyboard_buttons,
        "short_id": short_id,
        "thread_message_id": row.get("status_message_id"),
        "chat_id": chat_id,
    }


async def pick_alternative(
    session_id: int,
    short_id: str,
    choice: str,
) -> dict:
    """Body of bot.on_pick_alternative, relocated.

    Returns:
      - {"status": "expired", "message": "..."}
      - {"status": "already_processed", "message": "..."}
      - {"status": "declined"}                            (user picked none)
      - {"status": "invalid_choice", "message": "..."}
      - {"status": "calle_unreachable", "message": "..."}
      - {"status": "needs_more_info", "message": "..."}
      - {"status": "launched", "thread_text": "...",
         "thread_message_id": int | None}
    """
    import uuid as _uuid

    chat_id = int(session_id)

    pending = db.get_pending_confirmation(short_id)
    if not pending:
        return {
            "status": "expired",
            "message": "❌ This alternative has expired or been processed. Please start a new request.",
        }

    if pending["status"] != "pending":
        return {
            "status": "already_processed",
            "message": "❌ This alternative has already been processed.",
        }

    if choice == "none":
        db.set_pending_status(short_id, "declined")
        return {"status": "declined"}

    try:
        index = int(choice)
        alternatives = pending["alternatives"]
        if index < 0 or index >= len(alternatives):
            raise ValueError("Index out of range")
        selected = alternatives[index]
    except (ValueError, IndexError):
        return {
            "status": "invalid_choice",
            "message": "❌ Invalid selection. Please try again.",
        }

    db.set_pending_status(short_id, "processing", chosen_index=index)

    clinic_name_stored = pending["clinic_name"] or "the clinic"
    phone_stored = pending.get("phone") or ""
    who = (
        f"{clinic_name_stored} at {phone_stored}"
        if phone_stored
        else clinic_name_stored
    )
    patient_name = pending.get("patient_name") or ""
    raw_phrase = selected["raw_phrase"]

    what_parts = [pending["reason"], raw_phrase]
    what = ", ".join(part for part in what_parts if part) or "book an appointment"

    patient_segment = f" for {patient_name}" if patient_name else ""
    thread_text = (
        f"📞 Calling {who} to book{patient_segment} at {raw_phrase}…\n\n"
        f"📝 Request: {what}"
    )

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            _compose_book_task_input(who, what, patient_name=patient_name or None),
            None,
            "English",
        )
    except CalleError as exc:
        logger.warning("second call plan_call failed: %s", exc)
        db.set_pending_status(short_id, "failed")
        return {
            "status": "calle_unreachable",
            "message": (
                f"❌ Sorry, planning the second call failed:\n{exc}\n\n"
                f"The alternative at {raw_phrase} was not booked."
            ),
        }

    plan = structured_result(plan_response)
    if not plan.get("ready_to_run"):
        db.set_pending_status(short_id, "failed")
        return {
            "status": "needs_more_info",
            "message": (
                f"❌ I need more information to book the alternative at {raw_phrase}:\n\n"
                f"{_questions_text(plan)}"
            ),
        }

    launch = await launch_call(
        chat_id,
        plan,
        clinic_name=clinic_name_stored,
        phone=phone_stored,
        patient_name=patient_name or None,
    )
    if launch.get("status") == "launched":
        db.set_pending_status(
            short_id,
            "completed",
            second_call_run_id=launch.get("run_id"),
        )
    return {
        "status": "launched",
        "thread_text": thread_text,
        "thread_message_id": launch.get("thread_message_id"),
        "run_id": launch.get("run_id"),
    }


def _questions_text(plan: dict) -> str:
    questions = plan.get("clarifying_questions") or [
        q.get("question", "") for q in plan.get("questions", []) if isinstance(q, dict)
    ]
    lines = "\n".join(f"• {q}" for q in questions if q)
    if not lines:
        return (
            "I need a bit more information before I can place this call — "
            "please add more details in one message."
        )
    return f"I still need a few details:\n{lines}\n\nPlease answer in one message."


def _compose_book_task_input(
    who: str,
    what: str,
    ask_alternatives: bool = False,
    patient_name: str | None = None,
) -> str:
    task = f"Call {who}. {what}. {SAFETY_SUFFIX}"
    if patient_name:
        task = f"Call {who} for {patient_name}. {what}. {SAFETY_SUFFIX}"
    if ask_alternatives:
        task = f"{task} {BOOKING_ALTERNATIVES_INSTRUCTION}"
    return task


async def launch_call(
    session_id: int,
    plan: dict,
    *,
    clinic_name: str = "",
    phone: str = "",
    patient_name: str | None = None,
    retry_hint: str = "Send /call to try again.",
) -> dict:
    """Body of bot._launch_ready_call, but returns the launch-text instead
    of editing a Telegram thread. Caller is responsible for sending it
    and re-passing the resulting message_id into attach_thread().

    Returns:
      {"status": "launched", "run_id": "...", "thread_text": "...",
       "thread_message_id": None}
    """
    import uuid as _uuid

    chat_id = int(session_id)
    plan_id = plan.get("plan_id")
    token = plan.get("confirm_token")
    thread_text = "📞 Calling now, I'll let you know when it's done."

    if not plan_id or not token:
        await channel_send(
            chat_id,
            "❌ The call couldn't be started — CALL-E didn't return the "
            f"confirmation data needed. {retry_hint}",
        )
        return {"status": "failed", "thread_text": thread_text}

    try:
        run_response = await asyncio.to_thread(
            calle_client.run_call, str(plan_id), str(token)
        )
    except CalleError as exc:
        logger.warning("run_call failed: %s", exc)
        await channel_send(
            chat_id,
            f"❌ The call couldn't be started: {exc}\n\n{retry_hint}",
        )
        return {"status": "failed", "thread_text": thread_text}
    except Exception as exc:
        logger.exception("unexpected run_call error")
        await channel_send(
            chat_id,
            f"❌ Unexpected error while starting your call ({type(exc).__name__})"
            f" — {retry_hint.lower()}",
        )
        return {"status": "failed", "thread_text": thread_text}

    run = structured_result(run_response)
    run_id = run.get("run_id")
    if not run_id:
        await channel_send(
            chat_id,
            "❌ The call was submitted but no tracking id came back — I can't "
            f"follow up on its result. {retry_hint}",
        )
        return {"status": "failed", "thread_text": thread_text}

    await asyncio.to_thread(db.bump_usage, chat_id, _today_key())
    await asyncio.to_thread(
        db.insert_call,
        chat_id,
        str(plan_id),
        str(run_id),
        None,
        None,
        clinic_name,
        phone,
        patient_name,
    )
    return {
        "status": "launched",
        "run_id": str(run_id),
        "thread_text": thread_text,
        "thread_message_id": None,
    }


async def attach_thread(session_id: int, run_id: str, message_id: int) -> None:
    """Persist the launching message_id on the calls row so the poller can
    edit it in place at terminal status. Replaces the bot-side
    `insert_call(..., status_message_id=thread.message_id)` call site.
    """
    chat_id = int(session_id)
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE calls SET status_message_id = ? WHERE run_id = ?",
            (message_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# /call
# ===========================================================================


# Constants used by the /call flow's confirm card and prompts.
# Kept verbatim from bot.py so the message strings do not drift.
GUIDED_WHO_PROMPT = (
    "Let's do this step by step. First, who should I call? Include the "
    "clinic or doctor name and the phone number in international format — "
    "for example:\n\n"
    "  Dr Sharma Dental Clinic, +919876543210\n\n"
    "The phone number must start with + and country code (e.g. +91…)."
)

GUIDED_WHAT_PROMPT = (
    "Got it. What should I book? Include the reason and your preferred "
    "date/time (e.g. 'annual check-up, next Tuesday morning').\n\n"
    "Send /cancel to abort."
)

MAX_CLARIFY_ROUNDS = 3  # cap on successive clarify answers before giving up


def _missing_fields_text(missing: list[str], details: dict | None = None) -> str:
    """Body of bot._missing_fields_text, verbatim."""
    lines = []
    for field in missing:
        if field == "phone" and details and details.get("phone_invalid"):
            lines.append(
                "• That phone number doesn't look valid — please send it in "
                "international format, e.g. +919876543210."
            )
        else:
            lines.append(f"• {field.replace('_', ' ')}")
    return (
        "Got most of it! I still need:\n"
        + "\n".join(lines)
        + "\n\nPlease answer in one message."
    )


def _apply_details(details: dict, *, state: dict) -> None:
    """Body of bot._apply_details — mutates `state` instead of context.user_data.
    Same key writes, same pops.
    """
    clinic = details.get("clinic_or_doctor")
    phone = details.get("phone")
    who = f"{clinic} at {phone}" if clinic and phone else (phone or clinic or "")
    pieces = [
        details.get("reason"),
        details.get("preferred_date"),
        details.get("preferred_time"),
    ]
    what = ", ".join(piece for piece in pieces if piece) or "book an appointment"
    state["who"] = who
    state["what"] = what
    state["patient_name"] = details.get("patient_name")
    for key in ("details", "first_text", "clarify_rounds"):
        state.pop(key, None)


def _call_confirm_text(who: str, what: str) -> str:
    return (
        "Please double-check before I make a real phone call:\n\n"
        f"📞 Call: {mask_phones_in(who)}\n"
        f"📝 Request: {what}"
    )


async def start_call(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.received_first. Returns a dict the bot renders.

    Returns:
      - {"status": "groq_unavailable", "guided_prompt": GUIDED_WHO_PROMPT}
      - {"status": "groq_error",        "guided_prompt": GUIDED_WHO_PROMPT}
      - {"status": "ready", "confirm": { ... payload for call_confirm_card }}
      - {"status": "needs_clarify", "missing_text": "..."}
    """
    chat_id = int(session_id)
    text = (free_text or "").strip()

    if not groq_client.is_configured():
        logger.info("GROQ_API_KEY not set — using guided flow")
        return {"status": "groq_unavailable", "guided_prompt": GUIDED_WHO_PROMPT}

    try:
        details = await asyncio.to_thread(groq_client.extract_call_details, text)
    except groq_client.GroqError as exc:
        logger.warning("groq extraction failed (%s); falling back to guided flow", exc)
        return {"status": "groq_error", "guided_prompt": GUIDED_WHO_PROMPT}

    missing = details.get("missing_fields") or []
    if not missing:
        _apply_details(details, state=state)
        return {
            "status": "ready",
            "confirm": {"who": state.get("who", ""), "what": state.get("what", "")},
        }

    state["details"] = details
    state["first_text"] = text
    return {
        "status": "needs_clarify",
        "missing_text": _missing_fields_text(missing, details),
    }


async def call_clarify(session_id: int, follow_up_text: str, *, state: dict) -> dict:
    """Body of bot.received_followup. Same return shape as start_call."""
    chat_id = int(session_id)
    answer = (follow_up_text or "").strip()
    combined = "\n".join(part for part in (state.get("first_text", ""), answer) if part)

    try:
        details = await asyncio.to_thread(groq_client.extract_call_details, combined)
    except groq_client.GroqError as exc:
        logger.warning("groq follow-up extraction failed (%s)", exc)
        state["guided_mode"] = True
        return {"status": "groq_error", "guided_prompt": GUIDED_WHO_PROMPT}

    previous = state.get("details") or {}
    merged = {
        field: details.get(field) or previous.get(field)
        for field in groq_client.DETAIL_FIELDS
    }

    if not merged.get("phone"):
        state["guided_mode"] = True
        return {
            "status": "needs_phone_guided",
            "guided_prompt": (
                "I still don't have a phone number I can dial, so let's do this "
                "step by step.\n\n" + GUIDED_WHO_PROMPT
            ),
        }

    _apply_details(merged, state=state)
    return {
        "status": "ready",
        "confirm": {"who": state.get("who", ""), "what": state.get("what", "")},
    }


async def call_collect_who(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.received_who."""
    chat_id = int(session_id)
    text = (free_text or "").strip()
    if not PHONE_RE.search(text):
        return {
            "status": "no_phone_in_text",
            "message": (
                "❌ Hmm, I couldn't find a phone number in international format "
                "(starting with + and at least 8 digits). Please include it, e.g. "
                "+919876543210 — don't worry, I'll use it exactly as you wrote it."
            ),
        }
    state["who"] = text
    return {"status": "accepted", "next_prompt": GUIDED_WHAT_PROMPT}


async def call_collect_what(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.received_what."""
    text = (free_text or "").strip()
    if not text:
        return {
            "status": "empty",
            "message": "Please describe what you'd like to book.",
        }
    state["what"] = text
    return {
        "status": "ready",
        "confirm": {"who": state.get("who", ""), "what": state.get("what", "")},
    }


async def call_confirm_card(session_id: int, *, state: dict) -> dict:
    """Body of bot._ask_confirm (minus the actual send). Returns either a
    ready-to-render card+keyboard, or a 'needs language first' shape.
    """
    chat_id = int(session_id)
    who = state.get("who", "")
    what = state.get("what", "")
    summary = _call_confirm_text(who, what)
    if state.get("call_language"):
        return {
            "status": "ready",
            "card_text": summary,
            "keyboard": [
                [
                    {"label": "✅ Yes, call now", "callback_data": "call_yes"},
                    {"label": "❌ No, cancel", "callback_data": "call_no"},
                ]
            ],
        }
    return {
        "status": "needs_language",
        "card_with_lang_prompt": summary
        + "\n\n🌐 What language should the clinic be called in?",
        "language_keyboard": [
            [{"label": "🌐 English", "callback_data": "lang_en"}],
            [{"label": "🌐 हिन्दी (Hindi)", "callback_data": "lang_hi"}],
        ],
        "pending_card": {
            "card_text": summary,
            "keyboard": [
                [
                    {"label": "✅ Yes, call now", "callback_data": "call_yes"},
                    {"label": "❌ No, cancel", "callback_data": "call_no"},
                ]
            ],
        },
    }


async def call_plan_and_launch(session_id: int, *, state: dict) -> dict:
    """Body of on_confirm's `call_yes` path (the post-confirm plan_call +
    quota gate + e164 guard + run_call + db.bump_usage + db.insert_call).

    Returns:
      - {"status": "limit_reached", "message": "..."}
      - {"status": "invalid_phone", "message": "..."}
      - {"status": "calle_unreachable", "message": "..."}
      - {"status": "needs_clarify", "questions": "...", "plan_id": "..."}
      - {"status": "launched", "run_id": "...", "thread_text": "...",
         "thread_message_id": None}

    On any failure path this function sends a Telegram message via the
    channel (mirroring bot._launch_ready_call's behavior). The bot's
    caller will see the failure as a returned status and clear state.
    """
    chat_id = int(session_id)
    who = state.get("who", "")
    what = state.get("what", "")
    patient_name = state.get("patient_name")

    quota = await check_quota(chat_id)
    if quota["status"] != "ok":
        await channel_send(chat_id, _format_reset(quota["limit"]))
        return {"status": "limit_reached", "message": _format_reset(quota["limit"])}

    if not PHONE_RE.search(f"{who} {what}"):
        await channel_send(
            chat_id,
            "❌ That phone number isn't valid — please re-enter it in "
            "international format (e.g. +919876543210). Send /call to try again.",
        )
        return {
            "status": "invalid_phone",
            "message": (
                "❌ That phone number isn't valid — please re-enter it in "
                "international format (e.g. +919876543210). Send /call to try again."
            ),
        }

    language = state.get("call_language") or "English"

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            _compose_book_task_input(
                who, what, ask_alternatives=True, patient_name=patient_name
            ),
            None,
            language,
        )
    except CalleError as exc:
        logger.warning("plan_call failed: %s", exc)
        msg = f"❌ Sorry, planning the call failed:\n{exc}\n\nSend /call to try again."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg}
    except Exception:
        logger.exception("unexpected planning error")
        msg = "❌ Unexpected error while planning your call — please send /call to try again."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    if not plan.get("ready_to_run"):
        questions = _questions_text(plan)
        await channel_send(chat_id, questions)
        return {
            "status": "needs_clarify",
            "questions": questions,
            "plan_id": state["plan_id"],
        }

    clinic_name = ""
    phone = ""
    if " at " in who:
        clinic_name, phone = who.split(" at ", 1)
    launch = await launch_call(
        chat_id,
        plan,
        clinic_name=clinic_name,
        phone=phone,
        patient_name=patient_name,
    )
    if launch.get("status") != "launched":
        # launch_call has already sent the failure via channel_send.
        return {"status": "launch_failed", "thread_text": launch.get("thread_text", "")}
    return {
        "status": "launched",
        "run_id": launch.get("run_id"),
        "thread_text": launch.get("thread_text"),
        "thread_message_id": launch.get("thread_message_id"),
    }


async def call_clarify_post_confirm(
    session_id: int, follow_up_text: str, *, state: dict
) -> dict:
    """Body of bot.received_clarify. Returns the same shape as
    call_plan_and_launch for the 'needs_clarify' and 'launched' paths.
    """
    chat_id = int(session_id)
    state["clarify_rounds"] = state.get("clarify_rounds", 0) + 1
    if state["clarify_rounds"] > MAX_CLARIFY_ROUNDS:
        msg = (
            "This is taking longer than expected — cancelling this booking. "
            "Please send /call to start over with all details in mind."
        )
        await channel_send(chat_id, msg)
        return {"status": "too_many_rounds", "message": msg}

    answer = (follow_up_text or "").strip()
    plan_id = state.get("plan_id")
    language = state.get("call_language") or "English"

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            answer,
            str(plan_id) if plan_id else None,
            language,
        )
    except CalleError as exc:
        logger.warning("clarify plan_call failed: %s", exc)
        msg = f"❌ Sorry, updating the plan failed:\n{exc}\n\nTry answering again, or send /cancel."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg, "keep_state": True}
    except Exception:
        logger.exception("unexpected clarify error")
        msg = "❌ Unexpected error while updating your plan — try answering again, or send /cancel."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg, "keep_state": True}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    if not plan.get("ready_to_run"):
        questions = _questions_text(plan)
        await channel_send(chat_id, questions)
        return {
            "status": "needs_clarify",
            "questions": questions,
            "plan_id": state["plan_id"],
        }

    who = state.get("who", "")
    clinic_name = ""
    phone = ""
    if " at " in who:
        clinic_name, phone = who.split(" at ", 1)
    launch = await launch_call(
        chat_id,
        plan,
        clinic_name=clinic_name,
        phone=phone,
        patient_name=state.get("patient_name"),
    )
    if launch.get("status") != "launched":
        return {"status": "launch_failed", "thread_text": launch.get("thread_text", "")}
    return {
        "status": "launched",
        "run_id": launch.get("run_id"),
        "thread_text": launch.get("thread_text"),
        "thread_message_id": launch.get("thread_message_id"),
    }


# ===========================================================================
# /callaround and /earliest (multi-clinic)
# ===========================================================================


def _clean_request_text(what: str, targets: list[dict]) -> str:
    """Body of bot._clean_request_text, verbatim."""
    cleaned = what or ""
    for target in targets:
        phone = re.sub(r"\s+", "", target.get("phone") or "")
        if len(phone) >= 7:
            pattern = r"\+?\s*" + r"[\s\-_.]?".join(re.escape(d) for d in phone)
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        name = (target.get("name") or "").strip()
        if len(name) >= 4 and name.lower() not in {"clinic", "clinics"}:
            cleaned = re.sub(re.escape(name), "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"^[\s,;.\-]+|[\s,;\-]+$", "", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",{2,}", ",", cleaned)
    return cleaned or what or "book an appointment"


def _ca_confirm_text(valid_targets: list[dict], what: str) -> str:
    """Body of bot._ca_confirm_text, verbatim."""
    lines = "\n".join(
        f"• {mask_phones_in((t['name'] or 'clinic') + ' at ' + t['phone'])}"
        for t in valid_targets
    )
    request = _clean_request_text(what, valid_targets)
    return (
        "Please confirm this multi-clinic booking attempt:\n\n"
        f"📞 Clinics ({len(valid_targets)}):\n{lines}\n"
        f"📝 Request: {request}\n\n"
        f"This will place up to {len(valid_targets)} real calls — every "
        "clinic gets its own result reported here."
    )


def valid_ca_targets(details: dict) -> list[dict]:
    """Body of bot._valid_ca_targets."""
    return [
        t for t in details.get("targets", []) if t.get("phone") and not t.get("invalid")
    ]


def compose_what(details: dict) -> str:
    """Body of bot._compose_what."""
    pieces = [
        details.get("reason"),
        details.get("preferred_date"),
        details.get("preferred_time"),
    ]
    joined = ", ".join(piece for piece in pieces if piece)
    return joined or "book an appointment"


def _compose_task_input(mode: str, name: str, phone: str, what: str) -> str:
    """Body of bot._compose_task_input (the multi-clinic variant)."""
    if mode == "earliest":
        return (
            f"Ask {name} at {phone} when their earliest available appointment "
            f"slot is for {what}. Do not book — just report the earliest "
            f"available date and time back. {SAFETY_SUFFIX}"
        )
    return f"Call {name} at {phone}. {what}. {SAFETY_SUFFIX}"


async def start_multi_clinic(
    session_id: int, free_text: str, *, state: dict, mode: str
) -> dict:
    """Body of bot.ca_received / bot.early_received. Returns a dict the bot
    renders. `mode` is either "book" (callaround) or "earliest".

    Returns:
      - {"status": "groq_unavailable", "message": "..."}
      - {"status": "groq_error", "guided_prompt": "..."} (for guided target entry)
      - {"status": "needs_clarify", "problems": ["..."], "first_text": "...",
         "details": {...}}
      - {"status": "ready_for_preflight", "details": {...}, "targets": [...],
         "what": "...", "dropped_message": "..."|None}
    """
    chat_id = int(session_id)
    text = (free_text or "").strip()

    if not groq_client.is_configured():
        msg_kind = "callaround" if mode == "book" else "earliest"
        return {
            "status": "groq_unavailable",
            "message": (
                f"❌ /{msg_kind} needs my smart parser (GROQ_API_KEY), which is "
                "unavailable right now. Please use /call instead — sorry!"
            ),
        }

    extract_fn = (
        groq_client.extract_call_around
        if mode == "book"
        else groq_client.extract_earliest_details
    )
    try:
        details = await asyncio.to_thread(extract_fn, text)
    except groq_client.GroqError as exc:
        logger.warning("groq extraction failed for %s (%s)", mode, exc)
        return {
            "status": "groq_error",
            "guided_prompt": (
                "❌ I couldn't process that automatically, so let's build it step "
                "by step.\n\nSend clinic 1 as: name + number (e.g. Vision Care "
                "+919876543210). Send 'done' after the last one."
            ),
        }

    valid = valid_ca_targets(details)
    invalid_names = details.get("invalid_names") or []

    problems = []
    if len(valid) < 2:
        verb = "try them together" if mode == "book" else "compare them"
        problems.append(
            f"❌ I need at least 2 clinics with valid international-format "
            f"numbers to {verb}."
        )
    if invalid_names:
        problems.append(
            "❌ These numbers don't look valid — please resend them: "
            + ", ".join(invalid_names)
        )

    if not problems:
        truncated = valid[:MAX_CA_TARGETS]
        dropped_message = None
        if len(valid) > MAX_CA_TARGETS:
            extra = valid[MAX_CA_TARGETS:]
            dropped = ", ".join(t["name"] or "clinic" for t in extra)
            dropped_message = (
                f"⚠️ More than {MAX_CA_TARGETS} clinics listed — keeping the "
                f"first {MAX_CA_TARGETS} and dropping: {dropped}."
            )
        return {
            "status": "ready_for_preflight",
            "details": details,
            "targets": truncated,
            "what": compose_what(details),
            "dropped_message": dropped_message,
        }

    return {
        "status": "needs_clarify",
        "problems": problems,
        "details": details,
        "first_text": text,
    }


async def multi_clinic_clarify(
    session_id: int, follow_up_text: str, *, state: dict, mode: str
) -> dict:
    """Body of bot.ca_followup / bot.early_followup.

    Returns:
      - {"status": "groq_error", "message": "..."}
      - {"status": "needs_clarify", "problems": ["..."], "first_text": "...",
         "details": {...}}
      - {"status": "ready_for_preflight", "details": {...}, "targets": [...],
         "what": "...", "ack": "Got it."} or with "ack_note" suffix
    """
    chat_id = int(session_id)
    answer = (follow_up_text or "").strip()
    combined = "\n".join(part for part in (state.get("first_text", ""), answer) if part)

    extract_fn = (
        groq_client.extract_call_around
        if mode == "book"
        else groq_client.extract_earliest_details
    )
    try:
        details = await asyncio.to_thread(extract_fn, combined)
    except groq_client.GroqError as exc:
        logger.warning("groq follow-up failed for %s (%s)", mode, exc)
        return {
            "status": "groq_error",
            "message": (
                "❌ I couldn't process that automatically. Please try again with "
                f"/{('callaround' if mode == 'book' else 'earliest')}."
            ),
        }

    valid = valid_ca_targets(details)
    invalid_names = details.get("invalid_names") or []

    if len(valid) < 2:
        verb = "try them together" if mode == "book" else "compare them"
        return {
            "status": "needs_clarify",
            "problems": [
                f"❌ Still short — I need at least 2 clinics with valid "
                f"international-format numbers to {verb}. Please resend them, "
                f"or use /call for a single clinic."
            ],
            "details": details,
            "first_text": combined,
        }

    truncated = valid[:MAX_CA_TARGETS]
    ack = "Got it."
    if invalid_names:
        ack = "Got it.\n❌ Dropped unparseable entries: " + ", ".join(invalid_names)
    return {
        "status": "ready_for_preflight",
        "details": details,
        "targets": truncated,
        "what": compose_what(details),
        "ack": ack,
    }


async def multi_collect_target(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.ca_targets_collect / bot.early_targets_collect.

    Returns:
      - {"status": "need_more", "added": False, "message": "..."} (no phone in text)
      - {"status": "max_reached", "message": "..."} (5 already collected)
      - {"status": "added", "next_prompt": "..."} (clinic appended)
      - {"status": "ready_for_what", "next_prompt": "..."} (user said "done")
    """
    chat_id = int(session_id)
    answer = (free_text or "").strip()

    if answer.lower() in {"done", "finish"}:
        targets = state.get("targets", [])
        if len(targets) < 2:
            return {
                "status": "need_more",
                "message": (
                    f"Only {len(targets)} clinic(s) so far — I need at least "
                    f"2. Send another clinic, or /cancel to abort."
                ),
            }
        return {
            "status": "ready_for_what",
            "next_prompt": (
                "📝 Now, what should I book, and for when? (e.g. 'dental cleaning "
                "next Tuesday 10am')"
                if state.get("mode") == "book"
                else "📝 Now, what should I check availability for? (e.g. 'dental "
                "cleaning next week')"
            ),
        }

    match = re.search(r"\+[1-9]\d{6,14}", answer)
    name = re.sub(r"\+[1-9]\d{6,14}", "", answer).strip(" ,;-") or "Clinic"
    if not match:
        return {
            "status": "need_more",
            "message": (
                "❌ That number doesn't look valid — please include it in "
                "international format (e.g. +919876543210)."
            ),
        }
    targets = state.setdefault("targets", [])
    if len(targets) >= MAX_CA_TARGETS:
        return {
            "status": "max_reached",
            "message": f"⚠️ Maximum is {MAX_CA_TARGETS} clinics — send 'done' to continue.",
        }
    targets.append({"name": name, "phone": match.group()})
    idx = len(targets)
    return {
        "status": "added",
        "next_prompt": f"✅ Added {name}. Send clinic {idx + 1}, or 'done' to continue.",
    }


async def multi_collect_what(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.ca_what_collect / bot.early_what_collect."""
    chat_id = int(session_id)
    what = (free_text or "").strip()
    if not what:
        return {
            "status": "empty",
            "message": "Please describe the booking."
            if state.get("mode") == "book"
            else "Please describe the request.",
        }
    state["what"] = what
    return {"status": "ready_for_preflight"}


async def multi_preflight(session_id: int, *, state: dict) -> dict:
    """Body of bot._preflight_and_ask (minus the actual send). Returns:
    - {"status": "calle_unreachable", "message": "..."}
    - {"status": "needs_clarify", "questions": "..."}  (CA_CLARIFY or EARLY_CLARIFY)
    - {"status": "ready_to_confirm"}   (the bot then calls _ask_*_confirm)
    """
    chat_id = int(session_id)
    what = state.get("what", "")
    language = state.get("call_language") or "English"

    await channel_send(chat_id, "🔍 Checking your request with CALL-E…")
    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            f"I will call one of several clinics about this request: {what}. "
            f"{SAFETY_SUFFIX}",
            None,
            language,
        )
    except CalleError as exc:
        logger.warning("multi preflight plan_call failed: %s", exc)
        msg = (
            "❌ Couldn't reach CALL-E to validate this request — please try "
            f"/callaround again in a moment.\n\n({exc})"
        )
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    if plan.get("ready_to_run"):
        return {"status": "ready_to_confirm", "plan": plan}

    state.setdefault("sched_clarify_rounds", 0)
    questions = _questions_text(plan)
    await channel_send(chat_id, questions)
    return {"status": "needs_clarify", "questions": questions}


async def multi_clarify_post_preflight(
    session_id: int, follow_up_text: str, *, state: dict
) -> dict:
    """Body of bot.ca_clarify / bot.early_clarify."""
    chat_id = int(session_id)
    state["sched_clarify_rounds"] = state.get("sched_clarify_rounds", 0) + 1
    if state["sched_clarify_rounds"] > MAX_CLARIFY_ROUNDS:
        mode = state.get("mode")
        verb = "/callaround" if mode == "book" else "/earliest"
        msg = f"This is taking longer than expected — cancelling. Please send {verb} again."
        await channel_send(chat_id, msg)
        return {"status": "too_many_rounds", "message": msg}

    answer = (follow_up_text or "").strip()
    plan_id = state.get("plan_id")
    language = state.get("call_language") or "English"

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            answer,
            str(plan_id) if plan_id else None,
            language,
        )
    except CalleError as exc:
        logger.warning("multi clarify plan_call failed (%s)", exc)
        msg = f"❌ CALL-E couldn't process that answer ({exc}). Please try again."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg, "keep_state": True}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    prev = state.get("what_extra")
    state["what_extra"] = f"{prev}; {answer}" if prev else answer

    if plan.get("ready_to_run"):
        extra = state.pop("what_extra", None)
        if extra:
            state["what"] = f"{state['what']} ({extra})"
        return {"status": "ready_to_confirm", "plan": plan}

    questions = _questions_text(plan)
    await channel_send(chat_id, questions)
    return {"status": "needs_clarify", "questions": questions}


async def multi_confirm_card(session_id: int, *, state: dict) -> dict:
    """Body of bot._ask_ca_confirm / bot._ask_early_confirm (minus the actual send).

    Returns:
      - {"status": "limit_reached", "message": "..."}
      - {"status": "needs_language", "card_with_lang_prompt": "...",
         "language_keyboard": [...], "pending_card": {...}}
      - {"status": "ready", "card_text": "...", "keyboard": [...]}
      - {"status": "partial", "card_text": "...", "keyboard": [...],
         "offered": N, "total": M}
    """
    chat_id = int(session_id)
    targets = state.get("targets", [])
    card = _ca_confirm_text(targets, state.get("what", ""))
    n = len(targets)
    limit = get_max_calls_per_day()
    used = db.get_usage(chat_id, _today_key())
    remaining = max(0, limit - used)

    if remaining <= 0:
        return {"status": "limit_reached", "message": _format_reset(limit)}

    offered = min(n, remaining)
    mode = state.get("mode", "book")
    if offered < n:
        plural = "s" if offered != 1 else ""
        if mode == "earliest":
            text = (
                card + f"\n\n⚠️ You have only {remaining} call{plural} left today — "
                f"enough to try the first {offered}:"
            )
            keyboard = [
                [
                    {
                        "label": f"▶️ Try first {offered} clinic{plural}",
                        "callback_data": "early_reduce",
                    },
                    {"label": "❌ Cancel", "callback_data": "early_no"},
                ]
            ]
        else:
            text = (
                card + f"\n\n⚠️ You have only {remaining} call{plural} left today — "
                f"enough to try the first {offered}:"
            )
            keyboard = [
                [
                    {
                        "label": f"▶️ Try first {offered} clinic{plural}",
                        "callback_data": "ca_reduce",
                    },
                    {"label": "❌ Cancel", "callback_data": "ca_no"},
                ]
            ]
        payload = {
            "status": "partial",
            "card_text": text,
            "keyboard": keyboard,
            "offered": offered,
            "total": n,
        }
    else:
        text = card
        if mode == "earliest":
            keyboard = [
                [
                    {"label": "✅ Yes, check all", "callback_data": "early_yes"},
                    {"label": "❌ No, cancel", "callback_data": "early_no"},
                ]
            ]
        else:
            keyboard = [
                [
                    {"label": "✅ Yes, call all", "callback_data": "ca_yes"},
                    {"label": "❌ No, cancel", "callback_data": "ca_no"},
                ]
            ]
        payload = {"status": "ready", "card_text": text, "keyboard": keyboard}

    if state.get("call_language"):
        return payload
    return {
        "status": "needs_language",
        "card_with_lang_prompt": payload["card_text"]
        + "\n\n🌐 What language should the clinics be called in?",
        "language_keyboard": [
            [{"label": "🌐 English", "callback_data": "lang_en"}],
            [{"label": "🌐 हिन्दी (Hindi)", "callback_data": "lang_hi"}],
        ],
        "pending_card": payload,
    }


async def launch_multi_call(
    session_id: int,
    batch_id: str,
    targets: list[dict],
    what: str,
    *,
    language: str = "English",
    mode: str = "book",
) -> dict:
    """Body of bot._launch_multi_call + bot._ca_single_call, lifted.

    Fires off plan_call + run_call + db.bump_usage + db.insert_call for each
    target in parallel. The bot doesn't need to do anything after this
    (the poller picks up the new rows and edits each thread at terminal).

    Returns:
      {"status": "started", "batch_id": "...",
       "per_clinic": [{"name": "...", "run_id": "...", "status": "launched|failed"}]}
    """
    chat_id = int(session_id)
    results = []
    tasks = [
        asyncio.create_task(
            _multi_single(chat_id, batch_id, target, what, language, mode)
        )
        for target in targets
    ]
    results = await asyncio.gather(*tasks)
    return {
        "status": "started",
        "batch_id": batch_id,
        "per_clinic": results,
    }


async def _multi_single(
    chat_id: int,
    batch_id: str,
    target: dict,
    what: str,
    language: str,
    mode: str,
) -> dict:
    """Body of bot._ca_single_call, lifted. The poller edits the per-clinic
    '📞 Calling X at Y now…' thread at terminal — we just create the row
    and let the poller handle the rest.
    """
    name = target.get("name") or "Clinic"
    phone = target.get("phone") or ""

    # Send the per-clinic thread (the poller will edit it in place at terminal).
    thread = await channel_send(
        chat_id,
        f"📞 Calling {mask_phones_in(name + ' at ' + str(phone))} now…",
    )
    thread_msg_id = getattr(thread, "message_id", None) if thread else None

    def _record_failed(plan_id: str | None = None) -> None:
        try:
            row_id = db.insert_call(
                chat_id,
                plan_id,
                None,  # type: ignore[arg-type]  # run_id — None is accepted by
                # SQLite at runtime (the column is not NOT NULL) even though
                # the type annotation says str. The pre-W1 _ca_single_call
                # passed None here.
                thread_msg_id,
                batch_id=batch_id,
                clinic_name=name,
                phone=phone,
            )
            db.set_call_status_by_rowid(row_id, "failed")
        except Exception:
            logger.exception(
                "multi-call %s: failed to record failed row for %s", batch_id, name
            )

    async def _fail_thread(reason: str) -> None:
        try:
            await channel_send(chat_id, f"❌ {name}: {reason}")
        except Exception:
            logger.exception(
                "multi-call %s: failed to send failure message for %s", batch_id, name
            )

    task_input = _compose_task_input(mode, name, phone, what)
    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call, task_input, None, language
        )
    except CalleError as exc:
        logger.warning("multi-call %s plan_call failed for %s: %s", batch_id, name, exc)
        _record_failed()
        await _fail_thread(f"the planning request failed ({exc}).")
        return {
            "name": name,
            "status": "failed",
            "reason": "plan_failed",
            "error": str(exc),
        }

    plan = structured_result(plan_response)
    if not plan.get("ready_to_run"):
        questions = _questions_text(plan)
        _record_failed(plan.get("plan_id"))
        await _fail_thread(f"CALL-E needs more information:\n{questions}")
        return {"name": name, "status": "failed", "reason": "needs_clarify"}

    await asyncio.to_thread(db.bump_usage, chat_id, _today_key())

    token = plan.get("confirm_token")
    try:
        run_response = await asyncio.to_thread(
            calle_client.run_call, str(plan["plan_id"]), str(token)
        )
    except Exception as exc:
        logger.warning("multi-call %s run_call failed for %s: %s", batch_id, name, exc)
        _record_failed(str(plan["plan_id"]))
        await _fail_thread(f"the call couldn't be started ({exc}).")
        return {
            "name": name,
            "status": "failed",
            "reason": "run_failed",
            "error": str(exc),
        }

    run = structured_result(run_response)
    run_id = run.get("run_id")
    if not run_id:
        _record_failed(str(plan["plan_id"]))
        await _fail_thread("the call was submitted but no tracking id came back.")
        return {"name": name, "status": "failed", "reason": "no_run_id"}

    await asyncio.to_thread(
        db.insert_call,
        chat_id,
        str(plan["plan_id"]),
        str(run_id),
        thread_msg_id,
        batch_id=batch_id,
        clinic_name=name,
        phone=phone,
    )
    return {"name": name, "status": "launched", "run_id": str(run_id)}


# ===========================================================================
# /schedule
# ===========================================================================


# Constants for the schedule pre-confirm card. Kept verbatim from bot.py.
SCHED_KEYBOARD_TEXT_CONFIRM = (
    "Please confirm this SCHEDULED call:\n\n"
    "📞 Call: {who}\n"
    "📝 Request: {what}\n"
    "⏰ Will be placed: {stamp}"
)


_TIME_TOKEN_RE = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\bnoon\b|\bmidnight\b",
    re.IGNORECASE,
)


def parse_when(raw: str):
    """Body of bot._parse_when. Returns (resolved_datetime, error_message).
    On success: (datetime, None). On failure: (None, "...").

    `SCHEDULE_MIN_LEAD_S` is the user-facing lead time — users must give us
    at least this much headroom between "now" and the target dial time.
    Decoupled from APScheduler's own `misfire_grace_time` (300s in bot.py),
    which handles a different concern: a job that fires late. The user-side
    lead time here exists so a user who types "10:02 pm today" at 10:01:50
    gets a clear "too close" message instead of a "in the past" message
    that mis-explains what's actually happening.
    """
    SCHEDULE_MIN_LEAD_S = 60

    raw = (raw or "").strip()
    logger.info("parsing schedule time: raw=%r", raw)
    if not raw:
        return (
            None,
            "I couldn't find a date or time in your message — when should I "
            "place this call?",
        )
    if not _TIME_TOKEN_RE.search(raw):
        return (
            None,
            f'"{raw}" has no exact time of day — please include one '
            '(e.g. "2pm" or "10:30").',
        )
    normalized = re.sub(r"\b(?:next|this|coming)\s+", "", raw, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\b(1[3-9]|2[0-3]):(\d{2})\s*(am|pm)\b",
        r"\1:\2",
        normalized,
        flags=re.IGNORECASE,
    )
    parsed = dateparser.parse(
        normalized,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if parsed is None:
        logger.info("schedule time unparseable: raw=%r normalized=%r", raw, normalized)
        return (
            None,
            f'I couldn\'t understand that time ("{raw}") — please use a '
            'format like "4:45pm" or "14:45".',
        )
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    now = datetime.now(parsed.tzinfo)
    delta_s = (parsed - now).total_seconds()
    rejected = delta_s < SCHEDULE_MIN_LEAD_S
    logger.info(
        "parse_when: raw=%r parsed=%s now=%s delta_s=%.3f rejected=%s",
        raw,
        parsed.isoformat(),
        now.isoformat(),
        delta_s,
        rejected,
    )
    if rejected:
        return None, (
            f"That time is too close — please pick a time at least a minute "
            f"from now (you picked {parsed.strftime('%H:%M')}, it's now "
            f"{now.strftime('%H:%M')})."
        )
    return parsed, None


def _sched_problems(details: dict) -> list[str]:
    """Body of bot._sched_problems."""
    problems = []
    missing = [
        field for field in (details.get("missing_fields") or []) if field != "call_at"
    ]
    if missing:
        problems.append(_missing_fields_text(missing, details))
    if "call_at" in (details.get("missing_fields") or []):
        problems.append(
            "⏰ " + "What exact date and time should I PLACE the call? "
            '(e.g. "today 4:45pm", "tomorrow 9am")'
        )
    return problems


def _compose_sched_confirm(state: dict) -> str:
    """Body of bot._compose_sched_confirm."""
    resolved = state["resolved_at"]
    stamp = resolved.strftime("%a %d %b %Y at %H:%M")
    return mask_phones_in(
        SCHED_KEYBOARD_TEXT_CONFIRM.format(
            who=state.get("who", ""),
            what=state.get("what", ""),
            stamp=stamp,
        )
    )


def _stamp_for(resolved_at) -> str:
    return resolved_at.strftime("%a %d %b %Y at %H:%M")


async def start_schedule(session_id: int, free_text: str, *, state: dict) -> dict:
    """Body of bot.sched_received. Returns:
    - {"status": "groq_unavailable", "message": "..."}
    - {"status": "groq_error", "message": "..."}
    - {"status": "ready_for_preflight", "details": {...}}
      (state.who/what/patient_name/resolved_at populated)
    - {"status": "needs_clarify", "problems": ["..."], "details": {...},
       "first_text": "...", "resolved_at": datetime|None}
    """
    chat_id = int(session_id)
    text = (free_text or "").strip()

    if not groq_client.is_configured():
        return {
            "status": "groq_unavailable",
            "message": (
                "Scheduling needs my smart parser right now, but it's unavailable "
                "(GROQ_API_KEY not set). Please use /call instead — sorry!"
            ),
        }

    try:
        details = await asyncio.to_thread(groq_client.extract_call_details, text, True)
    except groq_client.GroqError as exc:
        logger.warning("groq extraction failed for schedule (%s)", exc)
        return {
            "status": "groq_error",
            "message": (
                "❌ I couldn't process that automatically. Please try again in a "
                "minute with /schedule."
            ),
        }

    resolved, when_error = parse_when(details.get("call_at"))
    problems = _sched_problems(details)
    if when_error:
        problems.append(when_error)

    if not problems:
        _apply_details(details, state=state)
        state["resolved_at"] = resolved
        return {"status": "ready_for_preflight"}

    state["details"] = details
    state["first_text"] = text
    if resolved is not None:
        state["resolved_at"] = resolved
    return {
        "status": "needs_clarify",
        "problems": problems,
        "details": details,
        "first_text": text,
        "resolved_at": resolved,
    }


async def schedule_clarify(
    session_id: int, follow_up_text: str, *, state: dict
) -> dict:
    """Body of bot.sched_followup. Returns:
    - {"status": "groq_error", "message": "..."}
    - {"status": "no_phone", "message": "..."}
    - {"status": "needs_clarify", "problems": [...], "details": {...},
       "first_text": "..."}
    - {"status": "ready_for_preflight"}
    """
    chat_id = int(session_id)
    answer = (follow_up_text or "").strip()
    combined = "\n".join(part for part in (state.get("first_text", ""), answer) if part)

    try:
        details = await asyncio.to_thread(
            groq_client.extract_call_details, combined, True
        )
    except groq_client.GroqError as exc:
        logger.warning("groq follow-up extraction failed for schedule (%s)", exc)
        return {
            "status": "groq_error",
            "message": (
                "❌ I couldn't process that automatically. Please try again with /schedule."
            ),
        }

    previous = state.get("details") or {}
    merged = {
        field: details.get(field) or previous.get(field)
        for field in groq_client.SCHED_FIELDS
    }
    merged["missing_fields"] = [
        field for field in groq_client.SCHED_FIELDS if not merged[field]
    ]

    resolved, when_error = parse_when(merged.get("call_at"))

    if not merged.get("phone"):
        return {
            "status": "no_phone",
            "message": (
                "I still don't have a phone number I can dial. Please send "
                "/schedule again including it (e.g. +919876543210)."
            ),
            "details": merged,
            "first_text": combined,
        }

    if when_error or "call_at" in (merged.get("missing_fields") or []):
        return {
            "status": "needs_clarify",
            "problems": [
                when_error
                or (
                    "⏰ " + "What exact date and time should I PLACE the call? "
                    '(e.g. "today 4:45pm", "tomorrow 9am")'
                )
            ],
            "details": merged,
            "first_text": combined,
            "resolved_at": resolved,
        }

    _apply_details(merged, state=state)
    state["resolved_at"] = resolved
    return {"status": "ready_for_preflight"}


async def schedule_preflight(session_id: int, *, state: dict) -> dict:
    """Body of bot._preflight_schedule_plan (minus the actual send).

    Returns:
      - {"status": "limit_reached", "message": "..."}
      - {"status": "invalid_phone", "message": "..."}
      - {"status": "calle_unreachable", "message": "..."}
      - {"status": "ready_to_confirm"}    (plan ready_to_run)
      - {"status": "needs_clarify", "questions": "..."}  (plan not ready)
    """
    chat_id = int(session_id)
    who = state.get("who", "")
    what = state.get("what", "")
    language = state.get("call_language") or "English"

    quota = await check_quota(chat_id)
    if quota["status"] != "ok":
        await channel_send(chat_id, _format_reset(quota["limit"]))
        return {"status": "limit_reached", "message": _format_reset(quota["limit"])}

    if not PHONE_RE.search(f"{who} {what}"):
        msg = (
            "❌ That phone number isn't valid — please re-enter it in "
            "international format (e.g. +919876543210). Send /schedule to try again."
        )
        await channel_send(chat_id, msg)
        return {"status": "invalid_phone", "message": msg}

    await channel_send(chat_id, "🔍 Checking your request with CALL-E…")
    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            _compose_book_task_input(who, what, patient_name=state.get("patient_name")),
            None,
            language,
        )
    except CalleError as exc:
        logger.warning("schedule pre-flight plan_call failed: %s", exc)
        msg = (
            "❌ Couldn't reach CALL-E to plan this call — please try /schedule "
            f"again in a moment.\n\n({exc})"
        )
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    # Handle daily mode: if we're in daily mode and have time but no date from CALL-E
    if state.get("mode") == "daily" and state.get("resolved_at") is None:
        # Extract time from state (set during preprocessing)
        time_str = state.get("daily_time_str")
        if not time_str:
            return {
                "status": "invalid_input",
                "message": "❌ Internal error: missing time for daily schedule",
            }

        # Parse time formats like "9am", "9:30am", "14:30"
        now = datetime.now().astimezone()
        parsed_time = None
        for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
            try:
                parsed_time = datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue

        if parsed_time is None:
            return {
                "status": "invalid_time",
                "message": f"❌ Couldn't parse time '{time_str}'. Use formats like '9am', '9:30am', '14:30'.",
            }

        # Create datetime for today at parsed time
        today_at_time = now.replace(
            hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0
        )

        # If time has passed today, schedule for tomorrow
        if today_at_time <= now:
            resolved_at = today_at_time + timedelta(days=1)
        else:
            resolved_at = today_at_time

        state["resolved_at"] = resolved_at
        # Store time-of-day for cron trigger
        state["daily_time"] = parsed_time

    if plan.get("ready_to_run"):
        return {"status": "ready_to_confirm", "plan": plan}

    state.setdefault("sched_clarify_rounds", 0)
    questions = _questions_text(plan)
    await channel_send(chat_id, questions)
    return {"status": "needs_clarify", "questions": questions}


async def schedule_clarify_post_preflight(
    session_id: int, follow_up_text: str, *, state: dict
) -> dict:
    """Body of bot.sched_plan_clarify. Same return shape as
    schedule_preflight.
    """
    chat_id = int(session_id)
    state["sched_clarify_rounds"] = state.get("sched_clarify_rounds", 0) + 1
    if state["sched_clarify_rounds"] > MAX_CLARIFY_ROUNDS:
        msg = (
            "This is taking longer than expected — cancelling this schedule "
            "request. Please send /schedule again with all the details."
        )
        await channel_send(chat_id, msg)
        return {"status": "too_many_rounds", "message": msg}

    answer = (follow_up_text or "").strip()
    plan_id = state.get("plan_id")
    language = state.get("call_language") or "English"

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            answer,
            str(plan_id) if plan_id else None,
            language,
        )
    except CalleError as exc:
        logger.warning("schedule clarify plan_call failed (%s)", exc)
        msg = f"❌ CALL-E couldn't process that answer ({exc}). Please try answering again."
        await channel_send(chat_id, msg)
        return {"status": "calle_unreachable", "message": msg, "keep_state": True}

    plan = structured_result(plan_response)
    state["plan_id"] = plan.get("plan_id")

    prev = state.get("what_extra")
    state["what_extra"] = f"{prev}; {answer}" if prev else answer

    if plan.get("ready_to_run"):
        extra = state.pop("what_extra", None)
        if extra:
            state["what"] = f"{state['what']} ({extra})"
        return {"status": "ready_to_confirm", "plan": plan}

    questions = _questions_text(plan)
    await channel_send(chat_id, questions)
    return {"status": "needs_clarify", "questions": questions}


async def schedule_confirm_card(session_id: int, *, state: dict) -> dict:
    """Body of bot._ask_sched_confirm (minus the actual send).

    Returns:
      - {"status": "needs_time", "message": "⏰ ..."}  (no resolved_at)
      - {"status": "needs_language", "card_with_lang_prompt": "...",
         "language_keyboard": [...], "pending_card": {...}}
      - {"status": "ready", "card_text": "..."}
    """
    chat_id = int(session_id)
    resolved = state.get("resolved_at")
    if resolved is None:
        logger.warning("confirm reached with unresolved call time — re-asking")
        return {
            "status": "needs_time",
            "message": "⏰ " + "What exact date and time should I PLACE the call? "
            '(e.g. "today 4:45pm", "tomorrow 9am")',
        }

    # Check if this is daily mode
    if state.get("mode") == "daily" and state.get("daily_time"):
        time_str = state["daily_time"].strftime("%I:%M %p").lstrip("0")
        card = (
            f"✅ Please confirm this daily recurring call:\n\n"
            f"📞 Who: {state.get('who')}\n"
            f"📝 What: {state.get('what')}\n"
            f"👤 Patient: {state.get('patient_name') or '(not specified)'}\n"
            f"🕐 Time: Every day at {time_str}\n"
            f"🌐 Language: {state.get('call_language') or 'English'}\n\n"
            f"This will place a call every day at the specified time.\n"
            f"Use /cancel <id> to stop the series."
        )
    else:
        # Existing one-time card logic
        card = _compose_sched_confirm(state)

    if state.get("call_language"):
        return {"status": "ready", "card_text": card}
    return {
        "status": "needs_language",
        "card_with_lang_prompt": card
        + "\n\n🌐 What language should the clinic be called in?",
        "language_keyboard": [
            [{"label": "🌐 English", "callback_data": "lang_en"}],
            [{"label": "🌐 हिन्दी (Hindi)", "callback_data": "lang_hi"}],
        ],
        "pending_card": {"card_text": card},
    }


async def schedule_job(
    session_id: int,
    *,
    state: dict,
    add_job_fn: Callable[..., Any],
    remove_job_fn: Callable[[str], None],
) -> dict:
    """Body of on_sched_confirm's `sched_yes` path (APScheduler add_job +
    db.insert_scheduled). Keeps the job store and the DB in sync: if the
    DB insert fails after the APScheduler job was added, the orphaned
    scheduler job is removed before returning the failure status.

    `add_job_fn` and `remove_job_fn` are callables the caller injects (same
    injection pattern as `add_job_fn`). The core function never touches
    APScheduler or the global scheduler directly — that responsibility
    lives in the bot layer.

    Returns:
      - {"status": "internal_state_error", "message": "..."}
      - {"status": "add_job_failed", "message": "..."}
      - {"status": "insert_failed", "message": "..."}
      - {"status": "scheduled", "short_id": "...", "stamp": "...",
         "completion_message": "⏰ Scheduled! ..."}
    """
    import uuid as _uuid

    chat_id = int(session_id)
    run_at = state.get("resolved_at")
    who = state.get("who", "")
    what = state.get("what", "")
    patient_name = state.get("patient_name") or None
    if run_at is None or not who:
        return {
            "status": "internal_state_error",
            "message": "❌ Internal state error — please use /schedule again.",
        }

    # 16 hex chars = 64 bits of uuid4 entropy. Plenty for collision safety;
    # `short_id = job_id[:8]` (8 chars shown to the user) is unchanged.
    job_id = _uuid.uuid4().hex[:16]
    language = state.get("call_language") or "English"

    # 1) Add the APScheduler job first. If this raises, nothing was written
    #    to either store, so no cleanup is needed.
    try:
        # Prepare recurrence parameters for daily jobs
        recurrence_param = "daily" if state.get("mode") == "daily" else None
        recurrence_time_param = None
        if state.get("mode") == "daily" and state.get("daily_time"):
            recurrence_time_param = state["daily_time"].strftime("%H:%M")

        add_job_fn(
            job_id=job_id,
            chat_id=chat_id,
            who=who,
            what=what,
            language=language,
            patient_name=patient_name,
            recurrence=recurrence_param,
            recurrence_time=recurrence_time_param,
        )
    except Exception as exc:
        logger.exception("add_job failed for %s", job_id)
        return {
            "status": "add_job_failed",
            "message": (
                f"⚠️ Couldn't create the scheduled job: {type(exc).__name__}: {exc}\n"
                "Please try /schedule again."
            ),
        }

    # 2) Persist the row. If this raises, remove the APScheduler job we
    #    just added so we never leave a phantom job pointing at nothing.
    #    Cleanup is best-effort: a failure in remove_job_fn is logged but
    #    does not mask the original insert failure from the caller.
    try:
        await asyncio.to_thread(
            db.insert_scheduled,
            chat_id,
            job_id,
            run_at.isoformat(),
            who,
            what,
            language,
            patient_name,
            state.get("recurrence"),  # NEW: 'daily' or None
            state.get("recurrence_time"),  # NEW: time string like "09:00" or None
        )
    except Exception as exc:
        logger.exception("insert_scheduled failed for %s", job_id)
        try:
            remove_job_fn(job_id)
        except Exception as cleanup_exc:
            logger.warning("remove_job cleanup failed for %s: %s", job_id, cleanup_exc)
        return {
            "status": "insert_failed",
            "message": (
                f"❌ Couldn't record the schedule: {type(exc).__name__}: {exc}\n"
                "Please try /schedule again."
            ),
        }

    stamp = _stamp_for(run_at)
    return {
        "status": "scheduled",
        "short_id": job_id[:8],
        "stamp": stamp,
        "completion_message": (
            f"⏰ Scheduled! I'll place the call on {stamp}.\n\n"
            f"Short id: {job_id[:8]}\nUse /mycalls to list pending calls, or "
            f"/cancel {job_id[:8]} to cancel."
        ),
    }


async def fire_scheduled_call(
    job_id: str,
    chat_id: int,
    who: str,
    what: str,
    language: str,
    patient_name: str | None = None,
    recurrence: str | None = None,  # NEW: 'daily' for recurring jobs
    recurrence_time: str | None = None,  # NEW: time-of-day like "09:00"
) -> None:
    """Body of bot.scheduled_call_job, moved verbatim (minus the `_application`
    reference — the caller passes `bot` and the channel is registered before
    scheduling). Decision logic only; rendering is handled via channel_send.
    """
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).strftime("%a %d %b")

    # Quota handling - different behavior for daily vs one-time
    if db.get_usage(chat_id, _today_key()) >= get_max_calls_per_day():
        if recurrence == "daily":
            # For daily jobs: skip today but continue the series
            await channel_send(
                chat_id,
                f"ℹ️ Daily call limit reached ({get_max_calls_per_day()} calls) — "
                f"today's recurring call was skipped. "
                f"Count resets at midnight tonight ({tomorrow}).",
            )
            return  # IMPORTANT: Return normally so schedule continues
        else:
            # For one-time jobs: mark as failed
            db.set_scheduled_status(job_id, "failed")
            await channel_send(
                chat_id,
                f"❌ Daily limit of {get_max_calls_per_day()} calls already reached — "
                f"this scheduled call was NOT placed. Count resets at midnight "
                f"tonight ({tomorrow}).",
            )
            return
            return

    base = await channel_send(chat_id, "⏰ Your scheduled call is starting now…")

    try:
        plan_response = await asyncio.to_thread(
            calle_client.plan_call,
            _compose_book_task_input(who, what, patient_name=patient_name),
            None,
            language or "English",
        )
    except CalleError as exc:
        logger.warning("scheduled plan_call failed for %s: %s", job_id, exc)
        db.set_scheduled_status(job_id, "failed")
        await channel_send(
            chat_id,
            f"❌ Your scheduled call couldn't be started:\n{exc}\n\nPlease use "
            "/call to retry manually.",
        )
        return

    plan = structured_result(plan_response)
    if not plan.get("ready_to_run"):
        db.set_scheduled_status(job_id, "failed")
        questions = _questions_text(plan)
        await channel_send(
            chat_id,
            "❌ I tried to place your scheduled call, but CALL-E needs more "
            f"information before it can dial:\n\n{questions}\n\nPlease use "
            "/call to book interactively.",
        )
        return

    db.set_scheduled_status(job_id, "fired")
    clinic_name, phone = who.split(" at ", 1) if who and " at " in who else ("", "")
    # Send the launch thread so the user sees the same "📞 Calling now…" line
    # the pre-W1 bot edited into the "Your scheduled call is starting now…"
    # message via _edit_or_send. We send a new message (preserves the visible
    # text; the message_id is the launch thread the poller will edit at
    # terminal).
    await channel_send(chat_id, "📞 Calling now, I'll let you know when it's done.")
    await launch_call(
        chat_id,
        plan,
        clinic_name=clinic_name,
        phone=phone,
        retry_hint="Please use /call.",
        patient_name=patient_name,
    )


# Re-export calle_client so callers can keep importing core.X (and the
# scheduled-call fire path can keep the same import surface).
from app import calle_client  # noqa: E402

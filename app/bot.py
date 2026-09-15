import asyncio
import html
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

import dateparser
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app import calle_client, core, db, groq_client
from app.calle_client import TERMINAL_STATUSES, CalleError, is_terminal
from app.config import get_max_calls_per_day, get_telegram_token
from app.groq_client import GroqError
from app.masking import mask_phones_in

logger = logging.getLogger(__name__)

WHO, WHAT, CONFIRM, CLARIFY, FIRST, FOLLOWUP = range(6)

(
    SCHED_FIRST,
    SCHED_FOLLOWUP,
    SCHED_CONFIRM,
    SCHED_PLAN,
    DAILY_FIRST,
    DAILY_FOLLOWUP,
    DAILY_CONFIRM,
) = range(6, 13)

CA_FIRST, CA_FOLLOWUP, CA_TARGETS, CA_WHAT, CA_CONFIRM, CA_CLARIFY = range(10, 16)

LANG_CALL, LANG_SCHED, LANG_CA = range(16, 19)

EARLY_FIRST, EARLY_FOLLOWUP, EARLY_TARGETS, EARLY_WHAT, EARLY_CONFIRM, EARLY_CLARIFY = (
    range(19, 25)
)
LANG_EARLY = 25

CHAIN_FIRST, CHAIN_FOLLOWUP, CHAIN_CONFIRM = range(26, 29)
LANG_CHAIN = 29

LANGUAGE_LABELS = {"lang_en": "English", "lang_hi": "Hindi"}

LANGUAGE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🌐 English", callback_data="lang_en")],
        [InlineKeyboardButton("🌐 हिन्दी (Hindi)", callback_data="lang_hi")],
    ]
)

MAX_CA_TARGETS = 5
MIN_CA_TARGETS = 2

CA_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Yes, call all", callback_data="ca_yes"),
            InlineKeyboardButton("❌ No, cancel", callback_data="ca_no"),
        ]
    ]
)

CA_REDUCE_KEYBOARD_TEMPLATE = "▶️ Try first {offered} clinic{plural}"

MAX_CLARIFY_ROUNDS = 5
POLL_ACTIVE_SECONDS = 3
POLL_IDLE_SECONDS = 10
MAX_ROW_ERRORS = 3
# Safety net: any call row that has been 'running' for longer than this
# without reaching a terminal status almost certainly means we missed a
# terminal string (new CALL-E status, or a path that never wrote the
# row). Force-report as POLL_ERROR after this bound rather than polling
# silently forever.
MAX_POLL_DURATION_SECONDS = 300  # 5 minutes
MAX_RESULT_CHARS = 3000
SEND_RETRIES = 3
SEND_RETRY_DELAY = 2

PHONE_RE = re.compile(r"\+[1-9]\d{6,14}")
E164_TOKEN_RE = re.compile(r"\+[1-9]\d{6,14}")

WELCOME_TEXT = (
    "Hi! I'm ATC, your personal phone assistant.\n\n"
    "Tell me which doctor or clinic you need and I'll place a real phone "
    "call on your behalf to book an appointment — then report back here "
    "with the confirmation.\n\n"
    "Type /call to book an appointment, or /help to see everything I can do."
)

HELP_TEXT = (
    "Here's what I can do:\n\n"
    "/start — introduction and how booking works\n"
    "/help — show this message\n"
    '/call — book an appointment in one message (e.g. "Book a cleaning at '
    "Dr Sharma Dental, +919876543210, Tuesday 10am\") or step by step — I'll "
    "always confirm before placing a real phone call. If the clinic can't book "
    "the exact slot, I'll show you their alternatives and place a second call "
    "to confirm the one you pick.\n"
    "/callaround — try several clinics in parallel for the same request: "
    "list each clinic with its number plus what you want booked; every "
    "clinic's result gets reported separately\n"
    "/earliest — like /callaround, but checks the earliest available slot "
    "at each clinic (no booking). When all clinics respond, I send a "
    "summary sorted soonest-first so you can pick the best one\n"
    "/chain — sequential fallback booking: list an ordered series of "
    "clinics and the next one is only called if the previous one "
    "couldn't book (e.g. 'Call Dr Sharma about a cleaning tomorrow; if no "
    "slot this week, call Vision Care instead; if neither works, just "
    "tell me')\n"
    "/schedule — like /call, but for later: tell me who, what and exactly "
    "when, and I'll place the call at that time\n"
    "/mycalls — list your pending scheduled calls\n"
    "/cancel <id> — cancel a scheduled call by its short id (or /cancel to "
    "see pending ones)\n\n"
    "After you confirm, I place the call via CALL-E and message you here "
    "with the result."
)

GUIDED_WHO_PROMPT = (
    "Who should I call? Send the clinic or person's name plus their phone "
    "number in international format (e.g. +919876543210).\n\n"
    "Send /cancel to abort."
)

CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Yes, call", callback_data="call_yes"),
            InlineKeyboardButton("❌ No, cancel", callback_data="call_no"),
        ]
    ]
)

SCHED_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Yes, schedule", callback_data="sched_yes"),
            InlineKeyboardButton("❌ No, cancel", callback_data="sched_no"),
        ]
    ]
)


async def _safe_send(bot, chat_id: int, text: str, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramError as exc:
            last_exc = exc
            logger.warning(
                "send to %s failed (%d/%d): %s", chat_id, attempt, SEND_RETRIES, exc
            )
            await asyncio.sleep(SEND_RETRY_DELAY * attempt)
    logger.error("giving up sending to %s: %s", chat_id, last_exc)
    return None


def _build_telegram_channel(bot, chat_id: int) -> core.Channel:
    """Build a Channel that sends Telegram messages on behalf of core.
    Long-running operations in core (chain executor, scheduled-call fire,
    poller) use this so they don't depend on PTB-specific code paths.
    """

    async def _send(text: str, **kwargs):
        return await _safe_send(bot, chat_id, text, **kwargs)

    async def _edit(message_id, text: str, **kwargs):
        if message_id is None:
            return await _safe_send(bot, chat_id, text, **kwargs)
        try:
            return await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, **kwargs
            )
        except TelegramError as exc:
            logger.warning("edit failed (%s); sending new message instead", exc)
            return await _safe_send(bot, chat_id, text, **kwargs)

    async def _edit_or_send(message, text: str, **kwargs):
        if message is not None and getattr(message, "message_id", None):
            return await _edit(message.message_id, text, **kwargs)
        return await _safe_send(bot, chat_id, text, **kwargs)

    return core.Channel(send_text=_send, edit_text=_edit, edit_or_send=_edit_or_send)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await _safe_send(context.bot, update.effective_chat.id, WELCOME_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await _safe_send(context.bot, update.effective_chat.id, HELP_TEXT)


async def call_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Tell me in one message who to call and what to book — for example: "
            '"Book a dental cleaning at Dr Sharma Dental Clinic, +919876543210, '
            'next Tuesday at 10am".\n\n'
            "I'll fill in any gaps, show you a summary, and confirm before I "
            "place the real call.\n\n"
            "Send /cancel to abort.",
        )
    return FIRST


# ---- /call handlers below are thin renderers; decision logic lives in
#      app.core. Bot-side helpers still used: mask_phones_in from app.masking
#      (PTB-presenter layer for chat cards; core emits masked strings itself,
#      the bot's local shorthand), _get_flow_language (PTB-only), and the
#      ConversationHandler state constants at the top of the file. All
#      decision logic (Groq extraction, CALL-E plan/run/clarify, quota
#      gates, E.164 validation, etc.) is now owned by app.core.


def _ca_keyboard_to_inline(buttons: list[list[dict]]) -> InlineKeyboardMarkup:
    """Translate a core-shaped keyboard (list of list of {label, callback_data}
    dicts) into a PTB InlineKeyboardMarkup. Used by the thin wrappers below
    for the /call, /schedule, /callaround, /earliest confirm cards.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(b["label"], callback_data=b["callback_data"])
                for b in row
            ]
            for row in buttons
        ]
    )


def _pending_confirm_to_inline(pending: dict) -> InlineKeyboardMarkup:
    """Same as _ca_keyboard_to_inline but accepts a {text, keyboard, state}
    dict (the shape the on_language_selected path persists into
    user_data["pending_confirm"]). Falls back to a simple yes/no when the
    keyboard slot is missing.
    """
    keyboard = pending.get("keyboard")
    if keyboard is not None and not isinstance(keyboard, InlineKeyboardMarkup):
        return _ca_keyboard_to_inline(keyboard)
    return keyboard or InlineKeyboardMarkup([])


def _language_prompt_keyboard() -> InlineKeyboardMarkup:
    return LANGUAGE_KEYBOARD


async def _ask_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Thin wrapper around core.call_confirm_card. Renders the confirm card
    (or the language-prompt variant) and returns the next ConversationHandler
    state. All decisions (text content, keyboard labels, quota gates) live
    in core; this function only does the PTB translation.
    """
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    payload = await core.call_confirm_card(chat_id, state=context.user_data)
    if payload["status"] == "ready":
        await _safe_send(
            context.bot,
            chat_id,
            payload["card_text"],
            reply_markup=_ca_keyboard_to_inline(payload["keyboard"]),
        )
        return CONFIRM
    # status == "needs_language"
    pending = payload["pending_card"]
    context.user_data["pending_confirm"] = {
        "text": pending["card_text"],
        "keyboard": _ca_keyboard_to_inline(pending["keyboard"]),
        "state": CONFIRM,
    }
    await _safe_send(
        context.bot,
        chat_id,
        payload["card_with_lang_prompt"],
        reply_markup=LANGUAGE_KEYBOARD,
    )
    return LANG_CALL


def _get_flow_language(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("call_language") or "English"


async def on_language_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)

    language = LANGUAGE_LABELS.get(query.data or "", "English")
    context.user_data["call_language"] = language

    pending = context.user_data.pop("pending_confirm", None)
    if not pending:
        return ConversationHandler.END

    try:
        await query.edit_message_text(pending["text"], reply_markup=pending["keyboard"])
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    return pending["state"]


async def _edit_or_send(bot, message, chat_id: int, text: str, **kwargs):
    if message is not None:
        try:
            return await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message.message_id, **kwargs
            )
        except TelegramError as exc:
            logger.warning("edit failed (%s); sending new message instead", exc)
    return await _safe_send(bot, chat_id, text, **kwargs)


SAFETY_SUFFIX = (
    "Only check availability, book or change the appointment, and report "
    "back logistics. Do not ask for or give medical advice."
)


BOOKING_ALTERNATIVES_INSTRUCTION = (
    "If the requested date or time isn't available, ask the clinic what "
    "alternative dates and times they have, tell them we will call back "
    "once we have confirmed with the patient, and do NOT book anything on "
    "this call."
)


async def received_first(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.start_call(chat_id, text, state=context.user_data)
    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, chat_id, result["guided_prompt"])
        return WHO
    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, chat_id, result["missing_text"])
        return FOLLOWUP
    # status == "ready" → ask for confirm
    return await _ask_confirm(chat_id, context)


async def received_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    result = await core.call_clarify(chat_id, answer, state=context.user_data)
    if result["status"] in ("groq_error", "needs_phone_guided"):
        await _safe_send(context.bot, chat_id, result["guided_prompt"])
        return WHO
    # status == "ready"
    return await _ask_confirm(chat_id, context)


async def received_who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.call_collect_who(chat_id, text, state=context.user_data)
    if result["status"] == "no_phone_in_text":
        await _safe_send(context.bot, chat_id, result["message"])
        return WHO
    await _safe_send(context.bot, chat_id, result["next_prompt"])
    return WHAT


async def received_what(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.call_collect_what(chat_id, text, state=context.user_data)
    if result["status"] == "empty":
        await _safe_send(context.bot, chat_id, result["message"])
        return WHAT
    return await _ask_confirm(chat_id, context)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id

    if query.data == "call_no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n❌ Cancelled — no call was placed."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            (query.message.text or "") + "\n\n🔍 Planning your call…"
        )
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)

    # Register the channel so failure messages from core.launch_call reach Telegram.
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))

    result = await core.call_plan_and_launch(chat_id, state=context.user_data)
    if result["status"] == "needs_clarify":
        return CLARIFY
    if result["status"] in (
        "limit_reached",
        "invalid_phone",
        "calle_unreachable",
        "launch_failed",
    ):
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "launched":
        # Send the launch thread and attach its message_id to the calls row.
        launch_msg = await _safe_send(context.bot, chat_id, result["thread_text"])
        if launch_msg and result.get("run_id"):
            await core.attach_thread(chat_id, result["run_id"], launch_msg.message_id)
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.clear()
    return ConversationHandler.END


async def received_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.call_clarify_post_confirm(
        chat_id, answer, state=context.user_data
    )
    if result["status"] == "too_many_rounds":
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "needs_clarify":
        return CLARIFY
    if result["status"] == "calle_unreachable":
        # keep state so user can retry
        return CLARIFY
    if result["status"] == "launched":
        launch_msg = await _safe_send(context.bot, chat_id, result["thread_text"])
        if launch_msg and result.get("run_id"):
            await core.attach_thread(chat_id, result["run_id"], launch_msg.message_id)
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "launch_failed":
        context.user_data.clear()
        return ConversationHandler.END
    return CLARIFY


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("booking_active"):
        context.user_data.clear()
        if update.effective_chat:
            await _safe_send(
                context.bot,
                update.effective_chat.id,
                "Cancelled — no call was placed. Send /call whenever you're ready.",
            )
        return ConversationHandler.END

    if not update.effective_chat:
        return None
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        rows = db.list_scheduled_for_chat(chat_id)
        if not rows:
            await _safe_send(
                context.bot, chat_id, "You have no pending scheduled calls."
            )
            return None
        lines = [
            f"[{r['job_id'][:8]}] {r['run_at']} — {mask_phones_in(r['who'])}"
            for r in rows
        ]
        await _safe_send(
            context.bot,
            chat_id,
            "Which one? Use /cancel <id>:\n\n" + "\n".join(lines),
        )
        return None

    short = args[0].strip()
    row = db.find_scheduled_by_prefix(chat_id, short)
    if row is None:
        matches = [
            r
            for r in db.list_scheduled_for_chat(chat_id)
            if r["job_id"].startswith(short)
        ]
        if len(matches) > 1:
            await _safe_send(
                context.bot,
                chat_id,
                f"'{short}' matches more than one call — use a longer prefix "
                "or check /mycalls.",
            )
            return None
        await _safe_send(
            context.bot,
            chat_id,
            f"No pending scheduled call matching '{short}'. Check /mycalls.",
        )
        return None

    job_id = row["job_id"]
    removed = True
    if _scheduler is not None:
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            removed = False
    db.set_scheduled_status(job_id, "cancelled")
    note = (
        ""
        if removed
        else "\n\n(The scheduler no longer had that job — likely already handled.)"
    )
    await _safe_send(
        context.bot,
        chat_id,
        f"🗑 Cancelled [{job_id[:8]}] — the call scheduled for {row['run_at']} "
        f"will NOT happen.{note}",
    )
    return None


_scheduler = None
_application = None

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


def _parse_when(raw: str):
    """Backwards-compat shim. Body lives in core.parse_when; bot.py keeps
    the import-time reference for any callers that still bind the name.
    New code should call core.parse_when directly.
    """
    from app import core as _core  # local import to avoid a circular at module load

    return _core.parse_when(raw)


async def schedule_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Let's schedule a call for later. In ONE message give me all "
            'three parts — for example: "Call Dr Sharma Dental at '
            "+919876543210 at 4:45pm today, to book a cleaning for next "
            'Tuesday at 10am".\n\n'
            "(In that example 4:45pm today is when I dial, and next Tuesday "
            "10am is the appointment slot I'll request.)\n\n"
            "Send /cancel to abort.",
        )
    return SCHED_FIRST


async def daily_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    context.user_data["mode"] = "daily"  # Mark as daily mode

    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Let's schedule a daily recurring call. In ONE message give me:\n"
            "1. Who to call (clinic name and phone)\n"
            "2. What to book (purpose)\n"
            "3. What time each day (e.g. '9am', '14:30')\n\n"
            'Example: "Call Dr Sharma Dental at +919876543210, for a cleaning, at 9am"\n\n'
            "The call will be placed every day at the specified time.\n"
            "Send /cancel to abort.",
        )
    return DAILY_FIRST


async def daily_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["daily_raw"] = text

    # Parse the input using core's schedule parsing logic
    result = await core.start_schedule(
        update.effective_chat.id, text, state=context.user_data
    )

    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return ConversationHandler.END

    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return DAILY_FOLLOWUP

    # Move to confirmation
    return await _ask_daily_confirm(update.effective_chat.id, context)


async def daily_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip()
    context.user_data["daily_answer"] = answer

    # Re-run schedule parsing with combined input
    combined = f"{context.user_data.get('daily_raw', '')} {answer}".strip()
    result = await core.start_schedule(
        update.effective_chat.id, combined, state=context.user_data
    )

    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return ConversationHandler.END

    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return DAILY_FOLLOWUP

    return await _ask_daily_confirm(update.effective_chat.id, context)


async def _ask_daily_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    payload = await core.schedule_confirm_card(chat_id, state=context.user_data)

    if payload["status"] == "needs_time":
        await _safe_send(context.bot, chat_id, payload["message"])
        return DAILY_FOLLOWUP
    if payload["status"] == "needs_language":
        await _safe_send(
            context.bot,
            chat_id,
            payload["card_with_lang_prompt"],
            reply_markup=InlineKeyboardMarkup(payload["language_keyboard"]),
        )
        return DAILY_CONFIRM
    if payload["status"] == "ready":
        await _safe_send(
            context.bot,
            chat_id,
            payload["pending_card"]["card_text"],
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, schedule daily", callback_data="daily_yes"
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="daily_no")],
                ]
            ),
        )
        return DAILY_CONFIRM

    await _safe_send(
        context.bot, chat_id, "❌ Something went wrong. Please use /daily again."
    )
    return ConversationHandler.END


async def _ask_sched_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Thin wrapper around core.schedule_confirm_card. The decision body
    (text, keyboard, language gate) lives in core; this only does the
    PTB translation.
    """
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    payload = await core.schedule_confirm_card(chat_id, state=context.user_data)
    if payload["status"] == "needs_time":
        await _safe_send(context.bot, chat_id, payload["message"])
        return SCHED_FOLLOWUP
    if payload["status"] == "ready":
        await _safe_send(
            context.bot, chat_id, payload["card_text"], reply_markup=SCHED_KEYBOARD
        )
        return SCHED_CONFIRM
    # status == "needs_language"
    context.user_data["pending_confirm"] = {
        "text": payload["pending_card"]["card_text"],
        "keyboard": SCHED_KEYBOARD,
        "state": SCHED_CONFIRM,
    }
    await _safe_send(
        context.bot,
        chat_id,
        payload["card_with_lang_prompt"],
        reply_markup=LANGUAGE_KEYBOARD,
    )
    return LANG_SCHED


async def _run_sched_preflight(chat_id: int, context) -> int:
    """Thin wrapper around core.schedule_preflight; maps the core status to
    the post-preflight ConversationHandler state.
    """
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.schedule_preflight(chat_id, state=context.user_data)
    if result["status"] == "ready_to_confirm":
        return await _ask_sched_confirm(chat_id, context)
    if result["status"] == "needs_clarify":
        return SCHED_PLAN
    # limit_reached / invalid_phone / calle_unreachable: core has already
    # sent the user-facing message; just end the flow.
    context.user_data.clear()
    return ConversationHandler.END


async def sched_plan_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Thin wrapper: a follow-up answer in the post-preflight clarify loop
    goes to core.schedule_clarify_post_preflight, which sends any
    user-facing messages itself and returns a state hint.
    """
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.schedule_clarify_post_preflight(
        chat_id, answer, state=context.user_data
    )
    if result["status"] == "too_many_rounds":
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "ready_to_confirm":
        return await _ask_sched_confirm(chat_id, context)
    # needs_clarify, calle_unreachable — stay in the plan state
    return SCHED_PLAN


# ---- /schedule thin handlers below; decision logic in app.core. ----


async def sched_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.start_schedule(chat_id, text, state=context.user_data)
    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "ready_for_preflight":
        return await _run_sched_preflight(chat_id, context)
    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return SCHED_FOLLOWUP
    return SCHED_FOLLOWUP


async def sched_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.schedule_clarify(chat_id, answer, state=context.user_data)
    if result["status"] in ("groq_error", "no_phone"):
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return SCHED_FOLLOWUP
    # status == "ready_for_preflight"
    return await _run_sched_preflight(chat_id, context)


async def on_sched_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id

    if query.data == "sched_no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n❌ Cancelled — nothing was scheduled."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return ConversationHandler.END

    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))

    def _add_job(
        *,
        job_id,
        chat_id,
        who,
        what,
        language,
        patient_name=None,
        recurrence=None,
        recurrence_time=None,
    ):
        if recurrence == "daily" and recurrence_time:
            # Parse the time string like "HH:MM" into hour and minute for the cron trigger.
            hour, minute = map(int, recurrence_time.split(":"))
            trigger = CronTrigger(hour=hour, minute=minute)
        else:
            # One-time job: use the resolved_at from context.
            trigger = DateTrigger(run_date=context.user_data["resolved_at"])
        _scheduler.add_job(
            scheduled_call_job,
            trigger=trigger,
            args=[job_id, chat_id, who, what, language, patient_name],
            id=job_id,
            name=f"scheduled call {chat_id}",
            replace_existing=True,
            misfire_grace_time=300,
        )

    def _remove_job(job_id: str) -> None:
        """Remove a scheduled job. Mirrors the pattern in _remove_scheduled_job
        (the /cancel path) so the auto-cleanup on db.insert_scheduled failure
        uses the same removal path as an explicit user /cancel.
        """
        if _scheduler is None:
            return
        try:
            _scheduler.remove_job(job_id)
        except Exception as exc:
            # Bubble up so core.schedule_job can log + continue cleanly.
            raise exc

    result = await core.schedule_job(
        chat_id,
        state=context.user_data,
        add_job_fn=_add_job,
        remove_job_fn=_remove_job,
    )
    if result["status"] == "internal_state_error":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] in ("add_job_failed", "insert_failed"):
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    # status == "scheduled"
    try:
        await query.edit_message_text((query.message.text or "") + "\n\n✅ Scheduled.")
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    await _safe_send(context.bot, chat_id, result["completion_message"])
    context.user_data.clear()
    return ConversationHandler.END


async def scheduled_call_job(
    job_id: str,
    chat_id: int,
    who: str,
    what: str,
    language: str = "English",
    patient_name: str | None = None,
) -> None:
    """APScheduler entry point. The decision body lives in
    app.core.fire_scheduled_call; this wrapper just provides the bot
    reference and registers the channel.
    """
    bot = _application.bot if _application else None
    if bot is None:
        logger.error("scheduled job %s fired but application ref is missing", job_id)
        return
    logger.info("scheduled job %s firing for chat %s", job_id, chat_id)
    core.register_channel(chat_id, _build_telegram_channel(bot, chat_id))

    # Get the scheduled call details from the database to ensure we have the
    # most up-to-date information (including recurrence info for daily jobs).
    job_details = db.get_scheduled_by_job_id(chat_id, job_id)
    if job_details is None:
        logger.error(
            "scheduled job %s for chat %s not found in database", job_id, chat_id
        )
        return

    # Use the details from the database (overrides the passed-in parameters).
    who = job_details["who"]
    what = job_details["what"]
    language = job_details["language"] or "English"
    patient_name = job_details["patient_name"]
    recurrence = job_details["recurrence"]  # Fixed missing closing bracket and quote
    recurrence_time = job_details["recurrence_time"]

    await core.fire_scheduled_call(
        job_id,
        chat_id,
        who,
        what,
        language,
        patient_name,
        recurrence=recurrence,
        recurrence_time=recurrence_time,
    )


async def my_calls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    rows = db.list_scheduled_for_chat(chat_id)
    if not rows:
        await _safe_send(context.bot, chat_id, "You have no pending scheduled calls.")
        return
    lines = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["run_at"]).strftime(
                "%a %d %b %Y at %H:%M"
            )
        except ValueError:
            stamp = row["run_at"]
        if row.get("recurrence") == "daily":
            emoji = "🔁"
            label = "Daily"
        else:
            emoji = "⏰"
            label = ""
        lines.append(
            f"[{row['job_id'][:8]}] {emoji} {stamp} {label}\n   {mask_phones_in(row['who'])} — {row['what'][:60]}"
        )
    await _safe_send(
        context.bot,
        chat_id,
        "Pending scheduled calls:\n\n" + "\n\n".join(lines),
    )


def _clean_request_text(what: str, targets: list[dict]) -> str:
    """Backwards-compat shim. Body lives in core._clean_request_text."""
    from app import core as _core

    return _core._clean_request_text(what, targets)


def _ca_confirm_text(valid_targets: list[dict], what: str) -> str:
    """Backwards-compat shim. Body lives in core._ca_confirm_text."""
    from app import core as _core

    return _core._ca_confirm_text(valid_targets, what)


def _compose_what(details: dict) -> str:
    """Backwards-compat shim. Body lives in core.compose_what."""
    from app import core as _core

    return _core.compose_what(details)


def _valid_ca_targets(details: dict) -> list[dict]:
    """Backwards-compat shim. Body lives in core.valid_ca_targets."""
    from app import core as _core

    return _core.valid_ca_targets(details)


async def ca_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Try several clinics at once! In one message list each clinic "
            "with its phone number, plus what you want booked and when — for "
            'example: "Try Dr Sharma Dental +919876543210 and Vision Care '
            '+919876543211 for a dental cleaning next Tuesday at 10am".\n\n'
            f"I'll call up to {MAX_CA_TARGETS} of them in parallel and report "
            "each clinic's result separately.\n\nSend /cancel to abort.",
        )
    return CA_FIRST


# ---- /callaround and /earliest thin handlers below; decision logic in app.core. ----


async def ca_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.start_multi_clinic(
        chat_id, text, state=context.user_data, mode="book"
    )
    if result["status"] == "groq_unavailable":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "groq_error":
        context.user_data["guided_mode"] = True
        context.user_data["targets"] = []
        await _safe_send(context.bot, chat_id, result["guided_prompt"])
        return CA_TARGETS
    if result["status"] == "needs_clarify":
        context.user_data["details"] = result["details"]
        context.user_data["first_text"] = result["first_text"]
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return CA_FOLLOWUP
    # ready_for_preflight
    context.user_data["ca_details"] = result["details"]
    context.user_data["targets"] = result["targets"]
    context.user_data["what"] = result["what"]
    if result.get("dropped_message"):
        await _safe_send(context.bot, chat_id, result["dropped_message"])
    return await _dispatch_multi_preflight(chat_id, context)


async def ca_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.multi_clinic_clarify(
        chat_id, answer, state=context.user_data, mode="book"
    )
    if result["status"] == "groq_error":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "needs_clarify":
        context.user_data["details"] = result["details"]
        context.user_data["first_text"] = result["first_text"]
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return CA_FOLLOWUP
    # ready_for_preflight
    context.user_data["ca_details"] = result["details"]
    context.user_data["targets"] = result["targets"]
    context.user_data["what"] = result["what"]
    await _safe_send(context.bot, chat_id, result["ack"])
    return await _dispatch_multi_preflight(chat_id, context)


async def ca_targets_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.multi_collect_target(chat_id, text, state=context.user_data)
    if result["status"] in ("need_more", "max_reached"):
        await _safe_send(context.bot, chat_id, result["message"])
        return CA_TARGETS
    if result["status"] == "added":
        await _safe_send(context.bot, chat_id, result["next_prompt"])
        return CA_TARGETS
    # ready_for_what
    await _safe_send(context.bot, chat_id, result["next_prompt"])
    return CA_WHAT


async def ca_what_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.multi_collect_what(chat_id, text, state=context.user_data)
    if result["status"] == "empty":
        await _safe_send(context.bot, chat_id, result["message"])
        return CA_WHAT
    return await _dispatch_multi_preflight(chat_id, context)


async def ca_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.multi_clarify_post_preflight(
        chat_id, answer, state=context.user_data
    )
    if result["status"] == "too_many_rounds":
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "calle_unreachable":
        return CA_CLARIFY
    if result["status"] == "needs_clarify":
        return CA_CLARIFY
    # ready_to_confirm
    return await _dispatch_multi_confirm(chat_id, context)


async def on_ca_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id

    if query.data == "ca_no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n❌ Cancelled — nothing was placed."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return ConversationHandler.END

    targets = context.user_data.get("targets", [])
    what = context.user_data["what"]
    n = len(targets)
    quota = await core.check_quota(chat_id)
    if quota["status"] != "ok":
        context.user_data.clear()
        await _safe_send(context.bot, chat_id, core._format_reset(quota["limit"]))
        return ConversationHandler.END
    # Cap by remaining quota. core.launch_multi_call does the same check
    # defensively, but doing it here keeps the user's quota accurate.
    remaining = quota["limit"] - quota.get("used", 0)
    n = min(n, remaining)
    batch_id = uuid.uuid4().hex[:12]
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    language = _get_flow_language(context)
    asyncio.create_task(
        core.launch_multi_call(
            chat_id, batch_id, targets[:n], what, language=language, mode="book"
        )
    )

    try:
        await query.edit_message_text(
            (query.message.text or "") + "\n\n📞 Calling all listed clinics now…"
        )
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    context.user_data.clear()
    return ConversationHandler.END


async def _dispatch_multi_preflight(chat_id: int, context) -> int:
    """Shared helper for ca_received / ca_followup / ca_what_collect."""
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    preflight = await core.multi_preflight(chat_id, state=context.user_data)
    if preflight["status"] == "calle_unreachable":
        context.user_data.clear()
        return ConversationHandler.END
    if preflight["status"] == "needs_clarify":
        return (
            EARLY_CLARIFY if context.user_data.get("mode") == "earliest" else CA_CLARIFY
        )
    # ready_to_confirm
    return await _dispatch_multi_confirm(chat_id, context)


async def _dispatch_multi_confirm(chat_id: int, context) -> int:
    """Shared helper for the post-preflight ready branch."""
    return await _ask_ca_or_early_confirm(chat_id, context)


async def _ask_ca_or_early_confirm(chat_id: int, context) -> int:
    """Body of bot._ask_ca_confirm / bot._ask_early_confirm via core.
    Returns the post-language ConversationHandler state.
    """
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    payload = await core.multi_confirm_card(chat_id, state=context.user_data)
    if payload["status"] == "limit_reached":
        context.user_data.clear()
        await _safe_send(context.bot, chat_id, payload["message"])
        return ConversationHandler.END
    if payload["status"] == "needs_language":
        context.user_data["pending_confirm"] = {
            "text": payload["pending_card"]["card_text"],
            "keyboard": _ca_keyboard_to_inline(
                payload["pending_card"].get("keyboard", [])
            ),
            "state": EARLY_CONFIRM
            if context.user_data.get("mode") == "earliest"
            else CA_CONFIRM,
        }
        await _safe_send(
            context.bot,
            chat_id,
            payload["card_with_lang_prompt"],
            reply_markup=LANGUAGE_KEYBOARD,
        )
        return LANG_EARLY if context.user_data.get("mode") == "earliest" else LANG_CA
    # status == "ready" or "partial"
    text = payload["card_text"]
    keyboard = _ca_keyboard_to_inline(payload["keyboard"])
    await _safe_send(context.bot, chat_id, text, reply_markup=keyboard)
    return EARLY_CONFIRM if context.user_data.get("mode") == "earliest" else CA_CONFIRM


async def early_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    context.user_data["mode"] = "earliest"
    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Find the earliest opening across several clinics! In one message "
            "list each clinic with its phone number, plus what you want booked "
            '— for example: "Dr Sharma Dental +919876543210 and Vision Care '
            '+919876543211 for a dental cleaning".\n\n'
            f"I'll call up to {MAX_CA_TARGETS} of them in parallel — each "
            "clinic gets its own result, and once they're all in I'll send a "
            "summary sorted by earliest available slot.\n\n"
            "I won't book anything — just check availability.\n\n"
            "Send /cancel to abort.",
        )
    return EARLY_FIRST


async def early_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    context.user_data.setdefault("mode", "earliest")
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.start_multi_clinic(
        chat_id, text, state=context.user_data, mode="earliest"
    )
    if result["status"] == "groq_unavailable":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "groq_error":
        context.user_data["guided_mode"] = True
        context.user_data["targets"] = []
        context.user_data["mode"] = "earliest"
        await _safe_send(context.bot, chat_id, result["guided_prompt"])
        return EARLY_TARGETS
    if result["status"] == "needs_clarify":
        context.user_data["details"] = result["details"]
        context.user_data["first_text"] = result["first_text"]
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return EARLY_FOLLOWUP
    # ready_for_preflight
    context.user_data["ea_details"] = result["details"]
    context.user_data["targets"] = result["targets"]
    context.user_data["what"] = result["what"]
    if result.get("dropped_message"):
        await _safe_send(context.bot, chat_id, result["dropped_message"])
    return await _dispatch_multi_preflight(chat_id, context)


async def early_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    context.user_data.setdefault("mode", "earliest")
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.multi_clinic_clarify(
        chat_id, answer, state=context.user_data, mode="earliest"
    )
    if result["status"] == "groq_error":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "needs_clarify":
        context.user_data["details"] = result["details"]
        context.user_data["first_text"] = result["first_text"]
        await _safe_send(context.bot, chat_id, "\n\n".join(result["problems"]))
        return EARLY_FOLLOWUP
    # ready_for_preflight
    context.user_data["ea_details"] = result["details"]
    context.user_data["targets"] = result["targets"]
    context.user_data["what"] = result["what"]
    await _safe_send(context.bot, chat_id, result["ack"])
    return await _dispatch_multi_preflight(chat_id, context)


async def early_targets_collect(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.multi_collect_target(chat_id, text, state=context.user_data)
    if result["status"] in ("need_more", "max_reached"):
        await _safe_send(context.bot, chat_id, result["message"])
        return EARLY_TARGETS
    if result["status"] == "added":
        await _safe_send(context.bot, chat_id, result["next_prompt"])
        return EARLY_TARGETS
    # ready_for_what
    await _safe_send(context.bot, chat_id, result["next_prompt"])
    return EARLY_WHAT


async def early_what_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.multi_collect_what(chat_id, text, state=context.user_data)
    if result["status"] == "empty":
        await _safe_send(context.bot, chat_id, result["message"])
        return EARLY_WHAT
    return await _dispatch_multi_preflight(chat_id, context)


async def early_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    result = await core.multi_clarify_post_preflight(
        chat_id, answer, state=context.user_data
    )
    if result["status"] == "too_many_rounds":
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "calle_unreachable":
        return EARLY_CLARIFY
    if result["status"] == "needs_clarify":
        return EARLY_CLARIFY
    # ready_to_confirm
    return await _dispatch_multi_confirm(chat_id, context)


async def on_early_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id

    if query.data == "early_no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n❌ Cancelled — nothing was placed."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return ConversationHandler.END

    targets = context.user_data.get("targets", [])
    what = context.user_data.get("what", "")
    n = len(targets)
    quota = await core.check_quota(chat_id)
    if quota["status"] != "ok":
        context.user_data.clear()
        await _safe_send(context.bot, chat_id, core._format_reset(quota["limit"]))
        return ConversationHandler.END
    n = min(n, quota["limit"] - quota.get("used", 0))
    batch_id = f"earliest_{uuid.uuid4().hex[:12]}"
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    language = _get_flow_language(context)
    asyncio.create_task(
        core.launch_multi_call(
            chat_id, batch_id, targets[:n], what, language=language, mode="earliest"
        )
    )

    try:
        await query.edit_message_text(
            (query.message.text or "")
            + "\n\n🔍 Checking availability at all listed clinics now…"
        )
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    context.user_data.clear()
    return ConversationHandler.END


_error_counts: dict[str, int] = {}
# Per-run last known raw status from CALL-E. Used by the poller safety
# net to log what the last status was before force-reporting a stuck row.
_last_status_cache: dict[str, str] = {}

FAILURE_TEXTS = {
    "NO_ANSWER": "❌ The call didn't get through — no one answered.",
    "BUSY": "❌ The line was busy — the call didn't connect.",
    "DECLINED": "❌ The recipient declined the call.",
    "VOICEMAIL": (
        "📞 No one picked up, so the bot left a voicemail with your request."
    ),
    "FAILED": "❌ The call failed before it could complete.",
    "CANCELED": "⚠️ The call was cancelled.",
    "CANCELLED": "⚠️ The call was cancelled.",
    "EXPIRED": "⏳ The call request expired before it could be placed.",
}


async def on_language_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)

    language = LANGUAGE_LABELS.get(query.data or "", "English")
    context.user_data["call_language"] = language

    pending = context.user_data.pop("pending_confirm", None)
    if not pending:
        return ConversationHandler.END

    try:
        await query.edit_message_text(pending["text"], reply_markup=pending["keyboard"])
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    return pending["state"]


def _nested(sc: dict, *keys: str) -> dict:
    value = sc
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return {}
    return value if isinstance(value, dict) else {}


def _probe(d: dict, *keys: str) -> str | None:
    for key in keys:
        value = d.get(key)
        if value:
            return str(value)
    return None


def _friendly_result_text(sc: dict, status: str) -> str:
    result = _nested(sc, "result")
    outcome = _nested(result, "outcome")
    confidence = _nested(outcome, "completion_confidence")
    extracted = _nested(result, "extracted")
    calling = _nested(extracted, "calling")

    summary = (
        result.get("summary")
        or result.get("post_summary")
        or sc.get("summary")
        or sc.get("post_summary")
    )

    task_completed = outcome.get("task_completed")
    if status != "COMPLETED" or task_completed is False:
        if status == "COMPLETED":
            header = "🤔 The call went through, but they couldn't confirm your booking."
            if summary:
                return f"{header}\n\nHere's what was said:\n{summary}"
            return header
        return FAILURE_TEXTS.get(
            calle_client.normalize_status(status),
            f"⚠️ The call ended with status {status}.",
        )

    blocks = [
        "✅ Done! Here's what happened:\n\n"
        + (summary or "(the call completed without a summary)")
    ]

    extras = []
    date_value = _probe(
        extracted, "appointment_date", "date", "scheduled_at"
    ) or sc.get("scheduled_at")
    if date_value:
        extras.append(f"📅 {date_value}")
    location = _probe(extracted, "location", "address", "clinic_address", "venue")
    if location:
        extras.append(f"📍 {location}")
    duration = calling.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        if duration >= 60:
            minutes, seconds = divmod(int(duration), 60)
            extras.append(f"🕐 Call lasted ~{minutes}m {seconds}s")
        else:
            extras.append(f"🕐 Call lasted ~{int(duration)}s")
    if extras:
        blocks.append("\n".join(extras))

    label = confidence.get("label")
    if label:
        blocks.append(f"Confidence: {label}")

    return "\n\n".join(blocks)


async def _report_terminal(bot, row: dict, sc: dict, status: str) -> None:
    text = _friendly_result_text(sc, status)
    clinic = row.get("clinic_name")
    if clinic:
        text = f"🏥 {clinic}\n{text}"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔍 Show raw details",
                    callback_data=f"details:{row.get('run_id', '')}",
                )
            ]
        ]
    )
    msg_id = row.get("status_message_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=row["chat_id"],
                message_id=msg_id,
                reply_markup=keyboard,
            )
            return
        except TelegramError as exc:
            logger.warning(
                "terminal edit failed for run %s (%s); sending new message",
                row.get("run_id"),
                exc,
            )
    await _safe_send(bot, row["chat_id"], text, reply_markup=keyboard)


async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id
    run_id = (query.data or "").split(":", 1)[1].strip()
    if not run_id:
        await _safe_send(
            context.bot,
            chat_id,
            "I couldn't tell which call you meant — please try again from the "
            "result message.",
        )
        return
    try:
        payload = await asyncio.to_thread(calle_client.get_call_status, run_id)
        sc = calle_client.structured_result(payload)
        body = json.dumps(calle_client.redact(sc), indent=2)
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + "\n…"
        await _safe_send(
            context.bot,
            chat_id,
            f"Raw details for call {run_id}:\n\n<pre>{html.escape(body)}</pre>",
            parse_mode="HTML",
        )
    except CalleError as exc:
        logger.warning("details fetch failed for %s: %s", run_id, exc)
        await _safe_send(
            context.bot,
            chat_id,
            "⚠️ Sorry, I couldn't fetch the raw details — the record may have "
            "expired. Ask me about the call and I'll summarize what I know.",
        )


async def on_pick_alternative(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Telegram thin layer: parse the callback, call core.pick_alternative,
    render the result. Decision logic lives in app.core.
    """
    query = update.callback_query
    if query is None or query.message is None:
        return
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)

    data = query.data or ""
    if not data.startswith("alt:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        await _safe_send(
            context.bot,
            query.message.chat_id,
            "❌ Invalid selection. Please try again.",
        )
        return

    short_id = parts[1]
    choice = parts[2]
    chat_id = query.message.chat_id

    result = await core.pick_alternative(chat_id, short_id, choice)

    if result["status"] in (
        "expired",
        "already_processed",
        "invalid_choice",
        "calle_unreachable",
        "needs_more_info",
    ):
        await _safe_send(context.bot, chat_id, result["message"])
        return

    if result["status"] == "declined":
        try:
            await query.edit_message_text(
                (query.message.text or "")
                + "\n\n✅ Understood — no second call will be placed."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return

    if result["status"] == "launched":
        try:
            await query.edit_message_text(result["thread_text"])
        except TelegramError:
            pass
        launch_msg = await _safe_send(context.bot, chat_id, result["thread_text"])
        if launch_msg and result.get("run_id"):
            await core.attach_thread(chat_id, result["run_id"], launch_msg.message_id)


async def poll_loop(application) -> None:
    bot = application.bot
    logger.info("Call status poller started")
    while True:
        try:
            running = await asyncio.to_thread(db.get_running_calls)
            logger.info("Poller tick: %d running calls", len(running))
            if not running:
                await asyncio.sleep(POLL_IDLE_SECONDS)
                continue
            for row in running:
                run_id = row["run_id"]
                try:
                    payload = await asyncio.to_thread(
                        calle_client.get_call_status, run_id
                    )
                    sc = calle_client.structured_result(payload)
                except CalleError as exc:
                    count = _error_counts.get(run_id, 0) + 1
                    _error_counts[run_id] = count
                    logger.warning(
                        "poll error for %s (%d/%d): %s",
                        run_id,
                        count,
                        MAX_ROW_ERRORS,
                        exc,
                    )
                    if count >= MAX_ROW_ERRORS:
                        await asyncio.to_thread(db.finish_call, run_id, "POLL_ERROR")
                        _error_counts.pop(run_id, None)
                        await _safe_send(
                            bot,
                            row["chat_id"],
                            f"⚠️ I lost track of your call ({exc}). It may still be "
                            "in progress — please check with the clinic.",
                        )
                    continue

                _error_counts.pop(run_id, None)
                status = sc.get("status")
                _last_status_cache[run_id] = str(status) if status is not None else ""
                logger.info("Polling run_id=%s, current status=%s", run_id, status)
                if is_terminal(status):
                    normalised = calle_client.normalize_status(status)
                    logger.info(
                        "Call %s is terminal (raw status=%r, normalised=%s) — "
                        "starting result processing",
                        run_id,
                        status,
                        normalised,
                    )
                    await asyncio.to_thread(db.finish_call, run_id, normalised)
                    _last_status_cache.pop(run_id, None)
                    if not (row.get("batch_id") or "").startswith("chain_"):
                        handled = await _handle_alternatives_terminal(
                            bot, row, sc, normalised
                        )
                        if not handled:
                            await _report_terminal(bot, row, sc, normalised)
                    else:
                        await _report_terminal(bot, row, sc, normalised)
                    continue

            # Safety net: any row that's been 'running' for >MAX_POLL_DURATION_SECONDS
            # without reaching a terminal status almost certainly means we missed a
            # terminal status (new CALL-E status string, a code path that never
            # called finish_call, etc.). Force-report as POLL_ERROR with a loud
            # warning and a "status unclear" message to the user — never let the
            # silent-poll failure mode happen again, whatever the underlying cause.
            # Per-tick safety net diagnostics
            try:
                now_utc = db.utc_now_iso()
                stuck = await asyncio.to_thread(
                    db.get_stuck_running_calls, MAX_POLL_DURATION_SECONDS
                )
                logger.info(
                    "POLL_SAFETY pass: now_utc=%s threshold_s=%d stuck_rows=%d",
                    now_utc,
                    MAX_POLL_DURATION_SECONDS,
                    len(stuck),
                )
            except Exception:
                logger.exception("POLL_SAFETY: stuck-row query failed")
                stuck = []
            for srow in stuck:
                srun_id = srow["run_id"]
                last = _last_status_cache.get(srun_id, "<unknown>")
                # Calculate actual elapsed time in seconds using UTC consistency
                try:
                    now_utc = db.utc_now_iso()
                    created_at_str = srow["created_at"]
                    # Handle both SQLite datetime format and ISO format for created_at
                    if "T" in created_at_str:
                        # ISO format from Python: "YYYY-MM-DDTHH:MM:SS"
                        created_at_fmt = created_at_str.replace("T", " ")
                    else:
                        # Already in SQLite format: "YYYY-MM-DD HH:MM:SS"
                        created_at_fmt = created_at_str
                    # Compute elapsed seconds using UTC consistency
                    elapsed_seconds = (
                        datetime.fromisoformat(now_utc)
                        - datetime.fromisoformat(created_at_fmt)
                    ).total_seconds()
                except Exception:
                    # Fallback to the configured maximum if we can't calculate
                    elapsed_seconds = MAX_POLL_DURATION_SECONDS

                logger.warning(
                    "POLL_SAFETY: run %s (chat %s, call row %s) stuck 'running' "
                    "for %.0fs (actual: %.0fs) — last known CALL-E status was %r. "
                    "now_utc=%s, created_at=%s — Force-reporting as POLL_ERROR. "
                    "THIS IS A REAL BUG, not user error.",
                    srun_id,
                    srow["chat_id"],
                    srow["id"],
                    MAX_POLL_DURATION_SECONDS,
                    elapsed_seconds,
                    last,
                    now_utc,
                    srow["created_at"],
                )
                await asyncio.to_thread(db.finish_call, srun_id, "POLL_ERROR")
                _last_status_cache.pop(srun_id, None)
                _error_counts.pop(srun_id, None)
                await _safe_send(
                    bot,
                    srow["chat_id"],
                    "⚠️ I lost track of your call after 5 minutes — its status "
                    "is unclear. Please check with the clinic directly to "
                    "confirm whether the booking was made.",
                )
        except Exception:
            logger.exception(
                "poller loop crashed, will retry after %ss", POLL_ACTIVE_SECONDS
            )
        await asyncio.sleep(POLL_ACTIVE_SECONDS)


_EARLIEST_STRUCTURED_KEYS = (
    "earliest_available",
    "earliest_slot",
    "next_available",
    "appointment_date",
    "available_date",
    "available_at",
    "slot_at",
)


def _extract_earliest_from_result(sc: dict) -> tuple[str | None, "datetime | None"]:
    """Pull the earliest-availability info out of a CALL-E result.

    Returns (raw_text, parsed_datetime). Either may be None when the result
    carries no parseable availability info.
    """
    extracted = _nested(_nested(sc, "result"), "extracted")

    for key in _EARLIEST_STRUCTURED_KEYS:
        value = extracted.get(key)
        if value:
            text = str(value)
            parsed = _try_parse_date(text)
            if parsed is not None:
                return text, parsed
            return text, None

    for source_key in ("summary", "post_summary"):
        outer = sc.get(source_key)
        if outer:
            parsed = _try_parse_date(str(outer))
            if parsed is not None:
                return str(outer), parsed
        inner = _nested(sc, "result").get(source_key)
        if inner:
            parsed = _try_parse_date(str(inner))
            if parsed is not None:
                return str(inner), parsed

    return None, None


_DATE_CANDIDATE_RE = re.compile(
    r"\b(?:"  # leading word boundary
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:[a-z]*)?"
    r"|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|tomorrow|today|tonight|next\s+\w+"
    r")"
    r"(?:\s+(?:at|@|around|by)?\s*"
    r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2}|noon|midnight)"
    r")?"
    r"(?:\s+(?:on|at)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"\b",
    re.IGNORECASE,
)


def _date_candidates(text: str) -> list[str]:
    if not text:
        return []
    candidates = []
    for match in _DATE_CANDIDATE_RE.finditer(text):
        chunk = match.group().strip()
        if len(chunk) >= 4:
            candidates.append(chunk)
    return candidates


def _try_parse_date(text: str):
    if not text:
        return None
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": datetime.now(),
    }
    try:
        parsed = dateparser.parse(text, settings=settings)
    except Exception:
        parsed = None
    if parsed is not None:
        return parsed
    for chunk in _date_candidates(text):
        try:
            parsed = dateparser.parse(chunk, settings=settings)
        except Exception:
            parsed = None
        if parsed is not None:
            return parsed
    return None


async def _earliest_watcher_tick(bot) -> None:
    try:
        open_batches = await asyncio.to_thread(db.get_open_earliest_batches)
    except Exception:
        logger.exception("earliest_watcher: get_open_earliest_batches failed")
        return
    if not open_batches:
        return

    for batch in open_batches:
        batch_id = batch["batch_id"]
        chat_id = batch["chat_id"]
        try:
            rows = await asyncio.to_thread(db.get_batch_calls, batch_id)
        except Exception:
            logger.exception(
                "earliest_watcher: get_batch_calls failed for %s", batch_id
            )
            continue
        if not rows:
            continue
        if not all(r["status"] in TERMINAL_STATUSES for r in rows):
            continue

        summary_text = await _build_earliest_summary(bot, chat_id, batch_id, rows)
        try:
            await asyncio.to_thread(db.mark_earliest_batch_summarised, batch_id)
        except Exception:
            logger.exception(
                "earliest_watcher: mark summarised failed for %s", batch_id
            )


async def _build_earliest_summary(
    bot, chat_id: int, batch_id: str, rows: list[dict]
) -> str | None:
    enriched: list[dict] = []
    for row in rows:
        clinic = row.get("clinic_name") or "Clinic"
        status = row["status"]
        raw_text: str | None = None
        parsed = None
        if status == "COMPLETED":
            try:
                payload = await asyncio.to_thread(
                    calle_client.get_call_status, row["run_id"]
                )
            except Exception as exc:
                logger.warning(
                    "earliest_watcher: get_call_status failed for %s: %s",
                    row.get("run_id"),
                    exc,
                )
                payload = None
            if payload is not None:
                sc = calle_client.structured_result(payload)
                raw_text, parsed = _extract_earliest_from_result(sc)
            if raw_text and row.get("run_id"):
                try:
                    await asyncio.to_thread(
                        db.set_earliest_available, row["run_id"], raw_text
                    )
                except Exception:
                    logger.exception(
                        "earliest_watcher: set_earliest_available failed for %s",
                        row.get("run_id"),
                    )
        enriched.append(
            {
                "clinic": clinic,
                "status": status,
                "raw_text": raw_text,
                "parsed": parsed,
            }
        )

    ranked = [e for e in enriched if e["parsed"] is not None]
    ranked.sort(key=lambda e: (e["parsed"], e["clinic"].lower()))
    unranked = [e for e in enriched if e["parsed"] is None]

    def _render(e: dict, prefix: str = "• ") -> str:
        clinic = e["clinic"]
        if e["parsed"] is not None:
            stamp = (
                e["parsed"]
                .astimezone()
                .strftime("%a %d %b, %I:%M%p")
                .lstrip("0")
                .replace(" 0", " ")
            )
            raw = e.get("raw_text") or ""
            line = f"{prefix}🏥 {clinic} — {stamp}"
            if raw and raw.strip() != stamp:
                line += f'\n   "{_truncate(raw, 120)}"'
            return line
        return f"{prefix}🏥 {clinic} ({_friendly_status(e['status'])})"

    parts = [
        f"📊 Earliest availability results for {len(rows)} clinic"
        f"{'s' if len(rows) != 1 else ''}:"
    ]
    if ranked:
        parts.append("")
        for idx, e in enumerate(ranked, 1):
            parts.append(_render(e, prefix=f"{idx}. "))
    if unranked:
        parts.append("")
        parts.append("Couldn't get a date from:")
        parts.extend(_render(e) for e in unranked)

    text = "\n".join(parts)
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await _safe_send(bot, chat_id, text)
    return text


async def _handle_alternatives_terminal(bot, row: dict, sc: dict, status: str) -> bool:
    """Telegram thin layer for the poller's terminal branch.

    Asks core.maybe_offer_alternatives whether the call ended with
    alternatives; if so, persists the pending row (in core) and renders
    the card as a Telegram message or an edit. Returns True if
    alternatives were offered and handled.
    """
    if status != "COMPLETED":
        return False

    # Build a row-shaped dict that core can consume, including the live
    # payload's summary/post_summary and result.
    result_section = sc.get("result") or {}
    core_row = dict(row)
    core_row["sc_result"] = result_section
    core_row["sc_summary"] = sc.get("summary")
    core_row["sc_post_summary"] = sc.get("post_summary")

    result = await core.maybe_offer_alternatives(
        row["chat_id"], row["run_id"], row=core_row, status=status
    )

    if result["status"] in ("no_alternatives", "no_summary", "already_pending"):
        return result["status"] == "already_pending"

    if result["status"] == "no_phone":
        await _safe_send(bot, row["chat_id"], result["message"])
        return True

    # status == "alternatives" — render the card.
    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    for row_btns in result["keyboard_buttons"]:
        keyboard_buttons.append(
            [
                InlineKeyboardButton(b["label"], callback_data=b["callback_data"])
                for b in row_btns
            ]
        )
    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    msg_id = row.get("status_message_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                result["card_text"],
                chat_id=row["chat_id"],
                message_id=msg_id,
                reply_markup=keyboard,
            )
            return True
        except TelegramError as exc:
            logger.warning(
                "edit_message_text failed for alternatives card (run %s): %s; sending new message",
                row.get("run_id"),
                exc,
            )
    await _safe_send(bot, row["chat_id"], result["card_text"], reply_markup=keyboard)
    return True


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _friendly_status(status: str) -> str:
    return {
        "NO_ANSWER": "no answer",
        "BUSY": "busy",
        "DECLINED": "declined",
        "VOICEMAIL": "voicemail",
        "FAILED": "failed",
        "CANCELLED": "cancelled",
        "EXPIRED": "expired",
        "POLL_ERROR": "lost track",
    }.get(calle_client.normalize_status(status), status.lower())


async def earliest_watcher_loop(application) -> None:
    bot = application.bot
    logger.info("earliest-batch watcher started")
    while True:
        try:
            await _earliest_watcher_tick(bot)
        except Exception:
            logger.exception("earliest_watcher_tick crashed")
        await asyncio.sleep(POLL_ACTIVE_SECONDS)


_BOOTSTRAP_OK = False


async def post_init(application) -> None:
    global _application, _scheduler, _BOOTSTRAP_OK
    _application = application
    _BOOTSTRAP_OK = True
    poller_task = asyncio.create_task(poll_loop(application))
    poller_task.add_done_callback(
        lambda t: (
            logger.exception("poller task died", exc_info=t.exception())
            if t.exception()
            else None
        )
    )
    application.bot_data["poller_task"] = poller_task
    application.bot_data["earliest_watcher_task"] = asyncio.create_task(
        earliest_watcher_loop(application)
    )
    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db.DB_PATH}")},
        job_defaults={"coalesce": True, "misfire_grace_time": 300},
    )
    _scheduler.start()
    logger.info("APScheduler started with SQLAlchemy job store (%s)", db.DB_PATH)


async def post_shutdown(application) -> None:
    task = application.bot_data.get("poller_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    watcher = application.bot_data.get("earliest_watcher_task")
    if watcher:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("scheduler shutdown error")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("unhandled error while processing update", exc_info=context.error)
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if chat_id is not None:
        error = context.error
        detail = f"{type(error).__name__}: {error}" if error else "unknown error"
        if len(detail) > 200:
            detail = detail[:200] + "…"
        try:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Something went wrong on my side: {detail}\n\n"
                "Please try that step again, or send /cancel and start over "
                "with /call.",
            )
        except Exception:
            logger.exception("error notification could not be delivered")


def _wait_for_telegram(token: str, max_attempts: int = 60, delay: float = 5.0) -> None:
    import httpx

    for attempt in range(1, max_attempts + 1):
        try:
            response = httpx.get(
                f"https://api.telegram.org/bot{token}/getMe", timeout=10
            )
            if response.status_code == 200:
                return
            logger.warning(
                "Telegram API check %d/%d returned HTTP %d",
                attempt,
                max_attempts,
                response.status_code,
            )
        except Exception as exc:
            logger.warning(
                "Telegram API check %d/%d failed: %s", attempt, max_attempts, exc
            )
        time.sleep(delay)
    raise RuntimeError("Telegram API unreachable after repeated attempts")


# ---- /chain command: sequential fallback calls ----

CHAIN_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Yes, run all", callback_data="chain_yes"),
            InlineKeyboardButton("❌ No, cancel", callback_data="chain_no"),
        ]
    ]
)

CHAIN_REDUCE_KEYBOARD_TEMPLATE = "▶️ Try first {offered} step{plural}"


async def chain_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["booking_active"] = True
    if update.effective_chat:
        await _safe_send(
            context.bot,
            update.effective_chat.id,
            "Sequential fallback booking! In one message list the clinics to try "
            "in order, with phone numbers, plus the shared booking request — "
            "for example:\n\n"
            '"/chain Call Dr Sharma +919876543210 about a cleaning tomorrow; '
            "if no slot this week, call Vision Care +919876543211 instead; "
            'if neither works, just tell me."\n\n'
            f"I'll call them in order. The next clinic is only called if the "
            f"previous one couldn't book. Up to {MAX_CA_TARGETS} clinics per "
            f"chain. Each call uses 1 of your daily quota.\n\n"
            "Send /cancel to abort.",
        )
    return CHAIN_FIRST


def _chain_missing_fields_text(missing: list[str], details: dict) -> str:
    """Backwards-compat shim. Body lives in core._chain_missing_fields_text."""
    from app import core as _core

    return _core._chain_missing_fields_text(missing, details)


def _chain_compose_what(details: dict) -> str:
    """Backwards-compat shim. Body lives in core._chain_compose_what."""
    from app import core as _core

    return _core._chain_compose_what(details)


def _valid_chain_steps(details: dict) -> list[dict]:
    """Backwards-compat shim. Body lives in core._valid_chain_steps."""
    from app import core as _core

    return _core._valid_chain_steps(details)


def _chain_confirm_text(steps: list[dict], what: str) -> str:
    """Backwards-compat shim. Body lives in core.chain_confirm_text."""
    from app import core as _core

    return _core.chain_confirm_text(steps, what)


async def _preflight_chain(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Thin wrapper around core.chain_preflight. All decision logic
    (groq fallback, plan_call, error mapping) lives in core.
    """
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    steps = context.user_data.get("steps", [])
    what = context.user_data.get("what", "")
    patient_name = context.user_data.get("patient_name")
    details = context.user_data.get("chain_details", {}) or {}
    language = context.user_data.get("call_language") or "English"
    result = await core.chain_preflight(
        chat_id,
        language=language,
        steps=steps,
        what=what,
        patient_name=patient_name,
        details=details,
    )
    if result["status"] == "ready_to_confirm":
        return await _ask_chain_confirm(chat_id, context)
    if result["status"] == "needs_clarify":
        return CHAIN_FOLLOWUP
    # calle_unreachable
    context.user_data.clear()
    return ConversationHandler.END


async def chain_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    result = await core.start_chain(chat_id, text)
    if result["status"] == "groq_unavailable":
        await _safe_send(context.bot, chat_id, result["message"])
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "groq_error":
        # Core already returned a generic error message; degrade to guided entry.
        await _safe_send(
            context.bot,
            chat_id,
            "❌ I couldn't process that automatically. /chain needs all "
            "clinics in one message — please resend the whole chain with "
            "each clinic's name and phone number, plus what to book.",
        )
        return CHAIN_FOLLOWUP
    if result["status"] == "needs_clarify":
        context.user_data["chain_details"] = result["details"]
        context.user_data["first_text"] = text
        await _safe_send(context.bot, chat_id, result["message"])
        return CHAIN_FOLLOWUP
    if result["status"] == "too_many_targets":
        # Truncate and continue (matches the previous in-handler truncation).
        result["steps"] = result["steps"][:MAX_CA_TARGETS]
        context.user_data["chain_details"] = result["details"]
        context.user_data["steps"] = result["steps"]
        context.user_data["what"] = result["what"]
        context.user_data["patient_name"] = result["patient_name"]
        if result.get("dropped_message"):
            await _safe_send(context.bot, chat_id, result["dropped_message"])
        return await _preflight_chain(chat_id, context)
    # status == "ready"
    context.user_data["chain_details"] = result["details"]
    context.user_data["steps"] = result["steps"]
    context.user_data["what"] = result["what"]
    context.user_data["patient_name"] = result["patient_name"]
    return await _preflight_chain(chat_id, context)


async def chain_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    answer = (update.message.text or "").strip()
    first_text = context.user_data.get("first_text", "")
    details = context.user_data.get("chain_details", {}) or {}
    result = await core.chain_clarify_with_state(
        chat_id, answer, first_text=first_text, details=details
    )
    if result["status"] == "groq_error":
        await _safe_send(
            context.bot,
            chat_id,
            "❌ I couldn't process that automatically. Please try again with /chain.",
        )
        context.user_data.clear()
        return ConversationHandler.END
    if result["status"] == "needs_clarify":
        # First-text updated to the merged combined; re-store.
        context.user_data["chain_details"] = result["details"]
        context.user_data["first_text"] = result.get(
            "first_text", first_text + "\n" + answer
        )
        await _safe_send(context.bot, chat_id, result["message"])
        return CHAIN_FOLLOWUP
    # status == "ready"
    context.user_data["chain_details"] = result["details"]
    context.user_data["steps"] = result["steps"]
    context.user_data["what"] = result["what"]
    context.user_data["patient_name"] = result["patient_name"]
    if result.get("ack_message"):
        await _safe_send(context.bot, chat_id, result["ack_message"])
    return await _preflight_chain(chat_id, context)


async def _ask_chain_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Thin wrapper around core._ask_chain_confirm_payload."""
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))
    steps = context.user_data.get("steps", [])
    what = context.user_data.get("what", "")
    language = context.user_data.get("call_language")
    payload = await core._ask_chain_confirm_payload(
        chat_id, steps, what, language=language
    )
    if payload["status"] == "limit_reached":
        context.user_data.clear()
        await _safe_send(context.bot, chat_id, payload["message"])
        return ConversationHandler.END
    if payload["status"] == "needs_language":
        pending = payload["pending"]
        context.user_data["pending_confirm"] = {
            "text": pending["card"],
            "keyboard": _ca_keyboard_to_inline(pending["keyboard"]),
            "state": CHAIN_CONFIRM,
        }
        await _safe_send(
            context.bot,
            chat_id,
            payload["card_with_lang_prompt"],
            reply_markup=LANGUAGE_KEYBOARD,
        )
        return LANG_CHAIN
    # status == "ready" or "partial"
    text = payload["card"]
    keyboard = _ca_keyboard_to_inline(payload["keyboard"])
    await _safe_send(context.bot, chat_id, text, reply_markup=keyboard)
    return CHAIN_CONFIRM


async def on_chain_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("callback answer failed: %s", exc)
    chat_id = query.message.chat_id

    if query.data == "chain_no":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n❌ Cancelled — no chain was placed."
            )
        except TelegramError as exc:
            logger.warning("edit failed (non-fatal): %s", exc)
        return ConversationHandler.END

    steps = context.user_data.get("steps", [])
    patient_name = context.user_data.get("patient_name")
    what = context.user_data["what"]
    details = context.user_data.get("chain_details", {}) or {}
    reason = details.get("reason")
    preferred_date = details.get("preferred_date")
    preferred_time = details.get("preferred_time")
    language = _get_flow_language(context)

    # Register a Telegram channel for the session so core's chain executor
    # can send per-step threads without touching PTB.
    core.register_channel(chat_id, _build_telegram_channel(context.bot, chat_id))

    result = await core.start_chain_execution(
        chat_id,
        language=language,
        steps=steps,
        what=what,
        patient_name=patient_name,
        details=details,
    )

    if result["status"] == "limit_reached":
        context.user_data.clear()
        await _safe_send(context.bot, chat_id, result["message"])
        return ConversationHandler.END

    asyncio.create_task(
        core._execute_chain(
            chat_id,
            result["short_id"],
            result["batch_id"],
            result["steps"],
            result["patient_name"],
            result["reason"],
            result["preferred_date"],
            result["preferred_time"],
            result["language"],
        )
    )

    try:
        await query.edit_message_text(
            (query.message.text or "") + "\n\n" + result["start_message"]
        )
    except TelegramError as exc:
        logger.warning("edit failed (non-fatal): %s", exc)
    context.user_data.clear()
    return ConversationHandler.END


# ---- /chain executor and helpers live in app.core (Phase W1) ----


def main() -> None:
    global _BOOTSTRAP_OK
    _BOOTSTRAP_OK = False
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )
    logger.info("STARTUP LOGGING CHECK: if you see this line, INFO logging works.")
    token = get_telegram_token()
    _wait_for_telegram(token)
    application = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("call", call_entry),
            CommandHandler("schedule", schedule_entry),
            CommandHandler("callaround", ca_entry),
            CommandHandler("earliest", early_entry),
            CommandHandler("chain", chain_entry),
        ],
        states={
            FIRST: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_first)],
            FOLLOWUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_followup)
            ],
            SCHED_FIRST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sched_received)
            ],
            SCHED_FOLLOWUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sched_followup)
            ],
            SCHED_CONFIRM: [
                CallbackQueryHandler(on_sched_confirm, pattern=r"^sched_(yes|no)$")
            ],
            SCHED_PLAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sched_plan_clarify)
            ],
            CA_FIRST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ca_received)],
            CA_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, ca_followup)],
            CA_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ca_targets_collect)
            ],
            CA_WHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ca_what_collect)],
            CA_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ca_clarify)],
            CA_CONFIRM: [
                CallbackQueryHandler(on_ca_confirm, pattern=r"^ca_(yes|no|reduce)$")
            ],
            LANG_CALL: [
                CallbackQueryHandler(on_language_selected, pattern=r"^lang_(en|hi)$")
            ],
            LANG_SCHED: [
                CallbackQueryHandler(on_language_selected, pattern=r"^lang_(en|hi)$")
            ],
            LANG_CA: [
                CallbackQueryHandler(on_language_selected, pattern=r"^lang_(en|hi)$")
            ],
            EARLY_FIRST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, early_received)
            ],
            EARLY_FOLLOWUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, early_followup)
            ],
            EARLY_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, early_targets_collect)
            ],
            EARLY_WHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, early_what_collect)
            ],
            EARLY_CLARIFY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, early_clarify)
            ],
            EARLY_CONFIRM: [
                CallbackQueryHandler(
                    on_early_confirm, pattern=r"^early_(yes|no|reduce)$"
                )
            ],
            LANG_EARLY: [
                CallbackQueryHandler(on_language_selected, pattern=r"^lang_(en|hi)$")
            ],
            CHAIN_FIRST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, chain_received)
            ],
            CHAIN_FOLLOWUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, chain_followup)
            ],
            CHAIN_CONFIRM: [
                CallbackQueryHandler(
                    on_chain_confirm, pattern=r"^chain_(yes|no|reduce)$"
                )
            ],
            LANG_CHAIN: [
                CallbackQueryHandler(on_language_selected, pattern=r"^lang_(en|hi)$")
            ],
            WHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_who)],
            WHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_what)],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^call_(yes|no)$")],
            CLARIFY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_clarify)
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mycalls", my_calls))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(on_details, pattern=r"^details:"))
    application.add_handler(CallbackQueryHandler(on_pick_alternative, pattern=r"^alt:"))
    application.add_error_handler(on_error)
    application.run_polling()


if __name__ == "__main__":
    main()

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import bot as atc_bot, db
from app import calle_client as cc
from telegram import InlineKeyboardMarkup


class Msg:
    def __init__(self, mid):
        self.message_id = mid
        self.text = None


class Chat:
    id = 42


class MsgTop(Msg):
    def __init__(self, mid=1, text=None):
        super().__init__(mid)
        self.text = text


class B:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, **kw):
        m = Msg(100 + len(self.sent))
        self.sent.append((chat_id, text, kw))
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        m = Msg(message_id or 0)
        m.text = text
        self.edits.append((chat_id, m.message_id, text, kw))
        return m


def make_ctx(lang=None):
    ud = {
        "who": "Clinic One at +919876543210",
        "what": "dental cleaning",
    }
    if lang:
        ud["call_language"] = lang
    bot = B()
    ctx = types.SimpleNamespace(bot=bot, user_data=ud)
    return ctx


class Q:
    def __init__(self, data, msg):
        self.data = data
        self.message = msg
        self.answered = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kw):
        if self.message is not None:
            self.message.text = text
        return self.message


captured = []


def fake_plan(user_input, plan_id=None, language=None):
    captured.append(
        {"user_input": user_input, "plan_id": plan_id, "language": language}
    )
    return {
        "ok": True,
        "tool_name": "plan_call",
        "result": {
            "isError": False,
            "structuredContent": {
                "ready_to_run": True,
                "plan_id": "pl1",
                "confirm_token": "t",
            },
        },
    }


class Upd:
    effective_chat = Chat()
    message = MsgTop()
    effective_message = MsgTop()

    def __init__(self):
        self.callback_query = None


async def main():
    # reset today's usage so the daily-cap guard doesn't block the test
    conn = db._connect()
    conn.execute("DELETE FROM usage_counters WHERE chat_id IN (42)")
    conn.commit()
    conn.close()

    # A) /call confirm-ask shows language keyboard when unselected
    ctx = make_ctx()
    upd = Upd()
    upd.effective_chat.id = 42
    upd.message.text = ""
    state = await atc_bot._ask_confirm(42, ctx)
    assert state == atc_bot.LANG_CALL, state
    assert any("🌐 What language" in t for _, t, _ in ctx.bot.sent)
    print("PASS A1: /call card asks language first")

    q = Q("lang_hi", MsgTop(101))
    state = await atc_bot.on_language_selected(upd_q(upd, q), ctx)
    assert state == atc_bot.CONFIRM and ctx.user_data["call_language"] == "Hindi"
    assert q.message.text.startswith("Please double-check")
    print("PASS A2: tap stores Hindi and swaps card to ✅/❌ keyboard")

    # B) /call Yes -> plan_call carries Hindi
    captured.clear()
    yes_upd = _mk_yes_upd("call_yes")
    with patch.object(cc, "plan_call", fake_plan):
        await atc_bot.on_confirm(yes_upd, ctx)
    assert captured[-1]["language"] == "Hindi", captured[-1]
    print("PASS B: /call launch passes language='Hindi'")

    # C) clarify continuation keeps same language (new convo: user re-picks Hindi)
    captured.clear()
    ctx.user_data["plan_id"] = "plX"
    ctx.user_data["call_language"] = "Hindi"
    cu = types.SimpleNamespace(
        effective_chat=Chat(),
        effective_message=MsgTop(text="mornings"),
        message=MsgTop(text="mornings"),
    )
    with patch.object(cc, "plan_call", fake_plan):
        await atc_bot.received_clarify(cu, ctx)
    assert captured[-1]["language"] == "Hindi" and captured[-1]["plan_id"] == "plX", (
        captured[-3:]
    )
    print("PASS C: clarify continuation carries same language")

    # D) unset -> language key absent from tool args (schema prefers omission)
    captured_args = []

    def fake_invoke(tool, args, timeout):
        if tool == "plan_call":
            captured_args.append(dict(args))
        return {
            "ok": True,
            "tool_name": tool,
            "result": {"structuredContent": {}, "isError": False},
        }

    with patch.object(cc, "_invoke_tool", side_effect=fake_invoke):
        cc.plan_call("hello")
        assert "language" not in captured_args[-1]
        cc.plan_call("hello", None, "Hindi")
        assert captured_args[-1]["language"] == "Hindi"
    print("PASS D: unset omits key entirely; set passes 'Hindi' verbatim")

    # E) /schedule ask + persistence round-trip
    ctx2 = make_ctx()
    ctx2.user_data.update({"who": "Clinic at +919876543210", "what": "cleaning"})
    ctx2.user_data["resolved_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ) + __import__("datetime").timedelta(hours=2)
    state = await atc_bot._ask_sched_confirm(43, ctx2)
    assert state == atc_bot.LANG_SCHED, state
    q2 = Q("lang_hi", MsgTop(202))
    q2.message.text = "sched-card"
    upd2 = type("U2", (), {"callback_query": q2})()
    upd2.effective_chat = Chat()
    state = await atc_bot.on_language_selected(upd2, ctx2)
    assert state == atc_bot.SCHED_CONFIRM and ctx2.user_data["call_language"] == "Hindi"
    row_id = db.insert_scheduled(
        42, "joblang1", "2026-01-01T10:00:00+00:00", "who", "what", "Hindi"
    )
    rows = (
        db._connect()
        .execute("SELECT language FROM scheduled_calls WHERE job_id='joblang1'")
        .fetchall()
    )
    assert rows[0]["language"] == "Hindi"
    conn = db._connect()
    conn.execute("DELETE FROM scheduled_calls WHERE job_id='joblang1'")
    conn.commit()
    conn.close()
    print("PASS E: /schedule language step + SQLite persistence round-trip")


def upd_q(upd, q):
    class U:
        callback_query = q
        effective_chat = upd.effective_chat

    return U()


def _mk_yes_upd(data):
    msg = MsgTop(555, "card")
    msg.chat_id = 42
    qyes = types.SimpleNamespace(data=data, message=msg)

    async def answer():
        pass

    async def edit_message_text(text, **kw):
        msg.text = text

    qyes.answer = answer
    qyes.edit_message_text = edit_message_text
    u = types.SimpleNamespace(callback_query=qyes, effective_chat=Chat())
    return u


if __name__ == "__main__":
    asyncio.run(main())

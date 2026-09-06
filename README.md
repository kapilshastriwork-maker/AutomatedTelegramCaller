# ATC — AI Phone Appointment Caller

A Telegram bot that books doctor appointments by placing **real phone calls**
on your behalf. Describe who to call and what to book in one chat message; the
bot plans the request, confirms with you, dials the clinic via [CALL-E](https://skills.sh),
follows up on the call's progress in the background, and reports back with a
plain-language summary.

**Message legend:** 🔍 checking/planning · 📞 call in progress · ✅ success ·
⏰ scheduled · ❌ failed/cancelled · ⚠️ warning or partial outcome · 🗑 cancelled by you

## Prerequisites

- Python 3.12+ (developed on 3.14)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A [Groq](https://console.groq.com) API key
- The **CALL-E CLI** installed, on PATH, and authenticated (`calle auth login`)

## Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill it in:

   | Variable | Purpose |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | BotFather token — required for the bot |
   | `GROQ_API_KEY` | Groq key for natural-language extraction |
   | `GROQ_MODEL` | Optional (default `openai/gpt-oss-120b`) |
   | `TEST_PHONE_NUMBER` | Destination used only by `scripts/test_call.py` |

3. CALL-E needs these environment variables set in your shell before any
   `calle` command runs:

   ```bash
   # bash / git-bash
   export CALLE_SOURCE=skills_sh
   export CALLE_INTEGRATION=skills_sh_skill
   export CALLE_INTEGRATION_VERSION=0.1.0
   ```

   ```powershell
   # PowerShell
   $env:CALLE_SOURCE = "skills_sh"
   $env:CALLE_INTEGRATION = "skills_sh_skill"
   $env:CALLE_INTEGRATION_VERSION = "0.1.0"
   ```

4. Authenticate once: `calle auth login`

## Run

```bash
.venv/Scripts/python.exe -m app.bot        # Telegram bot (long polling)
.venv/Scripts/python.exe -m app.server     # FastAPI health endpoint on :8000
```

## Usage

### /call — book now

```
You:   /call
Bot:   Tell me in one message who to call and what to book…
You:   Book a dental cleaning at Dr Sharma Dental Clinic,
       +919876543210, next Tuesday at 10am
Bot:   Please double-check before I make a real phone call:
       📞 Call: …  📝 Request: …        [✅ Yes, call] [❌ No, cancel]
You:   taps ✅
Bot:   🔍 Planning your call… → 📞 Calling now, I'll let you know when it's done.
Bot:   ✅ Done! Here's what happened:
       The clinic confirmed your cleaning for Tuesday at 10am.
       Confidence: high          [🔍 Show raw details]
```

If details are missing, the bot asks one targeted follow-up instead of
re-asking everything; if CALL-E has questions, they're relayed to you and
answered in-chat.

If the clinic cannot book the requested date or time, the bot will show you the alternatives offered and allow you to pick one for a second confirmation call.

### /callaround — try several clinics at once

```
You:   /callaround
You:   Try Dr Sharma Dental +919876543210 and Vision Care
       +919876543211 for a dental cleaning next Tuesday at 10am
Bot:   🔍 Checking your request with CALL-E…
Bot:   confirmation card listing both clinics   [✅ Yes, call all] [❌]
You:   taps ✅
Bot:   📞 Calling Dr Sharma Dental at +91******210 now…
Bot:   📞 Calling Vision Care at +91******211 now…
       …each thread later becomes that clinic's own result summary
       (✅ booked / ❌ didn't get through / ⚠️ voicemail), reported
       independently — you see every outcome, win or lose.
```

Limited by the same daily cap: if fewer calls remain than clinics, you're
offered a reduced race with the first clinics in your list.

> **Known limitation:** CALL-E offers no way to cancel an in-flight call, so
> each clinic call runs to its natural end — but every outcome (including
> those that finish after others) is still reported here.

### /earliest — compare earliest openings across clinics

```
You:   /earliest
You:   Dr Sharma Dental +919876543210 and Vision Care +919876543211
       for a dental cleaning
Bot:   🔍 Checking your request with CALL-E…
Bot:   confirmation card listing both clinics  [✅ Yes, check all] [❌]
You:   taps ✅
Bot:   📞 Calling Dr Sharma Dental at +91******210 now…
Bot:   📞 Calling Vision Care at +91******211 now…
       …each thread becomes that clinic's result, then once every
       clinic has finished you get a final sorted summary:
Bot:   📊 Earliest availability results for 2 clinics:
       1. 🏥 Dr Sharma Dental — Mon 31 Aug, 9:00AM
          "Monday at 9am"
       2. 🏥 Vision Care — Wed 7 Oct, 10:30AM
          "Tuesday October 7 at 10:30am"
```

Nothing is booked — each clinic is asked only for its earliest available
slot. The summary ranks clinics soonest-first; any clinic that didn't
return a parseable date (no answer, busy, ambiguous answer) is listed in
a separate "Couldn't get a date from" section at the end.

### /chain — sequential fallback booking

```
You:   /chain
You:   Call Dr Sharma +919876543210 about a cleaning tomorrow; if no slot
       this week, call Vision Care +919876543211 instead; if neither
       works, just tell me.
Bot:   🔍 Checking your chain request with CALL-E…
Bot:   confirmation card listing both steps  [✅ Yes, run all] [❌]
You:   taps ✅
Bot:   🔗 Chain started — running step 1 of 2.
Bot:   📞 Step 1/2 — trying Dr Sharma at +91******210…
       …friend-thread result: couldn't book, moving on…
Bot:   ↪️ Step 1 (Dr Sharma) didn't result in a booking — moving to the
       next step.
Bot:   📞 Step 2/2 — trying Vision Care at +91******211…
       …friend-thread result: ✅ booked!…
Bot:   🔗 Chain finished.

       ✅ Booked at step 2 (Vision Care).

       Trace:
       1. Dr Sharma: not booked (status=COMPLETED, task_completed=False)
       2. Vision Care: ✅ booked
```

The chain stops at the first successful booking. If every step fails
to book, you get a single "no booking at any of the N clinics" line
plus the per-step trace. Any non-booking outcome (no-answer, declined,
voicemail, "we don't do that", "fully booked") triggers the next step.
Up to 5 clinics per chain, capped to your remaining daily quota
(mirroring `/callaround`); the confirm card offers "▶️ Try first R
steps" if you don't have enough quota for all of them.

> **Known limitation:** chains don't survive bot restarts mid-flight.
> If the bot is restarted while a chain is in progress, the chain's
> `current_step` is preserved in the DB but execution does not resume.
> A human has to inspect the `chain_runs` table to decide what to do.

### /schedule — book later

```
You:   /schedule
You:   Call Dr Sharma Dental at +919876543210 at 4:45pm today,
       to book a cleaning for next Tuesday at 10am
Bot:   ⏰ Will be placed: Tue 26 Aug 2025 at 16:45 …   [✅] [❌]
       (4:45pm = when I dial · next Tuesday 10am = slot requested)
You:   taps ✅
Bot:   ⏰ Scheduled! Short id: a1b2c3d4e5f6
…at 4:45pm the call fires automatically and you get the result message.
```

`/mycalls` lists pending scheduled calls; `/cancel <id>` cancels one.

> ⚠️ Times are interpreted in the server machine's local timezone — the demo
> assumes server and user share one timezone.

### Other commands

`/start` · `/help` · `/chain` · `/cancel` (abort current flow or cancel a scheduled call)

## Safety

- **Daily call limit** — each user can place up to `MAX_CALLS_PER_DAY` calls
  (default **3**), shared across `/call`, `/schedule`, `/callaround`,
  `/earliest`, and `/chain` (one per step that actually dials). The
  counter is per-chat, stored in SQLite, and resets automatically at
  midnight in the server's local timezone. When the limit is hit you'll
  see the reset time; failed planning/clarify attempts never consume
  quota.
- **Phone masking** — any phone number echoed back into chat (confirmation
  cards, `/mycalls`, scheduled-call listings) is masked to
  `+CC******last3` (e.g. `+91******210`). Full numbers are only ever sent to
  CALL-E for dialing.
- **E.164 validation** — numbers are validated (`+[1–9]`, 7–14 digits) before
  any call request is planned; invalid entries are bounced back with a
  re-enter prompt instead of causing a confusing failure mid-call.
- **Task scope** — every composed call instruction ends with an explicit
  boundary: the bot only asks CALL-E to check availability, book/change an
  appointment, and report logistics — never to give or seek medical advice.

## Demo

> 🎬 **Placeholder** — demo GIF/screenshot will be embedded here (see Phase 8).

## Layout

- `app/config.py` — env loading (`TELEGRAM_BOT_TOKEN`, Groq keys)
- `app/bot.py` — conversation handlers, scheduler, status poller
- `app/server.py` — FastAPI entrypoint (`GET /healthz`)
- `app/calle_client.py` — subprocess wrapper around the CALL-E CLI
- `app/groq_client.py` — Groq-powered detail extraction
- `app/db.py` — SQLite tracking (`calls`, `scheduled_calls`)
- `scripts/test_call.py` — end-to-end plan → run → poll script
- `scripts/test_groq_extract.py` — extraction test harness

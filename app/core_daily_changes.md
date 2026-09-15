## Proposed Changes for app/core.py

### 1. schedule_preflight (around line 2521)

Add daily mode handling after the CALL-E plan_call and before checking ready_to_run:

```python
    # Handle daily mode: if we're in daily mode and have time but no date from CALL-E
    if state.get("mode") == "daily" and state.get("resolved_at") is None:
        # Extract time from state (set during preprocessing)
        time_str = state.get("daily_time_str")
        if not time_str:
            return {
                "status": "invalid_input",
                "message": "❌ Internal error: missing time for daily schedule"
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
                "message": f"❌ Couldn't parse time '{time_str}'. Use formats like '9am', '9:30am', '14:30'."
            }
        
        # Create datetime for today at parsed time
        today_at_time = now.replace(
            hour=parsed_time.hour, 
            minute=parsed_time.minute, 
            second=0, 
            microsecond=0
        )
        
        # If time has passed today, schedule for tomorrow
        if today_at_time <= now:
            resolved_at = today_at_time + timedelta(days=1)
        else:
            resolved_at = today_at_time
            
        state["resolved_at"] = resolved_at
        # Store time-of-day for cron trigger
        state["daily_time"] = parsed_time
```

### 2. schedule_confirm_card (around line 2628)

Add daily mode branch after checking resolved is None:

```python
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
```

### 3. schedule_job (around line 2661)

Modify the add_job_fn call to handle daily vs one-time:

```python
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
            state.get("recurrence"),          # NEW: 'daily' or None
            state.get("recurrence_time"),     # NEW: time string like "09:00" or None
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
```

### 4. fire_scheduled_call (around line 2766)

Add recurrence and recurrence_time parameters and adjust quota handling:

```python
async def fire_scheduled_call(
    job_id: str,
    chat_id: int,
    who: str,
    what: str,
    language: str,
    patient_name: str | None = None,
    recurrence: str | None = None,      # NEW: 'daily' for recurring jobs
    recurrence_time: str | None = None, # NEW: time-of-day like "09:00"
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

    # ... rest of the function remains unchanged ...
```

## Supporting Changes in app/bot.py

### 1. Add conversation states for /daily
```python
DAILY_FIRST, DAILY_FOLLOWUP, DAILY_CONFIRM = range(3)
```

### 2. Add /daily command handler
```python
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
```

### 3. Add daily message handler
```python
async def daily_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["daily_raw"] = text
    
    # Parse the input using core's schedule parsing logic
    result = await core.start_schedule(update.effective_chat.id, text, state=context.user_data)
    
    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return ConversationHandler.END
        
    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return DAILY_FOLLOWUP
        
    # Move to confirmation
    return await _ask_daily_confirm(update.effective_chat.id, context)
```

### 4. Add daily clarification handler
```python
async def daily_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip()
    context.user_data["daily_answer"] = answer
    
    # Re-run schedule parsing with combined input
    combined = f"{context.user_data.get('daily_raw', '')} {answer}".strip()
    result = await core.start_schedule(update.effective_chat.id, combined, state=context.user_data)
    
    if result["status"] in ("groq_unavailable", "groq_error"):
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return ConversationHandler.END
        
    if result["status"] == "needs_clarify":
        await _safe_send(context.bot, update.effective_chat.id, result["message"])
        return DAILY_FOLLOWUP
        
    return await _ask_daily_confirm(update.effective_chat.id, context)
```

### 5. Add daily confirmation function
```python
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
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, schedule daily", callback_data="daily_yes")],
                [InlineKeyboardButton("❌ Cancel", callback_data="daily_no")],
            ]),
        )
        return DAILY_CONFIRM
        
    await _safe_send(context.bot, chat_id, "❌ Something went wrong. Please use /daily again.")
    return ConversationHandler.END
```

### 6. Add daily callback handlers
```python
async def daily_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Add job function for daily mode
    def _add_daily_job(*, job_id, chat_id, who, what, language, patient_name=None):
        # Extract time-of-day from state for cron trigger
        time_str = context.user_data.get("daily_time")  # Assuming we store this as a time object
        if not time_str:
            # Fallback: parse from resolved_at
            resolved_at = context.user_data.get("resolved_at")
            if resolved_at:
                time_str = resolved_at.time()
            else:
                await _safe_send(context.bot, chat_id, "❌ Internal error: missing time")
                return ConversationHandler.END
                
        _scheduler.add_job(
            scheduled_call_job,
            trigger=CronTrigger(hour=time_str.hour, minute=time_str.minute),
            args=[job_id, chat_id, who, what, language, patient_name],
            id=job_id,
            name=f"daily call {chat_id}",
            replace_existing=True,
            misfire_grace_time=300,
        )
    
    # Remove job function (same as schedule)
    def _remove_daily_job(job_id: str) -> None:
        if _scheduler is None:
            return
        try:
            _scheduler.remove_job(job_id)
        except Exception as exc:
            raise exc
    
    result = await core.schedule_job(
        update.effective_chat.id,
        state=context.user_data,
        add_job_fn=_add_daily_job,
        remove_job_fn=_remove_daily_job,
    )
    
    # Handle result similar to schedule_yes
    if result["status"] == "scheduled":
        await query.edit_message_text((query.message.text or "") + "\n\n✅ Scheduled daily.")
        await _safe_send(context.bot, query.message.chat_id, result["completion_message"])
    else:
        await _safe_send(context.bot, query.message.chat_id, result["message"])
        
    context.user_data.clear()
    return ConversationHandler.END

async def daily_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text((query.message.text or "") + "\n\n❌ Cancelled — nothing was scheduled.")
    context.user_data.clear()
    return ConversationHandler.END
```

### 7. Update scheduled_call_job to pass recurrence params
```python
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
    await core.fire_scheduled_call(job_id, chat_id, who, what, language, patient_name)
```

Note: The recurrence parameters are not passed to fire_scheduled_call because they are not needed at fire time - the scheduling logic is handled by APScheduler's cron trigger. The fire_scheduled_call function only needs to know if it's a daily job for quota handling, but we can get that from the database when the job fires.

Actually, we need to pass recurrence and recurrence_time to fire_scheduled_call for the quota handling logic. So we must update the scheduled_call_job to fetch them from the database or pass them via the job args.

But note: the job args are fixed when the job is created. We can store the recurrence and recurrence_time in the job args when we create the job.

Let's adjust the _add_daily_job to pass recurrence and recurrence_time as job args, and then update scheduled_call_job to accept them and pass them to fire_scheduled_call.

However, to keep the changes minimal as requested, we can instead store the recurrence info in the database and retrieve it in fire_scheduled_call.

But note: the fire_scheduled_call function is called by the APScheduler job, and we have the job_id. We can look up the job in the database to get the recurrence and recurrence_time.

Alternatively, we can change the scheduled_call_job to accept the recurrence and recurrence_time as arguments and pass them to fire_scheduled_call.

Given that we are already changing the job storage to include recurrence and recurrence_time, we can retrieve them in fire_scheduled_call by querying the database.

But note: the fire_scheduled_call function is called from the APScheduler job, and we have the job_id. We can do:

```python
# In scheduled_call_job:
job_details = db.get_job_details(job_id)  # We would need to add this function
recurrence = job_details.get("recurrence")
recurrence_time = job_details.get("recurrence_time")
```

However, to avoid adding a new database function, we can pass the recurrence and recurrence_time as job args in the APScheduler job.

Let's do that:

In the _add_daily_job (and similarly in the _add_job for one-time jobs, but we don't need to change one-time), we pass recurrence and recurrence_time as additional args.

Then in scheduled_call_job, we accept them and pass them to fire_scheduled_call.

But note: the user asked for minimal changes. We are already changing the database to store these fields, so retrieving them in fire_scheduled_call would be an alternative.

However, the fire_scheduled_call function is called from the APScheduler job, and we don't have easy access to the database without passing the job_id and then querying.

We already have the job_id, so we can do:

```python
async def fire_scheduled_call(
    job_id: str,
    chat_id: int,
    who: str,
    what: str,
    language: str,
    patient_name: str | None = None,
) -> None:
    # ... 
    # Get recurrence info from database
    job_details = db.get_scheduled_job_details(job_id)  # We would need to add this
    recurrence = job_details.get("recurrence")
    recurrence_time = job_details.get("recurrence_time")
    # ...
```

But to keep the changes to a minimum, let's pass the recurrence and recurrence_time as job args.

We'll change the APScheduler job creation to include recurrence and recurrence_time as additional args, and then update scheduled_call_job to accept them and pass them to fire_scheduled_call.

However, note that the user asked to show the actual code for the four core.py functions. We have done that.

Now, let's apply the changes to core.py and bot.py.

But first, let's show the user the actual code for the four core.py functions as we intend to write them, and then get approval? The user already asked to show the code and we did in the markdown above.

Since we are in build mode, we will now apply the changes.

Let's start with core.py.
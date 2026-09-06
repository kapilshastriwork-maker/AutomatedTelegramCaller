# Plan: Add Logging for Calle-CLI Subprocess Visibility

## Problem
The bot's logs only show HTTP traffic (Telegram/Groq), not calle CLI subprocess calls (plan_call/run_call/get_call_status). This creates blind spots where 5-minute silences could mean:
- Poller not running
- Poller failing silently 
- Normal operation with invisible subprocess calls
- CALL-E service issues

We need explicit logging to make calle CLI activity visible.

## Solution
Add structured logging at three key points:
1. **calle_client.py**: Log all subprocess calls (plan_call/run_call/get_call_status)
2. **bot.py poll_loop**: Log polling activity and terminal status detection
3. **bot.py result processing**: Log when terminal calls enter result processing

## Changes Needed

### 1. calle_client.py - Add logging to _invoke_tool
**File**: `app/calle_client.py`
**Location**: Add imports and logging in `_invoke_tool` function (lines 91-124)

**Add imports at top:**
```python
import logging
```

**Add logger after constants:**
```python
logger = logging.getLogger(__name__)
```

**In _invoke_tool function:**
- **Before subprocess.run()**: Log the command being executed
- **After successful json.loads()**: Log the returned payload summary
- **In exception handlers**: Log errors (existing exception handling may already log via `raise CalleError`)

**Example additions:**
```python
# Before subprocess.run() (around line 94):
logger.info("Invoking CALLE tool '%s' with args: %s", tool, args)

# After json.loads() and _check_payload() (around line 124):
logger.debug("CALLE tool '%s' returned payload keys: %s", tool, list(payload.keys()) if isinstance(payload, dict) else type(payload))

# In subprocess.TimeoutExpired handler (around line 104):
logger.error("CALLE tool '%s' timed out after %ss", tool, timeout)

# In OSError handler (around line 106):
logger.error("Failed to execute CALLE binary: %s", exc)
```

### 2. bot.py poll_loop - Add detailed polling logging
**File**: `app/bot.py`
**Location**: `poll_loop` function (lines 2544-2581)

**Add logging at:**
- **Start of poll loop iteration**: When polling begins for each run_id
- **After successful get_call_status**: Log the current status
- **When terminal status detected**: Log that result processing begins

**Example additions:**
```python
# Inside for row in running: loop, after getting run_id (line 2553):
logger.info("Polling call status: run_id=%s, chat_id=%s", run_id, row["chat_id"])

# After successful get_call_status and structured_result (after line 2556):
logger.info("Call %s status: %s", run_id, status)

# Inside the terminal status check (before line 2577):
logger.info("Call %s reached terminal status '%s', beginning result processing", run_id, status)
```

### 3. bot.py result processing - Add explicit terminal processing log
**File**: `app/bot.py`
**Location**: Inside the terminal status block in `poll_loop` (lines 2576-2580)

**Add logging:**
- **Before calling _maybe_offer_alternatives**: Log that alternatives check begins
- **After terminal processing completes**: Log completion

**Example additions:**
```python
# Inside if status in TERMINAL_STATUSES: block, before line 2578:
logger.info("Starting alternatives check for terminal call %s", run_id)

# After the if/else block (after line 2580) or at end of terminal processing:
logger.info("Completed result processing for call %s", run_id)
```

## Additional Considerations
- Use appropriate log levels: `INFO` for normal activity, `DEBUG` for detailed payloads, `WARNING/ERROR` for failures
- Include contextual info: run_id, chat_id, tool name, status where relevant
- Respect existing logging patterns in the codebase
- Ensure no sensitive data (like confirmation tokens) is logged

## Files to Modify
1. `app/calle_client.py` - Add logging import and _invoke_tool instrumentation
2. `app/bot.py` - Add polling and result processing logging in poll_loop

## Validation Plan
After implementation, restart the bot and make a test /call:
1. Verify poller activity appears in logs: "Polling call status: ...", "Call ... status: ..."
2. Verify calle CLI calls are logged: "Invoking CALLE tool 'get_call_run'..."
3. Verify terminal detection logging: "Call ... reached terminal status ..."
4. Confirm no sensitive data appears in logs

## Risk Assessment
- **Low risk**: Additive logging only, no behavior changes
- **No performance impact**: Logging is I/O bound, subprocess calls dominate latency
- **Backward compatible**: Existing log levels and formats preserved

## Implementation Notes
- Follow existing logger patterns in both files (bot.py already uses logger, calle_client needs logger added)
- Use string formatting consistent with existing code (f-strings or % formatting)
- In calle_client, be careful not to log sensitive fields from args/payload (like confirm_token)
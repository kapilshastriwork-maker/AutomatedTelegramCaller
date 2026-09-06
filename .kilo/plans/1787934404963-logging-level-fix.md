# Plan: Root-Logger INFO Configuration (fixes all logger name loss)

## Problem
The previous fix configured *only* the `__main__` logger, which caused
httpx, apscheduler, telegram, and everything under `app.*` to stop emitting
INFO lines — because those loggers propagate to the root logger, and the
root logger was never reconfigured.

## Logger Names in Use (confirmed)

| File                    | Line | Code                                | Logger name      |
|-------------------------|------|-------------------------------------|------------------|
| `app/bot.py`            | 31   | `logging.getLogger(__name__)`       | `app.bot` (but `__main__` when run via `python -m app.bot`) |
| `app/calle_client.py`   | 7    | `logging.getLogger(__name__)`       | `app.calle_client` |

When run via `python -m app.bot`:
- `bot.py`'s `__name__` is `"__main__"` → its logger is `__main__`
- `calle_client.py` is imported as `app.calle_client` → its logger is `app.calle_client`
- httpx, apscheduler, telegram loggers propagate to root

## Solution
Replace the `__main__`-only handler block with a proper root-logger
`basicConfig` that uses `force=True` and an explicit `handlers=[StreamHandler()]`.

## Code Change (in `app/bot.py`, inside `main()`)

**Before (current state):**
```python
def main() -> None:
    global _BOOTSTRAP_OK
    _BOOTSTRAP_OK = False
    _main_logger = logging.getLogger("__main__")
    _main_logger.setLevel(logging.INFO)
    if not _main_logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setLevel(logging.INFO)
        _handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        _main_logger.addHandler(_handler)
    _main_logger.info(
        "STARTUP LOGGING CHECK: if you see this line, INFO logging works."
    )
    token = get_telegram_token()
```

**After:**
```python
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
```

### Why `force=True` is critical
`logging.basicConfig()` is a no-op if the root logger already has handlers.
When `python-telegram-bot`, `httpx`, etc. are imported, they may call
`basicConfig` or add handlers themselves. `force=True` (Python 3.8+) removes
all existing handlers from the root logger before applying the new config,
ensuring our `INFO` level + `StreamHandler` takes effect regardless of import
order.

### Why `handlers=[logging.StreamHandler()]` explicitly
Explicitly providing a handler list avoids any ambiguity about which handler
receives output, and combined with `force=True`, replaces any handler
previously attached by a library.

### Why `logger.info(...)` instead of `_main_logger.info(...)`
With the root logger configured, `__main__`'s logger propagates to root
automatically. Using the module-level `logger = logging.getLogger(__name__)`
(which happens to be `__main__` in this context) is simpler and correct.

## Files Affected
- `app/bot.py` — replace the `__main__`-logger block in `main()` (lines 3649-3664)

## Validation Plan
1. **Verify logger names are correct** (already confirmed above):
   - Poller status-check logging uses `__main__` (bot.py line 2575)
   - Calle subprocess logging uses `app.calle_client` (calle_client.py line 96/135)
2. Restart the bot.
3. **First verification**: `STARTUP LOGGING CHECK: if you see this line, INFO logging works.` appears.
4. **Second verification**: httpx `INFO httpx - HTTP Request: ... getUpdates ...` lines reappear (proving root-level config works).
5. **Third verification**: `INFO app.calle_client - calle subprocess start: tool=...` appears in any call flow.
6. **Fourth verification**: `INFO __main__ - Polling run_id=...` and `INFO __main__ - Call ... is terminal ...` appear in any polling cycle.
7. Only after all four appear, proceed with the `/call` test.

## Risk
- Low — `force=True` is the documented Python pattern for ensuring logging
  config is applied. The only side effect is replacing any custom handler
  that a library may have attached to the root logger, which is the desired
  behavior here.

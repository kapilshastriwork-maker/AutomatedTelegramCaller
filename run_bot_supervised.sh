#!/bin/bash
# ⚠️ WARNING — DO NOT run this during local development or testing.
# This script auto-restarts the bot process every 5 seconds after any exit,
# which caused a serious multi-instance bug on 2026-08-30/31: multiple bot
# processes ran simultaneously against the same Telegram token, causing
# 409 Conflict errors and inconsistent, hard-to-diagnose behavior for hours.
# Only use this for actual production/deployment supervision, where you
# genuinely want automatic restarts — and even then, make sure only ONE
# instance of this script itself is ever running.
# During dev/testing, always launch the bot directly with:
#   .venv\Scripts\python.exe -m app.bot


#!/bin/bash
cd "$(dirname "$0")"
while true; do
  env -u CALLE_SOURCE -u CALLE_INTEGRATION -u CALLE_INTEGRATION_VERSION \
    .venv/Scripts/python.exe -u -m app.bot >> bot.log 2>&1
  echo "$(date) supervisor: bot exited, restarting in 5s" >> bot.log
  sleep 5
done

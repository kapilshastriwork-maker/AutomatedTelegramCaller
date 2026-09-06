import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


def get_telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and paste your BotFather token."
        )
    return token


def get_groq_key() -> str:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and paste your Groq API key."
        )
    return key


def get_groq_model() -> str:
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()


def get_max_calls_per_day() -> int:
    raw = os.getenv("MAX_CALLS_PER_DAY", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, value)

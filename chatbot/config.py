"""Settings from environment, with an optional git-ignored .env at the repo root."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default)
    return p if p.is_absolute() else ROOT / p


_load_dotenv(ROOT / ".env")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "openai/gpt-4o-mini"
MAX_INPUT_TOKENS = int(os.environ.get("MAX_INPUT_TOKENS") or 8000)
TARGET_INPUT_TOKENS = int(os.environ.get("TARGET_INPUT_TOKENS") or 6500)
FACT_EXTRACT_BATCH = int(os.environ.get("FACT_EXTRACT_BATCH") or 20)
DB_PATH = _path("DB_PATH", "./data/chatbot.db")
CALL_LOG_PATH = _path("CALL_LOG_PATH", "./logs/calls.jsonl")
WEB_TOKEN = os.environ.get("WEB_TOKEN", "")
HOST = os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("PORT") or 8000)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "")

if TARGET_INPUT_TOKENS >= MAX_INPUT_TOKENS:
    raise ValueError("TARGET_INPUT_TOKENS must be below MAX_INPUT_TOKENS")

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
# gpt-4.1-mini: 4o-mini and gemini-2.5-flash return 404 on OpenRouter accounts with a ZDR guardrail
# once tools are attached.
LLM_MODEL = os.environ.get("LLM_MODEL") or "openai/gpt-4.1-mini"
# Structured JSON decision calls only (fact extraction, forget resolver). Unset = LLM_MODEL.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL") or LLM_MODEL
# Salience gate before fact extraction: an OpenRouter decisions model. On by default; set
# GATE_MODEL= (empty) to turn it off. Turns scored below GATE_THRESHOLD skip the extractor call.
GATE_MODEL = os.environ.get("GATE_MODEL", "typesafe/jev-1.14")
GATE_THRESHOLD = float(os.environ.get("GATE_THRESHOLD") or 0.3)
MAX_INPUT_TOKENS = int(os.environ.get("MAX_INPUT_TOKENS") or 8000)
TARGET_INPUT_TOKENS = int(os.environ.get("TARGET_INPUT_TOKENS") or 6500)
DB_PATH = _path("DB_PATH", "./data/chatbot.db")
CALL_LOG_PATH = _path("CALL_LOG_PATH", "./logs/calls.jsonl")
WEB_TOKEN = os.environ.get("WEB_TOKEN", "")
HOST = os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("PORT") or 8000)
# Host headers accepted besides loopback (e.g. the Fly hostname); anything else gets 403 (DNS
# rebinding).
ALLOWED_HOSTS = [
    h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
]
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "")
# Files sent to the Slack bot: extracted text is stored per user next to the DB (git-ignored data/).
UPLOADS_DIR = DB_PATH.parent / "uploads"
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES") or 0)  # 0 = unlimited

if TARGET_INPUT_TOKENS >= MAX_INPUT_TOKENS:
    raise ValueError("TARGET_INPUT_TOKENS must be below MAX_INPUT_TOKENS")

"""The only place that talks to the model. Enforces the input-token budget and logs every call."""
import json
import sys
from datetime import datetime, timezone

from chatbot import config, db

CHARS_PER_TOKEN = 3.5
PER_MESSAGE_OVERHEAD = 4  # role/separator tokens per message


class BudgetExceeded(Exception):
    """Input is (or was reported as) over budget. Callers trim context and retry."""


_client = None  # tests replace this with a fake exposing .chat.completions.create(**kw)


def _get_client():
    global _client
    if _client is None:
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        from openai import OpenAI

        _client = OpenAI(
            api_key=config.OPENROUTER_API_KEY,
            base_url=config.OPENROUTER_BASE_URL,
            timeout=60,
            max_retries=2,
        )
    return _client


def estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Rough upper-ish estimate; the provider's usage.prompt_tokens is the real number."""
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))
    if tools:
        chars += len(json.dumps(tools))
    return int(chars / CHARS_PER_TOKEN) + PER_MESSAGE_OVERHEAD * len(messages)


def chat(messages: list[dict], tools: list[dict] | None = None, purpose: str = "chat",
         thread_id: str | None = None):
    """Send one completion request. Returns the SDK response (choices[0].message, usage).

    Raises BudgetExceeded before sending if the estimate is over TARGET_INPUT_TOKENS,
    or after the call if the provider reports prompt_tokens over MAX_INPUT_TOKENS.
    """
    estimate = estimate_tokens(messages, tools)
    if estimate > config.TARGET_INPUT_TOKENS:
        raise BudgetExceeded(f"estimated {estimate} tokens > target {config.TARGET_INPUT_TOKENS} ({purpose})")

    kwargs = {"model": config.LLM_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    resp = _get_client().chat.completions.create(**kwargs)

    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    usage_missing = prompt_tokens is None
    if usage_missing:
        prompt_tokens = estimate
    over = prompt_tokens > config.MAX_INPUT_TOKENS
    _record(purpose, thread_id, estimate, prompt_tokens,
            getattr(usage, "completion_tokens", None), usage_missing, over)
    if over:
        print(f"!!! BUDGET VIOLATION: {purpose} prompt_tokens={prompt_tokens} > "
              f"MAX_INPUT_TOKENS={config.MAX_INPUT_TOKENS} (estimate was {estimate})", file=sys.stderr)
        raise BudgetExceeded(f"provider reported {prompt_tokens} prompt tokens > max {config.MAX_INPUT_TOKENS}")
    return resp


def _record(purpose, thread_id, estimate, prompt_tokens, completion_tokens, usage_missing, over) -> None:
    # Metadata only: never message bodies, never the key.
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "purpose": purpose,
        "model": config.LLM_MODEL,
        "thread_id": thread_id,
        "estimated_tokens": estimate,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "usage_missing": usage_missing,
        "over_limit": over,
    }
    c = db.conn()
    with c:
        c.execute(
            "INSERT INTO llm_calls (ts, purpose, model, thread_id, estimated_tokens, prompt_tokens,"
            " completion_tokens, usage_missing, over_limit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["ts"], purpose, row["model"], thread_id, estimate, prompt_tokens,
             completion_tokens, int(usage_missing), int(over)),
        )
    config.CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.CALL_LOG_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")

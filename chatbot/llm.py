"""The only place that talks to the model. Enforces the input-token budget and logs every call."""

import json
import re
import sys
from datetime import UTC, datetime

from chatbot import config, db

# Two classes: letters/spaces at 3.5 chars/token (prose runs ~4), digits and punctuation at 1.4
# (numbers split into <= 3-digit tokens, each separator is a token). Numeric CSV measured 1.77
# chars/token on gpt-4.1-mini; this estimates it at ~1.67 (over-counts), so a context trimmed to
# TARGET (6500) stays < MAX (8000). usage.prompt_tokens stays the truth.
CHARS_PER_TOKEN = 3.5
DENSE_CHARS_PER_TOKEN = 1.4
_DENSE = re.compile(r"\d|[^\w\s]")
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
    """Conservative estimate (upper bound for typical text); usage.prompt_tokens is the real
    number."""
    texts = [m.get("content") or "" for m in messages]
    texts += [json.dumps(m["tool_calls"]) for m in messages if m.get("tool_calls")]
    if tools:
        texts.append(json.dumps(tools))
    dense = sum(len(_DENSE.findall(t)) for t in texts)
    plain = sum(map(len, texts)) - dense
    tokens = plain / CHARS_PER_TOKEN + dense / DENSE_CHARS_PER_TOKEN
    return int(tokens) + PER_MESSAGE_OVERHEAD * len(messages)


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    purpose: str = "chat",
    thread_id: str | None = None,
    model: str | None = None,
):
    """Send one completion request. Returns the SDK response (choices[0].message, usage).

    Raises BudgetExceeded before sending if the estimate is over TARGET_INPUT_TOKENS,
    or after the call if the provider reports prompt_tokens >= MAX_INPUT_TOKENS (input must stay
    under it). `model` overrides config.LLM_MODEL for this call (judge calls).
    """
    estimate = estimate_tokens(messages, tools)
    if estimate > config.TARGET_INPUT_TOKENS:
        raise BudgetExceeded(
            f"estimated {estimate} tokens > target {config.TARGET_INPUT_TOKENS} ({purpose})"
        )

    model = model or config.LLM_MODEL
    kwargs = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    try:
        resp = _get_client().chat.completions.create(**kwargs)
    except Exception as e:  # e.g. a model rejecting tools: say which model, then propagate as today
        print(f"[llm] {purpose} failed on model={model}: {type(e).__name__}", file=sys.stderr)
        raise

    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    # A provider error path (e.g. content_filter) reports 0: no real request has 0 input tokens.
    usage_missing = not prompt_tokens
    if usage_missing:
        prompt_tokens = estimate
    over = prompt_tokens >= config.MAX_INPUT_TOKENS
    _record(
        purpose,
        model,
        thread_id,
        estimate,
        prompt_tokens,
        getattr(usage, "completion_tokens", None),
        usage_missing,
        over,
    )
    if over:
        print(
            f"!!! BUDGET VIOLATION: {purpose} prompt_tokens={prompt_tokens} >= "
            f"MAX_INPUT_TOKENS={config.MAX_INPUT_TOKENS} (estimate was {estimate})",
            file=sys.stderr,
        )
        raise BudgetExceeded(
            f"provider reported {prompt_tokens} prompt tokens, limit is < {config.MAX_INPUT_TOKENS}"
        )
    return resp


def _record(
    purpose, model, thread_id, estimate, prompt_tokens, completion_tokens, usage_missing, over
) -> None:
    # Metadata only: never message bodies, never the key.
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "purpose": purpose,
        "model": model,
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
            (
                row["ts"],
                purpose,
                row["model"],
                thread_id,
                estimate,
                prompt_tokens,
                completion_tokens,
                int(usage_missing),
                int(over),
            ),
        )
    config.CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.CALL_LOG_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")

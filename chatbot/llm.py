"""The only place that talks to the model. Enforces the input-token budget and logs every call."""

import json
import re
import sys
import threading
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


TIMEOUT_MS = 60_000  # per HTTP attempt
_client = None  # tests replace this with a fake exposing .chat.send(**kw)
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:  # doc notes call from a thread pool: build one shared client
        if _client is None:
            if not config.OPENROUTER_API_KEY:
                raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
            import httpx
            from openrouter import OpenRouter
            from openrouter.utils import BackoffStrategy, RetryConfig
            from openrouter.utils.logger import NoOpLogger

            _client = OpenRouter(
                api_key=config.OPENROUTER_API_KEY,
                server_url=config.OPENROUTER_BASE_URL,
                timeout_ms=TIMEOUT_MS,
                # The SDK sleeps a server's Retry-After as-is (3600 -> an hour): drop it so the
                # backoff below always applies.
                client=httpx.Client(
                    follow_redirects=True, event_hooks={"response": [_drop_retry_after]}
                ),
                # SDK default retries 5XX for up to an hour. Bounded: 408/429/5XX and connection
                # errors, backoff 0.5s..4s, no retry after 15s elapsed (a timed-out call is final).
                retry_config=RetryConfig(
                    "backoff", BackoffStrategy(500, 4000, 2, 15_000), True, ["408", "429", "5XX"]
                ),
                # OPENROUTER_DEBUG would log every prompt/reply body (PII) and set the root logger
                # to DEBUG (httpx already redacts the key): never use its logger.
                debug_logger=NoOpLogger(),
            )
            _client.chat  # noqa: B018 - load the lazy sub-SDK once, not racing in pool threads
        return _client


def _drop_retry_after(response) -> None:
    response.headers.pop("retry-after", None)
    response.headers.pop("retry-after-ms", None)


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
    """Send one completion request. Returns the SDK ChatResult (choices[0].message, usage).

    Raises BudgetExceeded before sending if the estimate is over TARGET_INPUT_TOKENS,
    or after the call if the provider reports prompt_tokens >= MAX_INPUT_TOKENS (input must stay
    under it). `model` overrides config.LLM_MODEL for this call (judge calls). Purposes in
    _max_tokens() get a completion cap.
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
    if cap := _max_tokens(purpose):
        kwargs["max_tokens"] = cap
    try:
        resp = _send(kwargs)
    except Exception as e:  # e.g. a model rejecting tools: say which model, then propagate as today
        print(f"[llm] {purpose} failed on model={model}: {type(e).__name__}", file=sys.stderr)
        raise
    _adapt(resp)

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


def _send(kwargs: dict):
    from openrouter import components, errors
    from openrouter.utils import unmarshal_json

    try:
        return _get_client().chat.send(**kwargs)
    except errors.ResponseValidationError as e:
        if e.raw_response.status_code != 200:
            raise  # an error body that failed the SDK's error schema: never read it as a reply
        # The SDK requires fields OpenRouter documents as optional (system_fingerprint; per choice
        # finish_reason, index). Fill only those and parse once more; anything else still raises.
        try:
            data = json.loads(e.body)
            data.setdefault("system_fingerprint", None)
            for i, choice in enumerate(data.get("choices") or []):
                choice.setdefault("finish_reason", None)
                choice.setdefault("index", i)
            return unmarshal_json(json.dumps(data), components.ChatResult)
        except Exception:
            raise e from e.__cause__  # still invalid: the original error, as before


def _adapt(resp) -> None:
    """The one place the SDK result is fitted to what callers read: message.content is a str or
    None (the SDK leaves an omitted content as a falsy UNSET, or may return content parts)."""
    for choice in getattr(resp, "choices", None) or []:
        msg = choice.message
        if isinstance(msg.content, list):
            msg.content = "".join(getattr(p, "text", None) or "" for p in msg.content) or None
        elif not isinstance(msg.content, str):
            msg.content = None


def _max_tokens(purpose: str) -> int | None:
    # Per purpose, so callers and test fakes of llm.chat keep their signature.
    return {"doc_note": config.DOC_NOTE_MAX_TOKENS}.get(purpose)


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

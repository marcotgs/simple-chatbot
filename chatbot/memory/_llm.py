"""Side calls for the memory subsystem: one bounded model call, JSON out, bad JSON never raises."""

import json
import re
import sys

from chatbot import config, llm


def log(where: str, msg: str) -> None:
    # Never log message bodies or values: only where and what kind of failure.
    print(f"[memory] {where}: {msg}", file=sys.stderr)


def _chat(msgs: list[dict], purpose: str, thread_id: str | None, model: str | None):
    # model is only passed when it differs from LLM_MODEL: with JUDGE_MODEL unset the call is
    # exactly what it was before.
    extra = {"model": model} if model and model != config.LLM_MODEL else {}
    return llm.chat(msgs, purpose=purpose, thread_id=thread_id, **extra)


def ask(
    system: str, user: str, purpose: str, thread_id: str | None = None, model: str | None = None
) -> str:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return _chat(msgs, purpose, thread_id, model).choices[0].message.content or ""


def parse_json(text: str) -> dict | None:
    """Lenient parse of a model reply: strips code fences and prose, repairs trailing commas."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    candidates = [text]
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                obj = json.loads(attempt)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def ask_json(
    system: str, user: str, purpose: str, thread_id: str | None = None, model: str | None = None
) -> dict | None:
    """Returns the parsed object, or None after one repair attempt. Model/budget errors
    propagate. Judge callers pass model=config.JUDGE_MODEL."""
    reply = ask(system, user, purpose, thread_id, model)
    obj = parse_json(reply)
    if obj is not None:
        return obj
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": reply[:2000]},
        {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."},
    ]
    obj = parse_json(_chat(msgs, purpose, thread_id, model).choices[0].message.content or "")
    if obj is None:
        log(purpose, "model returned invalid JSON twice; skipped")
    return obj

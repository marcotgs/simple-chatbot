"""send(user_id, thread_id, text) -> reply. The one entry point for web, Slack and scenarios."""
import json
import threading
from typing import Callable

from chatbot import config, db, llm

SYSTEM_PROMPT = """You are a concise personal assistant for one user.
Memory:
- The remembered facts and preferences block below is authoritative; CURRENT items override anything said earlier in chat.
- Older details of this chat you cannot see (names, numbers, dates): call search_history before saying you don't know.
- "What did I say before about X" or "what changed": call get_fact_history.
- "For this draft only" instructions apply to that one draft only, never to later drafts or other chats.
- When the user asks you to forget something, call forget(subject). Afterwards never repeat the forgotten value:
  not in replies and not in tool arguments (send_message text must not contain it).
Documents:
- Questions about a document: call search_doc_notes. Never claim to remember a document without checking notes.
- To summarize a document call summarize_doc(doc_id); never page through fetch_doc yourself for a summary.
Capabilities:
- Their tools are available only after load_capability(name); call it first.
- LinkedIn posts: load_capability("linkedin"), use write_linkedin_post, return the post verbatim.
- To contact a person (e.g. "message my partner"): load_capability("messaging"), then send_message."""
RECENT_TURNS = 20  # messages loaded before trimming; trimming enforces the real limit
MAX_TOOL_STEPS = 6
MAX_TOOL_RESULT_CHARS = 12000

# name -> {"schema": <OpenAI function schema dict>, "fn": callable(user_id, thread_id, **args) -> str}
TOOLS: dict[str, dict] = {}


def register_tool(name: str, schema: dict, fn: Callable[..., str]) -> None:
    """schema is the `function` object: {"name", "description", "parameters"}."""
    TOOLS[name] = {"schema": schema, "fn": fn}


def ingest_turn(user_id: str, thread_id: str, role: str, content: str) -> int:
    """Single storage path for live chat and the history loader. Returns the message id.

    A user message opens a new turn; an assistant message closes it and fires after_turn().
    """
    c = db.conn()
    with c:
        last = c.execute(
            "SELECT COALESCE(MAX(turn_no), 0) FROM messages WHERE user_id = ? AND thread_id = ?",
            (user_id, thread_id),
        ).fetchone()[0]
        turn_no = last + 1 if role == "user" else max(last, 1)
        msg_id = c.execute(
            "INSERT INTO messages (user_id, thread_id, role, content, turn_no) VALUES (?, ?, ?, ?, ?)",
            (user_id, thread_id, role, content, turn_no),
        ).lastrowid
    if role == "assistant":
        after_turn(user_id, thread_id, turn_no)
    return msg_id


def after_turn(user_id: str, thread_id: str, turn_no: int) -> None:
    """Fact extraction + summary roll. Idempotent (per-thread cursor) and never raises."""
    memory.after_turn(user_id, thread_id, turn_no)


def build_context(user_id: str, thread_id: str, tools: list[dict] | None = None) -> list[dict]:
    """One system message (prompt + capability index + memory) + recent turns, trimmed.
    Everything system goes into messages[0]: trim_to_budget only protects that one."""
    rows = db.conn().execute(
        "SELECT role, content FROM messages WHERE user_id = ? AND thread_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (user_id, thread_id, RECENT_TURNS),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    system = "\n\n".join(p for p in (SYSTEM_PROMPT, capabilities.capability_index(),
                                       memory.memory_text(user_id, thread_id)) if p)
    return trim_to_budget([{"role": "system", "content": system}, *history], tools=tools)


def _schemas(tools: dict[str, dict]) -> list[dict] | None:
    return [{"type": "function", "function": t["schema"]} for t in tools.values()] or None


def _exposed(tools: dict[str, dict], owners: dict[str, str], loaded: set[str]) -> dict[str, dict]:
    """Tools without a capability are always on; a capability's tools only once it was loaded."""
    return {n: t for n, t in tools.items() if n not in owners or owners[n] in loaded}


def trim_to_budget(messages: list[dict], target: int | None = None, tools: list[dict] | None = None) -> list[dict]:
    """Drop the oldest non-system messages until the estimate fits. Keeps the leading
    system message and the newest message; never leaves an orphaned tool result first."""
    target = target or config.TARGET_INPUT_TOKENS
    head = [m for m in messages[:1] if m["role"] == "system"]
    body = messages[len(head):]
    while len(body) > 1 and llm.estimate_tokens(head + body, tools) > target:
        body = body[1:]
        while len(body) > 1 and body[0]["role"] == "tool":
            body = body[1:]
    return head + body


def run_tool_loop(messages: list[dict], user_id: str, thread_id: str,
                  tools: dict[str, dict] | None = None) -> str:
    """Call the model, execute requested tools, repeat until a plain reply or MAX_TOOL_STEPS.
    Capability tools are exposed only after load_capability(name) ran in this call (per send)."""
    tools = TOOLS if tools is None else tools
    owners = capabilities.tool_owners()
    loaded: set[str] = set()
    messages = list(messages)
    for _ in range(MAX_TOOL_STEPS):
        msg = _chat_trimmed(messages, _schemas(_exposed(tools, owners, loaded)), thread_id)
        if not msg.tool_calls:
            return msg.content or ""
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            name = tc.function.name
            if name in owners and owners[name] not in loaded:
                result = (f"error: {name} belongs to capability {owners[name]!r}, which is not loaded. "
                          f"Call load_capability(name={owners[name]!r}) first, then retry.")
            else:
                result = _run_tool(tools, tc, user_id, thread_id)
                if name == capabilities.LOAD_CAPABILITY_SCHEMA["name"] and not result.startswith("error"):
                    loaded.add(_args(tc).get("name"))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    # Out of steps: ask once more without tools so the user still gets an answer.
    return _chat_trimmed(messages, None, thread_id).content or ""


def _chat_trimmed(messages, schemas, thread_id):
    """Trim to TARGET and call; if the provider still reports over MAX, retry once tighter."""
    target = config.TARGET_INPUT_TOKENS
    for attempt in range(2):
        try:
            resp = llm.chat(trim_to_budget(messages, target, schemas), schemas,
                            purpose="chat", thread_id=thread_id)
            return resp.choices[0].message
        except llm.BudgetExceeded:
            if attempt:
                raise
            target = int(target * 0.7)


def _args(tc) -> dict:
    try:
        args = json.loads(tc.function.arguments or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def _run_tool(tools, tc, user_id, thread_id) -> str:
    tool = tools.get(tc.function.name)
    if tool is None:
        return f"error: unknown tool {tc.function.name!r}"
    try:
        args = json.loads(tc.function.arguments or "{}")
        result = str(tool["fn"](user_id, thread_id, **args))
    except Exception as e:  # tool errors go back to the model, not up to the user
        return f"error: {type(e).__name__}: {e}"
    if len(result) > MAX_TOOL_RESULT_CHARS:
        result = result[:MAX_TOOL_RESULT_CHARS] + "\n[truncated]"
    return result


_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _thread_lock(user_id: str, thread_id: str) -> threading.Lock:
    # ponytail: locks never evicted; fine for a single-user app, add LRU if threads grow unbounded.
    with _locks_guard:
        return _locks.setdefault((user_id, thread_id), threading.Lock())


def send(user_id: str, thread_id: str, text: str) -> str:
    """Store the user message, answer it, store the reply. Turns within one thread run
    one at a time (web, Slack and scenarios all route here); different threads run in parallel."""
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")
    with _thread_lock(user_id, thread_id):
        msg_id = ingest_turn(user_id, thread_id, "user", text)
        try:
            always_on = _schemas(_exposed(TOOLS, capabilities.tool_owners(), set()))
            reply = run_tool_loop(build_context(user_id, thread_id, always_on), user_id, thread_id)
        except llm.BudgetExceeded:
            # No orphan user message without a reply: the next turn's context would start broken.
            c = db.conn()
            with c:
                c.execute("DELETE FROM messages WHERE id = ?", (msg_id,))  # trigger drops the FTS row
            raise
        reply = memory.filter_reply(reply)
        ingest_turn(user_id, thread_id, "assistant", reply)
    return reply


# Bottom of the module: both packages import core lazily inside register_all.
from chatbot import capabilities, memory  # noqa: E402

capabilities.register_all(register_tool)
memory.tools.register_all()

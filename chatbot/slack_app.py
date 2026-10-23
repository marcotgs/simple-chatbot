"""Slack bot over Socket Mode (outbound websocket, no public URL). DMs, `/bot` and @mentions
-> core.send() on the owner's current Slack thread (`slack-dm`, then `slack-dm-2`, ... after each
`/bot new`); slash and mention replies are ephemeral (owner only).

Run standalone: python -m chatbot.slack_app. Or host it in the web process: start_in_thread().
"""

import logging
import random
import re
import threading
from collections import deque

from chatbot import config, core, db, llm, uploads
from chatbot.capabilities import linkedin
from chatbot.server import DEFAULT_USER

THREAD_ID = "slack-dm"  # first Slack session; '/bot new' moves to slack-dm-2, slack-dm-3, ...
NEW_SESSION = "New session started."
SEEN_MAX = 1000
REFUSAL = "Sorry, this bot only talks to its owner."
# Posted the moment an owner's turn starts (no LLM call), so a slow answer is not mistaken for
# a dead bot. Never stored in messages. Varied so it reads like a person, not a spinner; a native
# "is typing" status needs Slack's assistant threads (assistant:write + app config), not used here.
ACKS = (
    "On it, give me a sec.",
    "Let me take a look…",
    "Sure, one moment.",
    "Let me think about that…",
    "Give me a moment to check.",
)
ACKS_FILE = (
    "Got your file, reading it now…",
    "Thanks for the file, let me read through it.",
    "Opening your file, one sec…",
)
# Only the line core.send appends (exact prefix, end of reply) becomes a link (R6-SEC-W1).
_DRAFT_LINE = re.compile(
    rf"\n\n{re.escape(linkedin.DRAFT_LABEL)}: ({re.escape(linkedin.DRAFT_URL)}\S*)\Z"
)
_MENTION = re.compile(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*")  # leading <@BOT> in app_mention text

log = logging.getLogger("chatbot.slack")

# Slack member ID -> internal user id. Anyone not listed is refused.
USER_MAP = {config.SLACK_USER_ID: DEFAULT_USER} if config.SLACK_USER_ID else {}

# ponytail: in-memory dedupe of the last SEEN_MAX events; a restart forgets it, so a retry
# arriving across a restart can answer twice. Move to a SQLite table if that ever matters.
_seen: set[str] = set()
_seen_order: deque[str] = deque()
_seen_lock = threading.Lock()


def _first_time(key: str) -> bool:
    with _seen_lock:
        if key in _seen:
            return False
        _seen.add(key)
        _seen_order.append(key)
        if len(_seen_order) > SEEN_MAX:
            _seen.discard(_seen_order.popleft())
        return True


def handle_message(event: dict, say) -> None:
    """Bolt `message` listener. Bolt acks first and runs this on its worker pool, so a slow
    LLM call never delays the 3 s ack."""
    subtype = event.get("subtype")
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return  # channels, bot (incl. our own) messages: no reply loops
    if subtype and subtype != "file_share":
        return  # edits, deletes, joins
    ts = event.get("ts")
    key = event.get("client_msg_id") or (f"{event.get('channel', '')}:{ts}" if ts else "")
    if not key:  # no way to dedupe a retry: fail closed rather than answer twice
        log.warning("dropping Slack DM event without client_msg_id or ts")
        return
    if not _first_time(key):
        return
    user_id = USER_MAP.get(event.get("user", ""))
    if user_id is None:
        say(REFUSAL)
        return
    text = event.get("text") or ""
    has_files = subtype == "file_share" or bool(event.get("files"))
    say(random.choice(ACKS_FILE if has_files else ACKS))
    if has_files:
        notes = _ingest_files(user_id, event.get("files") or [], say)
        if not text.strip():
            return
        text += "".join(notes)
    say(_answer(user_id, text, key))


def _ingest_files(user_id: str, files: list, say) -> list[str]:
    """Store each file as a doc, ack in Slack. Returns notes telling the model the doc ids."""
    lines, notes = [], []
    for f in files:
        name = uploads.safe_name(f.get("name") or f.get("title") or "")
        try:
            doc_id, pages, chars = uploads.ingest(user_id, f, config.SLACK_BOT_TOKEN)
        except uploads.UploadError as e:
            lines.append(f"Could not use {name}: {e}.")
            continue
        except Exception:
            log.exception("file ingest failed for %s", f.get("id"))
            lines.append(f"Could not use {name}: something went wrong on my side.")
            continue
        lines.append(
            f"Got {name}: {pages} page(s), {chars} characters. Saved as document {doc_id}."
        )
        notes.append(
            f"{uploads.NOTE_PREFIX}{name} I sent is available as document {doc_id} ({pages} "
            "page(s)); for an overview use summarize_doc; to read or quote a page use the docs "
            "capability (fetch_doc).]"
        )
    say("\n".join(lines) or "That message had no files I could read.")
    return notes


def _session_key(user_id: str) -> str:
    return f"slack_session:{user_id}"


def current_thread(user_id: str) -> str:
    """The thread DMs, /bot and @mentions share (AC-1). Stored, so a restart keeps it."""
    row = (
        db.conn()
        .execute("SELECT value FROM settings WHERE key = ?", (_session_key(user_id),))
        .fetchone()
    )
    n = int(row["value"]) if row else 1
    return THREAD_ID if n <= 1 else f"{THREAD_ID}-{n}"


def new_session(user_id: str) -> str:
    """'/bot new': move the user to the next Slack thread. Global facts carry over; the old
    thread keeps its history, summary, session facts and doc listing."""
    c = db.conn()
    with c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, '2') ON CONFLICT (key)"
            " DO UPDATE SET value = CAST(value AS INTEGER) + 1",
            (_session_key(user_id),),
        )
    return current_thread(user_id)


def _answer(user_id: str, text: str, ref: str) -> str:
    """core.send on the user's current Slack thread; every failure becomes a short, leak-free
    reply."""
    try:
        return _slack_links(core.send(user_id, current_thread(user_id), text))
    except ValueError:
        return "I can only answer text messages, and that one looked empty."
    except llm.BudgetExceeded:
        return "That message is too long for my token budget. Please send a shorter one."
    except Exception:
        log.exception("core.send failed for Slack event %s", ref)
        return "Something went wrong on my side. Please try again in a moment."


def _slack_links(reply: str) -> str:
    """The LinkedIn draft line as a clickable mrkdwn link. The URL is percent-encoded except its
    one '&', which Slack wants escaped; it holds no '<', '>' or '|' that could break the link.
    Everything else is model text: '&', '<' and '>' are escaped so it cannot form a link or
    mention of its own (R7-SEC-S1)."""
    m = _DRAFT_LINE.search(reply)
    head = reply[: m.start()] if m else reply
    head = head.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not m:
        return head
    return f"{head}\n\n<{m.group(1).replace('&', '&amp;')}|{linkedin.DRAFT_LABEL}>"


def handle_command(ack, command: dict, respond) -> None:
    """`/bot <text>`. Ack within Slack's 3 s first; every reply is ephemeral (Q-10: only the
    requester sees it, so the owner's memory never shows up in a channel)."""
    ack()
    key = command.get("trigger_id") or ""
    if not key or not _first_time(f"cmd:{key}"):
        return  # no trigger_id = cannot dedupe a redelivery: fail closed
    user_id = USER_MAP.get(command.get("user_id", ""))
    text = (command.get("text") or "").strip()
    if user_id is None:
        reply = REFUSAL
    elif not text:
        reply = "Usage: /bot <your message>, or /bot new to start a new session. Only you see it."
    elif text.lower() == "new":
        new_session(user_id)
        reply = NEW_SESSION
    else:
        respond(text=random.choice(ACKS), response_type="ephemeral")
        reply = _answer(user_id, text, key)
    respond(text=reply, response_type="ephemeral")


def handle_mention(event: dict, client) -> None:
    """`@bot <text>` in a channel the bot is in. Replies with chat.postEphemeral to the sender
    only (Q-10). Ephemeral replies show in a thread only if it already has replies, so a
    top-level mention is answered in the channel."""
    if event.get("bot_id"):
        return
    ts = event.get("ts")
    key = event.get("client_msg_id") or (f"{event.get('channel', '')}:{ts}" if ts else "")
    if not key:
        log.warning("dropping Slack mention event without client_msg_id or ts")
        return
    if not _first_time(key):
        return
    sender = event.get("user", "")
    user_id = USER_MAP.get(sender)
    text = _MENTION.sub("", event.get("text") or "", count=1).strip()
    if user_id is None:
        reply = REFUSAL
    elif not text:
        reply = "Usage: @mention me followed by your message. Only you see the answer."
    else:
        client.chat_postEphemeral(
            channel=event.get("channel"),
            user=sender,
            text=random.choice(ACKS),
            thread_ts=event.get("thread_ts"),
        )
        reply = _answer(user_id, text, key)
    client.chat_postEphemeral(
        channel=event.get("channel"), user=sender, text=reply, thread_ts=event.get("thread_ts")
    )


def start() -> None:
    """Connect over Socket Mode and block forever."""
    if not (config.SLACK_BOT_TOKEN and config.SLACK_APP_TOKEN):
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set")
    from slack_bolt import App  # imported here so the web server and tests run without slack_bolt
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    db.conn()
    # Default process_before_response=False: events are acked immediately and listeners run on
    # bolt's ThreadPoolExecutor(max_workers=5), so Slack does not retry long LLM turns.
    app = App(token=config.SLACK_BOT_TOKEN)
    app.event("message")(handle_message)
    app.event("app_mention")(handle_mention)
    app.command("/bot")(handle_command)
    if not USER_MAP:
        log.warning("SLACK_USER_ID is not set: every DM will be refused")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


def start_in_thread() -> threading.Thread:
    t = threading.Thread(target=start, name="slack-socket-mode", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        start()
    except KeyboardInterrupt:
        pass

"""Slack bot over Socket Mode (outbound websocket, no public URL). DMs, `/bot` and @mentions
-> core.send() on one `slack-dm` thread; slash and mention replies are ephemeral (owner only).

Run standalone: python -m chatbot.slack_app. Or host it in the web process: start_in_thread().
"""

import logging
import re
import threading
from collections import deque

from chatbot import config, core, db, llm, uploads
from chatbot.server import DEFAULT_USER

THREAD_ID = "slack-dm"  # one DM thread per user that never ends
SEEN_MAX = 1000
REFUSAL = "Sorry, this bot only talks to its owner."
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
    if subtype == "file_share" or event.get("files"):
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
            "page(s)); read it with summarize_doc or the docs capability (fetch_doc).]"
        )
    say("\n".join(lines) or "That message had no files I could read.")
    return notes


def _answer(user_id: str, text: str, ref: str) -> str:
    """core.send on the shared slack-dm thread; every failure becomes a short, leak-free reply."""
    try:
        return core.send(user_id, THREAD_ID, text)
    except ValueError:
        return "I can only answer text messages, and that one looked empty."
    except llm.BudgetExceeded:
        return "That message is too long for my token budget. Please send a shorter one."
    except Exception:
        log.exception("core.send failed for Slack event %s", ref)
        return "Something went wrong on my side. Please try again in a moment."


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
        reply = "Usage: /bot <your message>. Only you see the answer."
    else:
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

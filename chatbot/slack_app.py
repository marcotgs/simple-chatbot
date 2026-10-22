"""Slack DM bot over Socket Mode (outbound websocket, no public URL). DMs -> core.send().

Run standalone: python -m chatbot.slack_app. Or host it in the web process: start_in_thread().
"""
import logging
import threading
from collections import deque

from chatbot import config, core, db, llm
from chatbot.server import DEFAULT_USER

THREAD_ID = "slack-dm"  # one DM thread per user that never ends
SEEN_MAX = 1000

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
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
        return  # channels, edits, deletes, joins, bot (incl. our own) messages: no reply loops
    if not _first_time(event.get("client_msg_id") or event.get("ts") or ""):
        return
    user_id = USER_MAP.get(event.get("user", ""))
    if user_id is None:
        say("Sorry, this bot only talks to its owner.")
        return
    try:
        reply = core.send(user_id, THREAD_ID, event.get("text") or "")
    except ValueError:
        reply = "I can only answer text messages, and that one looked empty."
    except llm.BudgetExceeded:
        reply = "That message is too long for my token budget. Please send a shorter one."
    except Exception:
        log.exception("core.send failed for Slack DM ts=%s", event.get("ts"))
        reply = "Something went wrong on my side. Please try again in a moment."
    say(reply)


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

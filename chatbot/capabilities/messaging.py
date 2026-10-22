"""send_message: records the message in send_log. Nothing is actually sent anywhere."""
from chatbot import db


def _contains_forgotten(text: str) -> bool:
    try:
        from chatbot.memory.forget import contains_forgotten
    except ImportError:  # T3 not landed yet
        return False
    return contains_forgotten(text)


def send_message(user_id: str, thread_id: str, to: str = "", text: str = "") -> str:
    to, text = (to or "").strip(), (text or "").strip()
    if not to or not text:
        return "error: both 'to' and 'text' are required"
    if _contains_forgotten(text):
        return ("error: message NOT sent. It contains personal data the user asked to forget. "
                "Rewrite it without that information and do not mention the forgotten value.")
    c = db.conn()
    with c:
        row_id = c.execute(
            "INSERT INTO send_log (user_id, thread_id, recipient, text) VALUES (?, ?, ?, ?)",
            (user_id, thread_id, to, text),
        ).lastrowid
    return f"ok: message #{row_id} to {to} recorded in send_log ({len(text)} chars)"


INSTRUCTIONS = """send_message(to, text) records an outgoing message (it is logged, not delivered).
- Confirm the recipient and final text with the user if either is ambiguous.
- Never include data the user asked you to forget; if the tool rejects the text, rewrite without it.
- After success, tell the user the message was recorded and to whom."""

CAPABILITY = {
    "name": "messaging",
    "one_line": "send a message to a person (logged only)",
    "instructions": INSTRUCTIONS,
    "tools": [
        ({"name": "send_message", "description": "Send (log) a message to a recipient.",
          "parameters": {"type": "object", "properties": {
              "to": {"type": "string", "description": "Recipient name or handle."},
              "text": {"type": "string", "description": "Message body."}},
              "required": ["to", "text"]}}, send_message),
    ],
}

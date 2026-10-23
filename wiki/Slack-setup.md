# Slack setup

Condensed from [`SLACK_SETUP.md`](https://github.com/marcotgs/simple-chatbot/blob/main/SLACK_SETUP.md)
(full click path and troubleshooting there). Code:
[`chatbot/slack_app.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/slack_app.py),
[`chatbot/uploads.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/uploads.py).

The bot uses **Socket Mode**: it opens an outbound websocket to Slack, so it needs no public URL.

## Create the app

1. https://api.slack.com/apps > **Create New App** > **From scratch**.
2. **Socket Mode**: enable it and generate an app-level token with scope `connections:write`. The
   `xapp-...` token is `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** > Bot Token Scopes: `chat:write`, `im:history`, `im:read`, `im:write`,
   `commands`, `app_mentions:read`, `files:read`.
4. **Event Subscriptions**: enable, subscribe to bot events `message.im` and `app_mention`. Files
   need no extra event: they arrive as `message.im` with subtype `file_share`.
5. **App Home**: turn on the Messages tab and allow users to send messages from it.
6. **Slash Commands**: create `/bot` (Socket Mode needs no Request URL).
7. **Install to Workspace**. The **Bot User OAuth Token** `xoxb-...` is `SLACK_BOT_TOKEN`.
   Reinstall after any change to scopes, events or slash commands.
8. Your own member ID (avatar > Profile > more > **Copy member ID**, looks like `U0123ABCD`) is
   `SLACK_USER_ID`.

Put the three values in `.env` (git-ignored) or the environment. Never commit them.

## Run

| Command | What runs |
| --- | --- |
| `make run` | Web server; it also starts the Slack bot in a daemon thread when `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are both set |
| `make slack` (`python -m chatbot.slack_app`) | Slack bot only |

`slack_bolt` is installed by `make install`. Without `SLACK_USER_ID` the bot logs a warning and
refuses every DM.

## What the bot answers

| Entry | Where the reply goes | Thread |
| --- | --- | --- |
| DM to the bot | The DM | current Slack thread |
| `/bot <message>` in any channel | Ephemeral: only you see it | current Slack thread |
| `@<bot> <message>` in a channel the bot is in | Ephemeral via `chat.postEphemeral`; inside a thread only if it already has replies, else in the channel | current Slack thread |
| `/bot new` | Ephemeral "New session started." (no model call) | moves to the next thread |

All three message entries call `core.send(user, slack_app.current_thread(user), text)`: `slack-dm`
at first, then `slack-dm-2`, `slack-dm-3`, ... after each `/bot new`. The counter is stored in
table `settings`, so a restart keeps it. Global facts carry over to the new thread; the old one
keeps its history, summary, session facts and doc listing. The web page lists the Slack threads
too, so it shows the same conversation. Slash and mention replies are ephemeral, so the owner's memory never shows up
for other channel members.

Handling details:

- Bolt acks each event first and runs the handler on its worker pool, so a slow model call never
  misses Slack's 3-second ack.
- Retries are deduplicated in memory by `client_msg_id` (or channel + `ts`, or `trigger_id` for
  `/bot`), keeping the last 1000 keys. An event with no key is dropped rather than risk two answers.
- Ack: after dedupe and the owner check, the bot posts `:hourglass_flowing_sand: Working on it…`
  (`Reading your file…` for a file share; ephemeral for `/bot` and mentions) before calling
  `core.send`. No model call, never stored in `messages`.
- A LinkedIn draft line in the reply becomes a clickable Slack link `Open in LinkedIn (draft)`
  (see [[Capabilities guide|Capabilities-guide]]).
- DMs from other bots (including itself), message edits, deletes and joins are ignored.
- Errors become short replies that leak nothing: empty text, over the token budget, or "Something
  went wrong on my side".

## Owner only

`USER_MAP` maps `SLACK_USER_ID` to the internal user `user-1`, the same user as the web page.
Anyone else gets "Sorry, this bot only talks to its owner." in the DM, or ephemerally for `/bot`
and mentions. One Slack account per instance: the map holds a single entry.

## File uploads (DM only)

1. Send a `.txt`, `.md`, `.csv`, `.json` or `.pdf` file to the bot in the DM.
2. The bot downloads it with the bot token. It only fetches `https://` URLs on `slack.com` or a
   subdomain and refuses any redirect off Slack, because urllib would resend the token. A Slack
   sign-in page instead of the file means `files:read` is missing.
3. The text is extracted (PDF via `pypdf`; password-protected PDFs and scans without text are
   refused) and stored as `data/uploads/<user>/upload-<slug>.md`, next to the DB.
4. The bot replies `Got <name>: <n> page(s), <chars> characters. Saved as document upload-<slug>.`
   Text sent with the file is answered right away. A note naming the document id is appended to
   that turn, and the model reads the file through `summarize_doc` or the `docs` capability.
5. Uploads are visible only to the user who sent them. Re-uploading under the same name replaces
   the file and clears its old notes.

There is no size limit unless `MAX_FILE_BYTES` is set (bytes; `0` = unlimited). See
[[Known limits|Known-limits]].

## Test checklist

1. DM `hello`, then `what did I just say?`: both answered in the DM.
2. Web page: thread `slack-dm` shows the same messages. `/bot new`, then a DM: the web page lists a
   new `slack-dm-2` thread.
3. `/invite @<bot>` in a channel, then `@<bot> what did I just say?` and `/bot hello`: both marked
   "Only visible to you".
4. DM a small `.txt` file, then ask what it says.

Live Slack behaviour (Socket Mode connection, `/bot`, mentions, file share) is covered offline by
[`tests/test_slack_app.py`](https://github.com/marcotgs/simple-chatbot/blob/main/tests/test_slack_app.py)
and the integration tests with fakes. No automated test runs against a real workspace.

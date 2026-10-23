# Slack bot setup

The bot uses Socket Mode: it opens an outbound websocket to Slack, so it needs no public URL.
Every DM from your Slack account goes to the same chat user as the web page, in one thread
`slack-dm` that never ends. DMs from anyone else get a polite refusal.

You can also talk to it from any channel it is in, with `/bot <message>` or `@<bot name> <message>`.
Those replies are **ephemeral**: only you see them (Slack labels them "Only visible to you"), so the bot's
memory of you never shows up for other channel members. They use the same `slack-dm` thread. Anyone
else who tries gets a refusal that only they can see.

Files you send the bot in the DM (`.txt`, `.md`, `.csv`, `.json`, `.pdf`) are saved as documents only you
can read through the bot. It replies with the document id, pages and characters; text sent with the file
is answered right away. Files are stored as extracted text under `data/uploads/` (next to the DB).
There is no size limit unless you set `MAX_FILE_BYTES` (bytes; `0` = unlimited).

## 1. Create the app

1. Open https://api.slack.com/apps and click **Create New App** > **From scratch**. Pick a name and your workspace.
2. **Socket Mode** (sidebar): turn on **Enable Socket Mode**. When asked for an app-level token, name it (e.g. `socket`), add the scope `connections:write`, and generate it. Copy the `xapp-...` token: this is `SLACK_APP_TOKEN`.
   (Skipped the prompt? **Basic Information** > **App-Level Tokens** > **Generate Token and Scopes**.)
3. **OAuth & Permissions** > **Bot Token Scopes**: add `chat:write`, `im:history`, `im:read`, `im:write`, `commands`, `app_mentions:read`, `files:read`.
4. **Event Subscriptions**: turn on **Enable Events**. Under **Subscribe to bot events** add `message.im` and `app_mention`. Save. (No extra event for files: a file sent in the DM arrives as a `message.im` event with subtype `file_share`.)
5. **App Home** > **Show Tabs**: turn on **Messages Tab**, and tick **Allow users to send Slash commands and messages from the messages tab** (without it Slack shows "Sending messages to this app has been turned off").
6. **Slash Commands** > **Create New Command**: command `/bot`, short description (e.g. `Ask the bot privately`), usage hint `<message>`. With Socket Mode on, Slack asks for no Request URL. Save.
7. **Install App** (or **OAuth & Permissions**) > **Install to Workspace** > Allow. Copy the **Bot User OAuth Token** `xoxb-...`: this is `SLACK_BOT_TOKEN`.
   Changed scopes, events or slash commands later? Reinstall the app (Slack shows a banner asking you to).
   Upgrading an existing DM-only app: do steps 3, 4 and 6 (new scopes, the `app_mention` event, `/bot`), then reinstall. Tokens stay the same.
   Adding file support to an existing app: add `files:read` (step 3) and reinstall; without it the download returns Slack's sign-in page and the bot says so.

## 2. Find your member ID

In Slack, click your avatar > **Profile** > the **⋮** (more) button > **Copy member ID**. It looks like `U0123ABCD`. This is `SLACK_USER_ID`.

## 3. Configure and run

Put the three values in `.env` (git-ignored; see `.env.example`) or in the real environment:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_USER_ID=U0123ABCD
```

Run the bot on its own:

```
pip install -e .            # installs slack_bolt
python -m chatbot.slack_app
```

To host it inside the web server process instead, call `chatbot.slack_app.start_in_thread()` from the server's `main()` (it runs as a daemon thread).

## 4. Test

1. In Slack, open the app (sidebar **Apps**, or search its name) > **Messages** tab.
2. Send `hello`. The reply comes back in the DM, usually within a few seconds.
3. Send `what did I just say?`: it answers from the same `slack-dm` thread.
4. In the web page, the thread `slack-dm` shows the same conversation.
5. Invite the bot to a channel: type `/invite @<bot name>` in it. Mentions only reach the bot in channels it is a member of.
6. In that channel, send `@<bot name> what did I just say?`, then `/bot hello`. Both answers are marked "Only visible to you".
   A mention inside a thread that already has replies is answered in that thread; a top-level mention is answered in the channel.
7. In the DM, send a small `.txt` file (no message). The bot replies `Got <name>: 1 page(s), N characters. Saved as document upload-<name>.`
   Then ask `what does the file I just sent say?`: the answer quotes its content.

Troubleshooting:
- No reply, nothing in the log: check `message.im` is subscribed and the app was reinstalled after adding scopes.
- Reply "this bot only talks to its owner": `SLACK_USER_ID` is not your member ID.
- `/bot` says "dispatch_failed" or nothing happens: the bot process is not running, or the `/bot` command or `commands` scope was added without reinstalling.
- Mention ignored: the bot is not in the channel (`/invite` it), or `app_mention` / `app_mentions:read` is missing.
- File reply "Slack sent a web page instead of the file": `files:read` is missing or the app was not reinstalled.
- File reply "I can read ... files only": the type is not supported (images, Office files, archives are not).
- `invalid_auth` in the log: wrong or revoked token; `SLACK_APP_TOKEN` must be the `xapp-` one.

Verification notes: the `connections:write` scope, the `xapp-`/`xoxb-` token types, the Messages Tab toggle and `message.im` needing `im:history` were checked against docs.slack.dev (2026-10-22). The exact wording of the "Allow users to send Slash commands and messages from the messages tab" checkbox and the error text in step 5 are from memory and were not found in current Slack docs.
The `/bot` and mention steps follow docs.slack.dev (`chat.postEphemeral`: bot and user must be in the channel; ephemeral thread replies show only in threads that already have replies). The `dispatch_failed` text is from memory (2026-10-22).

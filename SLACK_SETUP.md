# Slack DM bot setup

The bot uses Socket Mode: it opens an outbound websocket to Slack, so it needs no public URL.
Every DM from your Slack account goes to the same chat user as the web page, in one thread
`slack-dm` that never ends. DMs from anyone else get a polite refusal.

## 1. Create the app

1. Open https://api.slack.com/apps and click **Create New App** > **From scratch**. Pick a name and your workspace.
2. **Socket Mode** (sidebar): turn on **Enable Socket Mode**. When asked for an app-level token, name it (e.g. `socket`), add the scope `connections:write`, and generate it. Copy the `xapp-...` token: this is `SLACK_APP_TOKEN`.
   (Skipped the prompt? **Basic Information** > **App-Level Tokens** > **Generate Token and Scopes**.)
3. **OAuth & Permissions** > **Bot Token Scopes**: add `chat:write`, `im:history`, `im:read`, `im:write`.
4. **Event Subscriptions**: turn on **Enable Events**. Under **Subscribe to bot events** add `message.im`. Save.
5. **App Home** > **Show Tabs**: turn on **Messages Tab**, and tick **Allow users to send Slash commands and messages from the messages tab** (without it Slack shows "Sending messages to this app has been turned off").
6. **Install App** (or **OAuth & Permissions**) > **Install to Workspace** > Allow. Copy the **Bot User OAuth Token** `xoxb-...`: this is `SLACK_BOT_TOKEN`.
   Changed scopes later? Reinstall the app.

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

Troubleshooting:
- No reply, nothing in the log: check `message.im` is subscribed and the app was reinstalled after adding scopes.
- Reply "this bot only talks to its owner": `SLACK_USER_ID` is not your member ID.
- `invalid_auth` in the log: wrong or revoked token; `SLACK_APP_TOKEN` must be the `xapp-` one.

Verification notes: the `connections:write` scope, the `xapp-`/`xoxb-` token types, the Messages Tab toggle and `message.im` needing `im:history` were checked against docs.slack.dev (2026-10-22). The exact wording of the "Allow users to send Slash commands and messages from the messages tab" checkbox and the error text in step 5 are from memory and were not found in current Slack docs.

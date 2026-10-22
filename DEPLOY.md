# Deploy (Fly.io + CircleCI)

Optional: the take-home is evaluated locally. This puts the web page and the Slack bot on one
Fly.io machine, with SQLite and the call log on a persistent volume.

Replace every `<...>` placeholder. Never commit real values.

## Constraints

- **Exactly one machine.** SQLite lives on one Fly volume, and a volume attaches to one machine.
  Never run `fly scale count` above 1. Deploys use `--ha=false`.
- **The machine never sleeps.** The Slack bot holds an outbound Socket Mode connection in the same
  process, so `fly.toml` sets `auto_stop_machines = "off"` and `min_machines_running = 1`.
  Slack needs no public URL.
- **The web page is public.** Only `WEB_TOKEN` stands between the internet and your OpenRouter
  key. Always set it in production.
- Each deploy restarts the single machine, so expect a few seconds of downtime.

## One-time setup (about 15 minutes)

Prerequisites: a Fly.io account, `flyctl` installed (`brew install flyctl`), and a CircleCI account
connected to the GitHub repo.

1. Log in:
   ```sh
   fly auth login
   ```
2. Put your app name and region in `fly.toml` (`app`, `primary_region`). List regions with
   `fly platform regions`.
3. Create the app:
   ```sh
   fly apps create <YOUR_FLY_APP>
   ```
4. Create the volume in the same region as `primary_region` (1 GB is plenty):
   ```sh
   fly volumes create data --size 1 --region <YOUR_REGION> -a <YOUR_FLY_APP>
   ```
5. Generate a web token:
   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
6. Set the secrets. Start the line with a space so most shells keep it out of history:
   ```sh
    fly secrets set -a <YOUR_FLY_APP> \
     OPENROUTER_API_KEY=<YOUR_OPENROUTER_KEY> \
     SLACK_BOT_TOKEN=<YOUR_SLACK_BOT_TOKEN> \
     SLACK_APP_TOKEN=<YOUR_SLACK_APP_TOKEN> \
     SLACK_USER_ID=<YOUR_SLACK_USER_ID> \
     WEB_TOKEN=<YOUR_WEB_TOKEN>
   ```
   Non-secret settings (`LLM_MODEL`, `MAX_INPUT_TOKENS`, ...) live in `fly.toml` under `[env]`.
7. Do the first deploy from your machine:
   ```sh
   fly deploy --ha=false
   fly scale count 1 -a <YOUR_FLY_APP>
   ```

## CircleCI (continuous deploy from `main`)

The pipeline runs `test` on every branch and `build` (a Docker build check) on non-main branches.
On `main`, it runs `test`, then waits at `hold` for your approval, then runs `deploy`
(`flyctl deploy --remote-only`).

1. In CircleCI, set the project up from the repo (it picks up `.circleci/config.yml`).
2. Create an **app-scoped deploy token**, not a personal token. It can only deploy this one app:
   ```sh
   fly tokens create deploy -a <YOUR_FLY_APP> --expiry 2160h
   ```
3. In CircleCI: Organization Settings → Contexts → create the context `fly-deploy`. Add the
   environment variable `FLY_API_TOKEN` and paste the token. Restrict the context to a security
   group that includes only you.
4. Push to `main`. Approve the `hold` job in the CircleCI UI to deploy.

Rotate the token: create a new one, update the context, then revoke the old one
(`fly tokens list -a <YOUR_FLY_APP>`, then `fly tokens revoke <TOKEN_ID>`).

## Use it

- Web page: `https://<YOUR_FLY_APP>.fly.dev/?token=<YOUR_WEB_TOKEN>`.
  API: send the header `X-Web-Token: <YOUR_WEB_TOKEN>` instead of the query string, e.g.
  ```sh
  curl -s https://<YOUR_FLY_APP>.fly.dev/send -H "X-Web-Token: <YOUR_WEB_TOKEN>" \
    -H 'Content-Type: application/json' -d '{"thread_id":"t1","text":"hi"}'
  ```
- Slack: DM the bot. Nothing else to configure on Fly.

## Operate

- Live logs: `fly logs -a <YOUR_FLY_APP>`
- Status: `fly status -a <YOUR_FLY_APP>`
- Per-call token log on the volume:
  `fly ssh console -a <YOUR_FLY_APP> -C "tail -n 20 /data/logs/calls.jsonl"`
- Change a secret (this restarts the machine): `fly secrets set -a <YOUR_FLY_APP> WEB_TOKEN=<NEW_VALUE>`

## Rollback

1. List releases and their images:
   ```sh
   fly releases -a <YOUR_FLY_APP> --image
   ```
2. Redeploy the last good image:
   ```sh
   fly deploy -a <YOUR_FLY_APP> --ha=false --image <PREVIOUS_IMAGE_REF>
   ```

Data rollback: Fly takes daily volume snapshots. Find them with
`fly volumes list -a <YOUR_FLY_APP>` and `fly volumes snapshots list <VOLUME_ID>`. To restore,
create a new volume from a snapshot (`fly volumes create data --snapshot-id <SNAPSHOT_ID>
--region <YOUR_REGION> -a <YOUR_FLY_APP>`), then destroy the old volume. Destroying a volume is
irreversible.

## Tear down

`fly apps destroy <YOUR_FLY_APP>` deletes the app, the volume and all data. This is irreversible.

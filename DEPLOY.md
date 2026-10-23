# Deploy (pipeline: CircleCI -> Fly.io)

Optional: the take-home is evaluated locally. This puts the web page and the Slack bot on one
Fly.io machine, with SQLite and the call log on a persistent volume.

Every provisioning and deploy step runs in CircleCI. You only do the setup below that a pipeline
cannot do for you (accounts, tokens, contexts). Replace every `<...>` placeholder. Never commit real
values.

## Constraints

- **Exactly one machine.** SQLite lives on one Fly volume, and a volume attaches to one machine.
  Never run `fly scale count` above 1. Deploys use `--ha=false`. The bootstrap job refuses to
  continue if it finds more than one volume.
- **The machine never sleeps.** The Slack bot holds an outbound Socket Mode connection in the same
  process, so `fly.toml` sets `auto_stop_machines = "off"` and `min_machines_running = 1`.
  Slack needs no public URL.
- **The web page is public.** Only `WEB_TOKEN` stands between the internet and your OpenRouter
  key.
- Each deploy restarts the single machine, so expect a few seconds of downtime.

## How the pipeline works

| Job | Runs on | Gate | Context | Does |
| --- | --- | --- | --- | --- |
| `test` | every branch | none | none | gitleaks secret scan (all commits), install locked deps with hashes, lock-drift check, ruff, unit tests |
| `build` | non-main branches | `test` | none | Docker build check, not pushed |
| `bootstrap` | `main` | approve `hold-bootstrap` | `fly-bootstrap`, `app-secrets` | `deploy/bootstrap.sh`: create app if absent, create the one volume if absent, set snapshot retention, stage secrets |
| `deploy` | `main` | approve `hold` | `fly-deploy` | `deploy/deploy.sh`: check bootstrap ran, `fly deploy --remote-only --ha=false`, smoke `GET /healthz` |
| `rollback` | `main`, pipeline parameter `rollback_image` set | approve `hold-rollback` | `fly-deploy` | redeploy an earlier image, smoke check |

`fly.toml` is the single source of truth. The jobs read `app`, `primary_region` and `[mounts]`
(`source`, `initial_size`, `snapshot_retention`) from it. Nothing about the app is hardcoded in
the pipeline. One exception: `bootstrap` runs with an org token, so it also requires `FLY_APP` in
the `fly-bootstrap` context and stops before any Fly call when `fly.toml` `app` differs. A commit
that points `fly.toml` at another app in the org cannot steer the org token there.

Both scripts are idempotent: re-running `bootstrap` creates nothing that already exists. It
re-applies the snapshot retention and re-stages the secrets.

### Why two Fly tokens

- `fly-bootstrap` holds an **org token**. Only an org token can create an app. It can touch every
  app in the org, so keep its expiry short and revoke it when you are done.
- `fly-deploy` holds an **app-scoped deploy token**. It can only touch this one app. This token is
  used on every deploy, so it is the one that stays around.

The deploy token cannot exist before the app exists, so you create it after the first bootstrap.

## 1. Manual prerequisites (one time, about 20 minutes)

You need a Fly.io account, `flyctl` on your machine (`brew install flyctl`), a CircleCI account,
and the repo on GitHub.

1. Edit `fly.toml`: set `app` to `<YOUR_FLY_APP>`, `primary_region` to `<YOUR_REGION>`
   (`fly platform regions` lists them), and `ALLOWED_HOSTS` to `<YOUR_FLY_APP>.fly.dev`. Commit.
   The jobs fail with a clear message while any `<YOUR_...>` placeholder is left.
2. Log in and create a short-lived org token:
   ```sh
   fly auth login
   fly orgs list
   fly tokens create org -o <YOUR_FLY_ORG> -x 24h
   ```
   The default expiry is 20 years: always pass `-x`. Copy the token straight into step 4. Do not
   save it anywhere else.
3. In CircleCI, connect the repo: Projects → Set Up Project → use the existing
   `.circleci/config.yml`.
4. In CircleCI, go to Organization Settings → Contexts and create three contexts:
   - `fly-bootstrap`: `FLY_API_TOKEN` = the org token from step 2, `FLY_ORG` = `<YOUR_FLY_ORG>`,
     `FLY_APP` = `<YOUR_FLY_APP>` (must equal `app` in `fly.toml`).
   - `app-secrets`: all of these are required, and bootstrap lists any that are missing:
     - `OPENROUTER_API_KEY`
     - `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_USER_ID` (see `SLACK_SETUP.md`)
     - `WEB_TOKEN` and `FORGET_SECRET`. Generate each one with
       `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
       `FORGET_SECRET` keys the HMAC on forget tombstones. Set it before the first forget and
       never change it: rotating it makes existing tombstones stop matching.
   - `fly-deploy`: leave it empty for now. It gets filled in step 7.

   Values must be single-line. Never put `FLY_API_TOKEN` in `app-secrets`.
5. Lock the contexts and `main` down. Whoever can change `main` controls what the Fly jobs run:
   - GitHub → Settings → Branches (or Rules → Rulesets): protect `main`. Require a pull request
     with at least one approval, block force pushes and deletions, and do not allow bypass. Turn on
     **Secret scanning** and **Push protection** under Settings → Code security (free on public
     repos; private repos need GitHub Secret Protection).
   - On each of the three contexts, add an expression restriction (org admin only):
     ```sh
     circleci context restriction create fly-bootstrap --type expression \
       --value 'pipeline.git.branch == "main" and not job.ssh.enabled' --org <YOUR_CIRCLECI_ORG_SLUG>
     ```
     Repeat for `app-secrets` and `fly-deploy`. A branch that edits `.circleci/config.yml` to
     attach a context then gets no secrets, and "Rerun job with SSH" cannot open a shell holding
     them.
   - Also add a security-group restriction to a GitHub team that holds only you.
   - In Project Settings → Advanced, keep **Pass secrets to builds from forked pull requests**
     OFF (the default). Fork PRs then build as `pull/<N>` with no context. The context jobs only
     run on `main`.
   - Keep the CircleCI project private (it follows the GitHub repo's visibility). Job logs print
     `fly status` and flyctl output; on a public project anyone can read them.
6. Push to `main`. Approve `hold-bootstrap` in the CircleCI UI and wait for `bootstrap` to pass.
   Leave `hold` unapproved: `deploy` would fail because `fly-deploy` is still empty.
7. Create the app-scoped deploy token and put it in the `fly-deploy` context as `FLY_API_TOKEN`:
   ```sh
   fly tokens create deploy -a <YOUR_FLY_APP> -x 2160h
   ```
8. Optional, recommended: revoke the org token now (`fly tokens list`, then
   `fly tokens revoke <TOKEN_ID>`) and delete it from `fly-bootstrap`. Create a new short-lived
   one when you next need bootstrap (see "Rotate a secret").
9. Approve `hold` on the same pipeline (or on a re-run of it). `deploy` builds remotely, deploys
   one machine, and checks `https://<YOUR_FLY_APP>.fly.dev/healthz` for up to 2 minutes.

Non-secret settings stay in `fly.toml` under `[env]`:
- `ALLOWED_HOSTS` is **required**. The server rejects any request whose `Host` header is not
  loopback or in this list (comma-separated, no scheme; add a custom domain here if you attach one).
- `LLM_MODEL` defaults to `openai/gpt-4.1-mini`. Other models can fail with
  `404 ... 0 endpoints` when the OpenRouter account has the zero-data-retention guardrail on and
  tools are attached.
- `MAX_INPUT_TOKENS`, `TARGET_INPUT_TOKENS`: token budget knobs.
- `GATE_MODEL`, `GATE_THRESHOLD`: the salience gate, see below.
- `JUDGE_MODEL` (unset by default, falls back to `LLM_MODEL`): model for fact extraction and the
  forget resolver. It must support OpenRouter chat/completions with JSON replies. Never set it to a
  `typesafe/jev-*` model: those are decisions-only, every call fails with HTTP 400, and memory
  silently stops storing facts.

### Salience gate

Before each fact-extraction call, a cheap decisions model scores "is anything in this turn worth
long-term memory?" (0..1). Below `GATE_THRESHOLD` the extractor call is skipped. It is
**fail-open**: on any error, timeout or odd reply the extractor runs anyway, so an outage costs
money, never facts.

- Defaults (in code and in `fly.toml`): `GATE_MODEL = "typesafe/jev-1.14"`,
  `GATE_THRESHOLD = "0.3"`. It uses the same `OPENROUTER_API_KEY`; no new secret.
- Turn it off: set `GATE_MODEL = ""` in `fly.toml` `[env]` and deploy.
- Cost: on a 500-turn load it skipped 84% of turns, about -72% cost ($0.24 -> $0.07) and about
  half the wall time. Turns that pass the gate pay about +0.5s p50 latency.
- Risk: it calls the **alpha** OpenRouter endpoint `/api/alpha/decisions`, and the model is a dated
  build (`jev-1.14-20261014` when tuned). A silent model update can shift scores. After any
  change to `GATE_MODEL`, `GATE_THRESHOLD`, or a provider-side model update, run the live guard
  test from your machine (about 60 calls, under $0.002; reads `OPENROUTER_API_KEY` from `.env`):
  ```sh
  RUN_LIVE_TESTS=1 python3 -m unittest tests.test_gate_quality -v
  ```
  It must report recall 1.00. If it does not, set `GATE_MODEL = ""` until it is re-tuned.
- Data: the text of each user turn goes to OpenRouter, then to the TypeSafe Jev provider. The
  zero-data-retention guardrail accepted the decisions endpoint (checked 2026-10-22); keep that
  guardrail on (see "Forget and backups").

## 2. Order of runs

1. Push to `main`. `test` runs.
2. Approve `hold-bootstrap`, but only the first time or when a secret or the volume settings
   change. Wait for `bootstrap` to pass.
3. Approve `hold`. `deploy` runs, then the smoke check. If `/healthz` does not return 200 within
   2 minutes, the job prints `fly status`, then fails. It does not copy app logs into CircleCI
   (they can hold tracebacks and provider error bodies): read them with `fly logs -a <YOUR_FLY_APP>`.

Both Fly jobs share one CircleCI `serial-group`, so two of them never change Fly at the same time.
Secrets that bootstrap stages go live on the next deploy.

## 3. Rotate a secret

1. Change the value in the `app-secrets` context.
2. If you revoked the org token, put a new short-lived one into `fly-bootstrap`
   (`fly tokens create org -o <YOUR_FLY_ORG> -x 1h`).
3. Re-run the latest `main` pipeline. Approve `hold-bootstrap`, wait for it to pass, then
   approve `hold`.
4. Revoke the org token again.

Rotate the deploy token: create a new one (step 7), update `fly-deploy`, then revoke the old one
(`fly tokens list -a <YOUR_FLY_APP>`, then `fly tokens revoke <TOKEN_ID>`).

Blast radius: the deploy token can deploy, read and set this app's secrets, open `fly ssh console`,
and read the `/data` volume. The org token can do the same for every app in the org and can create
new apps. Treat either token leaking as a full data breach and revoke it at once.

## 4. Rollback

1. Find the image to go back to. The last good release's image is in the `deploy` log of that
   release, or run `fly releases -a <YOUR_FLY_APP> --image` on your machine.
2. In CircleCI: Project → Trigger Pipeline → branch `main` → add the parameter
   `rollback_image` (string) = `registry.fly.io/<YOUR_FLY_APP>:<TAG>`.
3. Approve `hold-rollback`. The job prints the last releases, redeploys that image with
   `--ha=false`, and runs the same smoke check. Only images from this app's own registry repo are
   accepted.

Data is not rolled back: the volume stays as it is. To restore data, see the manual fallback
appendix, and read the next section first.

## Forget and backups

`forget` scrubs the live database on the volume. It does **not** reach copies outside it:

- **Fly volume snapshots.** Fly snapshots the volume daily and keeps each snapshot for the
  retention period. The Fly default is 5 days. The bootstrap job sets it to `snapshot_retention`
  from `fly.toml` (1 day, the Fly minimum) on every run, when it creates the volume and with
  `fly volumes update --snapshot-retention` after that. Until a snapshot expires, it still holds
  the forgotten value in plaintext. Restoring a snapshot brings the value back and drops every
  tombstone created after the snapshot, so redaction stops working for it too. **After any
  restore, run forget again for everything forgotten since the snapshot date.**
- **OpenRouter.** Prompts that held the value were sent to the LLM provider. OpenRouter and the
  upstream provider may retain them according to their own policy and your account's logging
  setting. Keep prompt logging off and the zero-data-retention guardrail on at
  `https://openrouter.ai/settings/privacy`.

You cannot delete snapshots after a forget: flyctl has no snapshot delete command
(`fly volumes snapshots` only has `create` and `list`). Snapshots expire at the end of their
retention period. To keep no backups at all, turn scheduled snapshots off by hand (existing
snapshots still live until they expire):
```sh
fly volumes update <VOLUME_ID> -a <YOUR_FLY_APP> --scheduled-snapshots=false
```
Trade-off: with retention 1 or snapshots off, a bad deploy that corrupts data has at most one day
of backup, or none.

## Use it

- Web page: `https://<YOUR_FLY_APP>.fly.dev/?token=<YOUR_WEB_TOKEN>`.
  API: send the header `X-Web-Token: <YOUR_WEB_TOKEN>` instead of the query string, e.g.
  ```sh
  curl -s https://<YOUR_FLY_APP>.fly.dev/send -H "X-Web-Token: <YOUR_WEB_TOKEN>" \
    -H 'Content-Type: application/json' -d '{"thread_id":"t1","text":"hi"}'
  ```
- Slack: DM the bot. Nothing else to configure on Fly.

## Operate (read-only, from your machine)

- Live logs: `fly logs -a <YOUR_FLY_APP>`
- Status: `fly status -a <YOUR_FLY_APP>`
- Per-call token log on the volume:
  `fly ssh console -a <YOUR_FLY_APP> -C "tail -n 20 /data/logs/calls.jsonl"`

## Dependencies

`pyproject.toml` declares ranges. Two hashed lockfiles pin every package, transitive ones included:

- `requirements.lock`: runtime deps. The `Dockerfile` installs it with
  `pip install --require-hashes --no-deps`.
- `requirements-dev.lock`: the same plus `ruff`. The CircleCI `test` job installs it the same way.

After any change to `dependencies` or the `dev` extra in `pyproject.toml`, regenerate both and
commit them with the change (needs [uv](https://docs.astral.sh/uv/)):
```sh
uv pip compile pyproject.toml --generate-hashes --universal --python-version 3.12 --no-header -o requirements.lock
uv pip compile pyproject.toml --extra dev --generate-hashes --universal --python-version 3.12 --no-header -o requirements-dev.lock
```
Add `--upgrade` to pick up new releases. The `test` job fails with "requirements locks are stale"
when `pyproject.toml` asks for a version the locks do not hold. Local development can keep using
`pip install -e ".[dev]"`.

## Known limits

- The smoke check only proves `/healthz` answers. It does not prove Slack connected or that the
  OpenRouter key works.

## Appendix: manual fallback

Use this only when CircleCI is down. These commands do what the jobs do.

```sh
fly apps create <YOUR_FLY_APP> --org <YOUR_FLY_ORG>
fly volumes create data -a <YOUR_FLY_APP> --region <YOUR_REGION> --size 1 --snapshot-retention 1 --yes
fly volumes list -a <YOUR_FLY_APP>
fly volumes update <VOLUME_ID> -a <YOUR_FLY_APP> --snapshot-retention 1
fly secrets import -a <YOUR_FLY_APP> --stage < <FILE_WITH_NAME=VALUE_LINES>   # delete the file afterwards
fly deploy -a <YOUR_FLY_APP> --remote-only --ha=false
fly deploy -a <YOUR_FLY_APP> --ha=false --image <PREVIOUS_IMAGE_REF>          # rollback
```

Data restore: find snapshots with `fly volumes snapshots list <VOLUME_ID>`. Create a new volume
from one (`fly volumes create data --snapshot-id <SNAPSHOT_ID> --region <YOUR_REGION> -a
<YOUR_FLY_APP>`), then destroy the old volume so only one is left. Destroying a volume is
irreversible. Read "Forget and backups" before you restore.

## Tear down

`fly apps destroy <YOUR_FLY_APP>` deletes the app, the volume and all data. This is irreversible.
Then delete the three CircleCI contexts and revoke both tokens.

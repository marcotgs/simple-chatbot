#!/usr/bin/env bash
# Idempotent Fly.io provisioning, run by the CircleCI `bootstrap` job. Safe to re-run:
#   1. create the app if it does not exist      (needs FLY_ORG + an org-scoped token)
#   2. create the ONE volume if it does not exist (SQLite => one machine => one volume)
#   3. apply snapshot retention from fly.toml to that volume
#   4. stage app secrets from CircleCI context env vars (applied by the next deploy)
# Env: FLY_API_TOKEN + FLY_APP (context fly-bootstrap; FLY_APP must equal fly.toml app),
#      FLY_ORG (only needed to create the app),
#      the secrets below (context app-secrets). Never add `set -x`.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=deploy/common.sh
source deploy/common.sh

# The org token can touch every app in the org, and fly.toml is editable by anyone who can land a
# commit on main. The target app is pinned in context fly-bootstrap, not taken from the repo alone.
: "${FLY_APP:?FLY_APP missing: add the Fly app name to context fly-bootstrap (must equal fly.toml app)}"
[ "$APP" = "$FLY_APP" ] || die "fly.toml app '$APP' != context fly-bootstrap FLY_APP '$FLY_APP'. \
Refusing to run with the org token. Fix fly.toml or the context on purpose."

REQUIRED_SECRETS=(OPENROUTER_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_USER_ID WEB_TOKEN FORGET_SECRET)

# Fail fast before touching Fly: list every missing or malformed secret by NAME only.
missing=()
for name in "${REQUIRED_SECRETS[@]}"; do
  value="${!name:-}"
  if [ -z "$value" ] || [[ "$value" == *$'\n'* ]]; then missing+=("$name"); fi
done
unset value
if [ "${#missing[@]}" -gt 0 ]; then
  die "missing or multi-line in context app-secrets: ${missing[*]}"
fi

echo "App: $APP  region: $REGION  volume: $MOUNT_SOURCE (${VOLUME_SIZE_GB}GB, snapshots ${SNAPSHOT_RETENTION}d)"

# 1. App.
if status_out="$("$FLY" status -a "$APP" 2>&1)"; then
  echo "App $APP exists."
elif grep -qiE 'could not find app|not found' <<<"$status_out"; then
  : "${FLY_ORG:?FLY_ORG missing: add the Fly org slug to context fly-bootstrap (needed to create the app)}"
  echo "Creating app $APP in org $FLY_ORG."
  "$FLY" apps create "$APP" --org "$FLY_ORG"
else
  echo "$status_out" >&2
  die "could not check app $APP (token scope or Fly API error above)."
fi

# 2. Volume: exactly one.
load_volume_ids
if [ "${#VOLUME_IDS[@]}" -eq 0 ]; then
  echo "Creating volume $MOUNT_SOURCE in $REGION."
  "$FLY" volumes create "$MOUNT_SOURCE" -a "$APP" --region "$REGION" --size "$VOLUME_SIZE_GB" \
    --snapshot-retention "$SNAPSHOT_RETENTION" --yes
  load_volume_ids
fi
# Also catches two concurrent bootstraps that both created a volume.
[ "${#VOLUME_IDS[@]}" -eq 1 ] || die "expected 1 volume named $MOUNT_SOURCE, found ${#VOLUME_IDS[@]} \
(${VOLUME_IDS[*]}). SQLite needs exactly one: destroy the EMPTY extra by hand (fly volumes destroy <ID>; \
check which one the machine mounts first), then re-run bootstrap."
echo "Volume $MOUNT_SOURCE: ${VOLUME_IDS[0]}"

# 3. Snapshot retention (create sets it; update keeps an existing volume in line with fly.toml).
"$FLY" volumes update "${VOLUME_IDS[0]}" -a "$APP" --snapshot-retention "$SNAPSHOT_RETENTION"

# 4. Secrets: NAME=VALUE on stdin via the printf builtin, so values never reach argv or the log.
# --stage: no restart here; the deploy job rolls them out.
for name in "${REQUIRED_SECRETS[@]}"; do
  printf '%s=%s\n' "$name" "${!name}"
done | "$FLY" secrets import -a "$APP" --stage >/dev/null
echo "Staged secrets: ${REQUIRED_SECRETS[*]}. Approve 'hold' to deploy them."

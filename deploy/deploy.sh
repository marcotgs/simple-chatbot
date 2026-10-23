#!/usr/bin/env bash
# Deploy + smoke check, run by the CircleCI `deploy` and `rollback` jobs.
# Usage: deploy/deploy.sh                 build remotely from this commit and deploy
#        deploy/deploy.sh <IMAGE_REF>     redeploy an existing image (rollback)
# Env: FLY_API_TOKEN (app-scoped deploy token, context fly-deploy). Never add `set -x`.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=deploy/common.sh
source deploy/common.sh

image="${1:-}"

# Bootstrap must have run once: the app and its single volume must exist.
if ! load_volume_ids || [ "${#VOLUME_IDS[@]}" -ne 1 ]; then
  die "app $APP or its single volume '$MOUNT_SOURCE' is missing (or there are several), or the \
fly-deploy token is invalid (see the flyctl error above). Approve 'hold-bootstrap' on a main \
pipeline, let 'bootstrap' finish, then re-run this job."
fi

if [ -n "$image" ]; then
  "$FLY" releases -a "$APP" --image | head -n 15 || true
  # Only images from this app's own Fly registry repo.
  [[ "$image" =~ ^registry\.fly\.io/${APP}(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$ ]] \
    || die "image must be registry.fly.io/$APP:<tag> or @sha256:<digest> (see 'fly releases --image' output above)."
  "$FLY" deploy -a "$APP" --ha=false --image "$image"
else
  "$FLY" deploy -a "$APP" --remote-only --ha=false
fi

# Smoke: /healthz must return 200 within 2 minutes. It is unauthenticated and returns no data.
url="https://${APP}.fly.dev/healthz"
deadline=$((SECONDS + 120))
until curl -fsS -o /dev/null --max-time 5 "$url"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Smoke check FAILED: $url did not return 200 within 120s." >&2
    "$FLY" status -a "$APP" || true
    # App logs are not copied into the CI log (they can hold tracebacks and provider error bodies).
    echo "Read the app logs from your machine: fly logs -a $APP" >&2
    exit 1
  fi
  sleep 5
done
echo "Smoke check OK: $url -> 200"
"$FLY" status -a "$APP"

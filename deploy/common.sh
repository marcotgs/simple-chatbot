#!/usr/bin/env bash
# Sourced by deploy/*.sh. Reads fly.toml (single source of truth) and defines helpers.
# Never enable `set -x` in these scripts: they handle tokens and secrets.
set -euo pipefail

FLY="${FLY:-$HOME/bin/flyctl}"
FLY_TOML="${FLY_TOML:-fly.toml}"

die() { echo "ERROR: $*" >&2; exit 1; }

# toml_get <section> <key>: string value of <key> in [section] ("" = top level, "vm" = [[vm]]).
# ponytail: line-based awk, enough for this flat fly.toml; values must not contain '#'.
toml_get() {
  awk -v want="$1" -v key="$2" '
    /^[[:space:]]*\[/ { sec = $0; gsub(/^[[:space:]]*\[+|\]+.*$/, "", sec); next }
    sec == want && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]*#.*$/, ""); gsub(/^"|"$/, ""); print; exit
    }' "$FLY_TOML"
}

[ -f "$FLY_TOML" ] || die "$FLY_TOML not found (run from the repo root)."
if grep -q '<YOUR_' "$FLY_TOML"; then
  die "$FLY_TOML still has <YOUR_...> placeholders. Set app, primary_region and ALLOWED_HOSTS, commit, push."
fi
: "${FLY_API_TOKEN:?FLY_API_TOKEN missing: attach the Fly context (fly-bootstrap or fly-deploy) to this job}"
export FLY_API_TOKEN

APP="$(toml_get "" app)"
REGION="$(toml_get "" primary_region)"
MOUNT_SOURCE="$(toml_get mounts source)"
VOLUME_SIZE_GB="$(toml_get mounts initial_size | tr -dc '0-9')"
SNAPSHOT_RETENTION="$(toml_get mounts snapshot_retention)"

[[ "$APP" =~ ^[a-z0-9][a-z0-9-]{1,61}$ ]] || die "fly.toml app '$APP' is not a valid Fly app name."
[[ "$REGION" =~ ^[a-z]{3}$ ]] || die "fly.toml primary_region '$REGION' is not a 3-letter Fly region."
[[ "$MOUNT_SOURCE" =~ ^[a-z0-9_]+$ ]] || die "fly.toml [mounts] source '$MOUNT_SOURCE' is invalid."
[[ "$VOLUME_SIZE_GB" =~ ^[0-9]+$ ]] || die "fly.toml [mounts] initial_size must look like \"1gb\"."
[[ "$SNAPSHOT_RETENTION" =~ ^[0-9]+$ ]] || die "fly.toml [mounts] snapshot_retention must be a number of days."

# load_volume_ids: sets array VOLUME_IDS to this app's volumes named $MOUNT_SOURCE.
# Returns non-zero (and set -e exits) if the listing fails, e.g. the app does not exist.
# Field names differ across flyctl versions (id/ID, name/Name): accept both.
# shellcheck disable=SC2034  # VOLUME_IDS is read by the calling scripts
load_volume_ids() {
  local out
  out="$("$FLY" volumes list -a "$APP" --json \
    | jq -r --arg n "$MOUNT_SOURCE" '.[] | select((.name // .Name) == $n) | (.id // .ID)')" || return 1
  VOLUME_IDS=()
  if [ -n "$out" ]; then mapfile -t VOLUME_IDS <<<"$out"; fi
}

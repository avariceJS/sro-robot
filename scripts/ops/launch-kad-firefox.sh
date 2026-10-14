#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
firefox_bin="${SRO_FIREFOX_BIN:-firefox}"
service_url="${SRO_LOCAL_APP_URL:-http://127.0.0.1:8000}"
current_user="$(id -un)"
user_home="$(getent passwd "$current_user" | cut -d: -f6)"
profile_dir="${SRO_KAD_PROFILE_DIR:-$user_home/.local/share/sro-robot/kad-firefox}"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Start Firefox as the private application operator, not as root." >&2
  exit 2
fi
if [[ -z "$user_home" || ! -d "$user_home" ]]; then
  echo "Could not determine the current operator's home directory." >&2
  exit 2
fi
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  echo "An interactive X11/Wayland session is required; this launcher does not use headless Firefox." >&2
  exit 2
fi
case "$service_url" in
  http://127.0.0.1:8000|http://localhost:8000) ;;
  *) echo "SRO_LOCAL_APP_URL must point to the loopback application on port 8000." >&2; exit 2 ;;
esac
if [[ ! -f "$repo_root/browser-extension/kad-helper/manifest.json" ]]; then
  echo "The KAD helper manifest is missing from this checkout." >&2
  exit 2
fi
if ! command -v "$firefox_bin" >/dev/null 2>&1; then
  echo "Firefox was not found; set SRO_FIREFOX_BIN to the installed binary." >&2
  exit 2
fi
if ! command -v curl >/dev/null 2>&1 || ! curl --fail --silent --show-error "$service_url/healthz" >/dev/null; then
  echo "The loopback SRO Robot service is not responding at $service_url." >&2
  exit 2
fi

mkdir -p "$(dirname "$profile_dir")"
if [[ -e "$profile_dir" ]]; then
  owner_uid="$(stat -c '%u' "$profile_dir")"
  if [[ "$owner_uid" != "$(id -u)" ]]; then
    echo "The Firefox profile directory belongs to another account; refusing to open it." >&2
    exit 2
  fi
else
  mkdir "$profile_dir"
fi
chmod 700 "$profile_dir"

echo "Opening Firefox for $current_user with private profile $profile_dir."
echo "In Firefox, load $repo_root/browser-extension/kad-helper/manifest.json from about:debugging."
echo "Temporary add-ons must be loaded again after Firefox exits."
exec "$firefox_bin" --no-remote --profile "$profile_dir" \
  "$service_url/" "about:debugging#/runtime/this-firefox"

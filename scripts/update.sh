#!/bin/bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:---apply}"
if [[ "$EUID" -eq 0 || "$#" -gt 1 || ( "$MODE" != --apply && "$MODE" != --check && "$MODE" != --recover && "$MODE" != --cleanup ) ]]; then
  echo '以普通用户运行 bash scripts/update.sh [--apply|--check|--recover|--cleanup]。升级前请断开 VPN 并退出 App。' >&2
  exit 1
fi

if [[ -f "$ROOT/RELEASE.json" && ( "$MODE" == --apply || "$MODE" == --check ) ]]; then
  /usr/bin/python3 "$ROOT/scripts/release.py" --verify "$ROOT"
fi

# Stage only release inputs, so administrator tools need no external-volume access.
STAGE="$(mktemp -d /private/tmp/snvpn-update.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/scripts" "$STAGE/service" "$STAGE/dist"
for name in install.py update.py; do
  cp "$ROOT/scripts/$name" "$STAGE/scripts/$name"
done
if [[ "$MODE" != --recover && "$MODE" != --cleanup ]]; then
  for name in control.py connect.py setup.py vpnc-script; do
    cp "$ROOT/service/$name" "$STAGE/service/$name"
  done
  /usr/bin/ditto "$ROOT/dist/SN VPN.app" "$STAGE/dist/SN VPN.app"
  /usr/bin/ditto "$ROOT/dist/service" "$STAGE/dist/service"
fi
/usr/bin/osascript - "$STAGE" "$MODE" "$(id -u)" <<'APPLESCRIPT'
on run argv
  set updater to (item 1 of argv) & "/scripts/update.py"
  set command to "SUDO_UID=" & quoted form of (item 3 of argv) & " /usr/bin/python3 -IB " & quoted form of updater & " " & quoted form of (item 2 of argv) & " " & quoted form of (item 3 of argv)
  do shell script command with administrator privileges
end run
APPLESCRIPT
if [[ "$MODE" == --apply ]]; then
  /usr/bin/open "/Applications/SN VPN.app"
fi

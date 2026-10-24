#!/bin/bash
set -euo pipefail
umask 022
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=2.10.0
SHA256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
mkdir -p "$ROOT/dist"
if [[ -f "$ROOT/dist/sparkle/.archive-sha256" ]] && [[ "$(cat "$ROOT/dist/sparkle/.archive-sha256")" == "$SHA256" ]] && [[ -d "$ROOT/dist/sparkle/Sparkle.framework" ]]; then
  echo "Sparkle $VERSION is prepared."
  exit 0
fi
STAGE="$(mktemp -d "$ROOT/dist/.sparkle-prepare.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
URL="https://github.com/sparkle-project/Sparkle/releases/download/$VERSION/Sparkle-$VERSION.tar.xz"
curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 180 "$URL" -o "$STAGE/Sparkle.tar.xz"
ACTUAL="$(shasum -a 256 "$STAGE/Sparkle.tar.xz" | cut -d ' ' -f 1)"
[[ "$ACTUAL" == "$SHA256" ]] || { echo 'Sparkle archive digest mismatch.' >&2; exit 1; }
mkdir "$STAGE/sdk"
tar -xJf "$STAGE/Sparkle.tar.xz" -C "$STAGE/sdk"
[[ -d "$STAGE/sdk/Sparkle.framework" ]] || { echo 'Sparkle framework missing.' >&2; exit 1; }
printf '%s\n' "$SHA256" > "$STAGE/sdk/.archive-sha256"
if [[ -e "$ROOT/dist/sparkle" ]]; then mv "$ROOT/dist/sparkle" "$STAGE/previous"; fi
if ! mv "$STAGE/sdk" "$ROOT/dist/sparkle"; then
  [[ ! -e "$STAGE/previous" ]] || mv "$STAGE/previous" "$ROOT/dist/sparkle"
  exit 1
fi
echo "Prepared Sparkle $VERSION in dist/sparkle."

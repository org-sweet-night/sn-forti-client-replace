#!/bin/bash
set -euo pipefail
umask 022
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/dist/SN VPN.app"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources" "$CONTENTS/Library/HelperTools" "$ROOT/dist"
BUILD_LOCK="$ROOT/dist/.build-lock"
SWIFT_TARGET="arm64-apple-macosx15.0"
if ! mkdir "$BUILD_LOCK" 2>/dev/null; then
  echo "Another build is already using $ROOT/dist" >&2
  exit 1
fi
trap 'rmdir "$BUILD_LOCK"' EXIT
VERSION_SWIFT="$ROOT/dist/BuildVersion.swift"
/usr/bin/python3 "$HERE/version.py" --self-test
/usr/bin/python3 "$HERE/version.py" "$VERSION_SWIFT"
HELPER="$CONTENTS/Library/HelperTools/local.snvpn.helper"
swiftc -O -target "$SWIFT_TARGET" -framework Foundation "$HERE/VPNHelperProtocol.swift" "$HERE/VPNHelper.swift" -o "$HELPER"
"$HELPER" --self-test
/usr/bin/codesign --force --options runtime --identifier local.snvpn.helper --sign - "$HELPER"
/usr/bin/python3 - "$HELPER" "$CONTENTS/Resources/helper-requirement.txt" <<'PY'
import re, subprocess, sys
from pathlib import Path
result = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', sys.argv[1]], capture_output=True, text=True, check=True)
digest = re.search(r'^CDHash=([0-9a-f]{40})$', result.stderr, re.M)
assert digest
Path(sys.argv[2]).write_text('identifier "local.snvpn.helper" and cdhash H"' + digest[1] + '"')
PY
swiftc -O -target "$SWIFT_TARGET" -framework AppKit -framework Foundation -framework ServiceManagement "$HERE/VPNHelperProtocol.swift" "$HERE/VPNHelperClient.swift" "$VERSION_SWIFT" "$HERE/SNVPN.swift" -o "$CONTENTS/MacOS/SN VPN"
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SN VPN</string>
<key>CFBundleIdentifier</key><string>local.snvpn.desktop</string>
<key>CFBundleName</key><string>SN VPN</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$(sed -n 's/.*marketing = "\([^"]*\)".*/\1/p' "$VERSION_SWIFT")</string>
<key>CFBundleVersion</key><string>$(sed -n 's/.*build = "\([^"]*\)".*/\1/p' "$VERSION_SWIFT")</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>LSUIElement</key><true/>
<key>CFBundleIconFile</key><string>AppIcon</string>
</dict></plist>
PLIST
ICONSET="$ROOT/dist/AppIcon.iconset"
mkdir -p "$ICONSET"
"$CONTENTS/MacOS/SN VPN" --make-icon "$ROOT/dist/icon.png"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$ROOT/dist/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  /usr/bin/sips -z "$double" "$double" "$ROOT/dist/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/AppIcon.icns"
rm -rf "$ICONSET" "$ROOT/dist/icon.png"
"$CONTENTS/MacOS/SN VPN" --self-test
/usr/bin/codesign --force --options runtime --identifier local.snvpn.desktop --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/codesign --verify --strict "$HELPER"
echo "Built $APP"

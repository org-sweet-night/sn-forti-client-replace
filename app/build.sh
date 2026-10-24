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
bash "$ROOT/scripts/prepare-sparkle.sh"
UPDATER="$CONTENTS/Helpers/SN VPN Updater.app"
mkdir -p "$UPDATER/Contents/MacOS" "$UPDATER/Contents/Frameworks"
# Only the unprivileged updater loads Sparkle; the main app and root helper keep library validation.
/usr/bin/ditto "$ROOT/dist/sparkle/Sparkle.framework" "$UPDATER/Contents/Frameworks/Sparkle.framework"
swiftc -O -target "$SWIFT_TARGET" -parse-as-library -framework AppKit -framework Sparkle \
  -F "$ROOT/dist/sparkle" -Xlinker -rpath -Xlinker '@executable_path/../Frameworks' \
  "$ROOT/helpers/updater.swift" "$HERE/UpdateCoordinator.swift" -o "$UPDATER/Contents/MacOS/SN VPN Updater"
cat > "$UPDATER/Contents/Info.plist" <<'UPDATER_PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SN VPN Updater</string>
<key>CFBundleIdentifier</key><string>local.snvpn.updater</string>
<key>CFBundleName</key><string>SN VPN Updater</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
UPDATER_PLIST
/usr/bin/codesign --force --options runtime --identifier local.snvpn.updater \
  --entitlements "$ROOT/helpers/updater-entitlements.plist" --sign - "$UPDATER"
"$UPDATER/Contents/MacOS/SN VPN Updater" --self-test
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
swiftc -O -target "$SWIFT_TARGET" -framework AppKit -framework Foundation -framework ServiceManagement "$HERE/VPNHelperProtocol.swift" "$HERE/VPNHelperClient.swift" "$HERE/UpdateCoordinator.swift" "$VERSION_SWIFT" "$HERE/SNVPN.swift" -o "$CONTENTS/MacOS/SN VPN"
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SN VPN</string>
<key>CFBundleIdentifier</key><string>local.snvpn.desktop</string>
<key>CFBundleName</key><string>SN VPN</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$(sed -n 's/.*marketing = "\([^"]*\)".*/\1/p' "$VERSION_SWIFT")</string>
<key>CFBundleVersion</key><string>$(sed -n 's/.*marketing = "\([^"]*\)".*/\1/p' "$VERSION_SWIFT")</string>
<key>SNVPNBuildNumber</key><string>$(sed -n 's/.*build = "\([^"]*\)".*/\1/p' "$VERSION_SWIFT")</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>LSUIElement</key><true/>
<key>CFBundleIconFile</key><string>AppIcon</string>
</dict></plist>
PLIST
if [[ -n "${SNVPN_UPDATE_CONFIG:-}" ]]; then
  /usr/bin/python3 "$ROOT/scripts/configure-updates.py" "$CONTENTS/Info.plist" "$SNVPN_UPDATE_CONFIG" --test
else
  /usr/bin/python3 "$ROOT/scripts/configure-updates.py" "$CONTENTS/Info.plist" "$HERE/update-config.json"
fi
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
/usr/bin/codesign --verify --deep --strict "$UPDATER"
echo "Built $APP"

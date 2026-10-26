#!/bin/bash
set -euo pipefail
umask 022
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BRAND_VALUES="$(/usr/bin/python3 "$ROOT/scripts/branding.py" --shell)"
eval "$BRAND_VALUES"
APP="$ROOT/dist/$APP_NAME.app"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources" "$CONTENTS/Library/HelperTools" "$ROOT/dist"
BUILD_LOCK="$ROOT/dist/.build-lock"
SWIFT_TARGET="arm64-apple-macosx15.0"
if ! mkdir "$BUILD_LOCK" 2>/dev/null; then
  echo "Another build is already using $ROOT/dist" >&2
  exit 1
fi
trap 'rmdir "$BUILD_LOCK"' EXIT
BRAND_SWIFT="$ROOT/dist/BuildBrand.swift"
/usr/bin/python3 "$ROOT/scripts/branding.py" --swift "$BRAND_SWIFT"
cp "$HERE/brand.json" "$CONTENTS/Resources/brand.json"
VERSION_SWIFT="$ROOT/dist/BuildVersion.swift"
/usr/bin/python3 "$HERE/version.py" --self-test
/usr/bin/python3 "$HERE/version.py" "$VERSION_SWIFT"
bash "$ROOT/scripts/prepare-sparkle.sh"
UPDATER="$CONTENTS/Helpers/$UPDATER_NAME.app"
mkdir -p "$UPDATER/Contents/MacOS" "$UPDATER/Contents/Frameworks"
# Only the unprivileged updater loads Sparkle; the main app and root helper keep library validation.
/usr/bin/ditto "$ROOT/dist/sparkle/Sparkle.framework" "$UPDATER/Contents/Frameworks/Sparkle.framework"
swiftc -O -target "$SWIFT_TARGET" -parse-as-library -framework AppKit -framework Sparkle \
  -F "$ROOT/dist/sparkle" -Xlinker -rpath -Xlinker '@executable_path/../Frameworks' \
  "$BRAND_SWIFT" "$ROOT/helpers/updater.swift" "$HERE/UpdateCoordinator.swift" -o "$UPDATER/Contents/MacOS/$UPDATER_NAME"
/usr/bin/python3 - "$ROOT" "$UPDATER/Contents/Info.plist" <<'PYPLIST'
import plistlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from branding import BRAND
Path(sys.argv[2]).write_bytes(plistlib.dumps({
    'CFBundleExecutable': BRAND.updater_name, 'CFBundleIdentifier': BRAND.updater_id,
    'CFBundleName': BRAND.display_name + ' Updater', 'CFBundleDisplayName': BRAND.display_name + ' Updater',
    'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
    'LSMinimumSystemVersion': '15.0', 'LSUIElement': True}))
PYPLIST
/usr/bin/codesign --force --options runtime --identifier "$UPDATER_ID" \
  --entitlements "$ROOT/helpers/updater-entitlements.plist" --sign - "$UPDATER"
"$UPDATER/Contents/MacOS/$UPDATER_NAME" --self-test
HELPER="$CONTENTS/Library/HelperTools/$HELPER_LABEL"
swiftc -O -target "$SWIFT_TARGET" -framework Foundation "$BRAND_SWIFT" "$HERE/VPNHelperProtocol.swift" "$HERE/VPNHelper.swift" -o "$HELPER"
"$HELPER" --self-test
/usr/bin/codesign --force --options runtime --identifier "$HELPER_LABEL" --sign - "$HELPER"
/usr/bin/python3 - "$HELPER" "$CONTENTS/Resources/helper-requirement.txt" "$HELPER_LABEL" <<'PY'
import re, subprocess, sys
from pathlib import Path
result = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', sys.argv[1]], capture_output=True, text=True, check=True)
digest = re.search(r'^CDHash=([0-9a-f]{40})$', result.stderr, re.M)
assert digest
Path(sys.argv[2]).write_text('identifier "' + sys.argv[3] + '" and cdhash H"' + digest[1] + '"')
PY
swiftc -O -target "$SWIFT_TARGET" -framework AppKit -framework Foundation -framework ServiceManagement "$BRAND_SWIFT" "$HERE/VPNHelperProtocol.swift" "$HERE/VPNHelperClient.swift" "$HERE/UpdateCoordinator.swift" "$VERSION_SWIFT" "$HERE/App.swift" -o "$CONTENTS/MacOS/$APP_NAME"
/usr/bin/python3 - "$ROOT" "$CONTENTS/Info.plist" "$VERSION_SWIFT" <<'PYPLIST'
import plistlib, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from branding import BRAND
version = Path(sys.argv[3]).read_text()
marketing = re.search(r'marketing = "([^"]+)"', version)[1]
build = re.search(r'build = "([^"]+)"', version)[1]
Path(sys.argv[2]).write_bytes(plistlib.dumps({
    'CFBundleExecutable': BRAND.app_name, 'CFBundleIdentifier': BRAND.bundle_id,
    'CFBundleName': BRAND.display_name, 'CFBundleDisplayName': BRAND.display_name,
    'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': marketing,
    'CFBundleVersion': marketing, BRAND.build_key: build,
    'LSMinimumSystemVersion': '15.0', 'NSHighResolutionCapable': True,
    'LSUIElement': True, 'CFBundleIconFile': 'AppIcon'}))
PYPLIST
UPDATE_CONFIG_VARIABLE="${ENV_PREFIX}_UPDATE_CONFIG"
if [[ -n "${!UPDATE_CONFIG_VARIABLE:-}" ]]; then
  /usr/bin/python3 "$ROOT/scripts/configure-updates.py" "$CONTENTS/Info.plist" "${!UPDATE_CONFIG_VARIABLE}" --test
else
  /usr/bin/python3 "$ROOT/scripts/configure-updates.py" "$CONTENTS/Info.plist" "$HERE/update-config.json"
fi
ICONSET="$ROOT/dist/AppIcon.iconset"
mkdir -p "$ICONSET"
"$CONTENTS/MacOS/$APP_NAME" --make-icon "$ROOT/dist/icon.png"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$ROOT/dist/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  /usr/bin/sips -z "$double" "$double" "$ROOT/dist/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/AppIcon.icns"
rm -rf "$ICONSET" "$ROOT/dist/icon.png"
"$CONTENTS/MacOS/$APP_NAME" --self-test
/usr/bin/codesign --force --options runtime --identifier "$BUNDLE_ID" --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/codesign --verify --strict "$HELPER"
/usr/bin/codesign --verify --deep --strict "$UPDATER"
echo "Built $APP"

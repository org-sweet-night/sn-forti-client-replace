#!/bin/bash
set -euo pipefail
umask 022
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || { echo "macOS is required." >&2; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew, then run: brew install openconnect" >&2; exit 1; }
command -v swiftc >/dev/null || { echo "Install Xcode Command Line Tools: xcode-select --install" >&2; exit 1; }
PREFIX="$(brew --prefix openconnect)"
[[ -x "$PREFIX/bin/openconnect" ]] || { echo "Install OpenConnect: brew install openconnect" >&2; exit 1; }
"$PREFIX/bin/openconnect" --version | /usr/bin/grep -Eq '^Supported protocols:.*fortinet' || {
  echo "The installed OpenConnect must support the fortinet protocol." >&2; exit 1;
}
mkdir -p "$ROOT/dist"
STAGE="$(mktemp -d "$ROOT/dist/.runtime-prepare.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/service" "$STAGE/service/runtime"
swiftc -Onone -framework Foundation -framework Security "$ROOT/helpers/vpn-keychain.swift" -o "$STAGE/service/vpn-keychain"
cp "$STAGE/service/vpn-keychain" "$STAGE/service/vpn-profile-keychain"
swiftc -Onone -framework Foundation -framework SystemConfiguration "$ROOT/helpers/vpn-sleep-setting.swift" -o "$STAGE/service/vpn-sleep-setting"
/usr/bin/python3 - "$PREFIX/bin/openconnect" "$STAGE/service/runtime" <<'PY'
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

engine, destination = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
copied = {}
names = {}

def run(*args):
    return subprocess.check_output(args, text=True)

def system(path):
    return path.startswith(('/usr/lib/', '/System/Library/'))

def resolve(reference, source):
    def expand(value):
        return value.replace('@loader_path', str(source.parent)).replace('@executable_path', str(engine.parent))
    if reference.startswith('@rpath/'):
        paths = re.findall(r'cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.*?) \(offset', run('/usr/bin/otool', '-l', str(source)))
        candidates = [Path(expand(path)) / reference[7:] for path in paths]
    else:
        candidates = [Path(expand(reference))]
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError('Cannot resolve dependency: ' + reference)

def copy(source, name):
    source = source.resolve()
    if source in copied:
        return copied[source]
    if name in names and names[name] != source:
        raise RuntimeError('Conflicting dependency filename: ' + name)
    names[name] = source
    target = destination / name
    shutil.copy2(source, target)
    target.chmod(0o755)
    copied[source] = target
    lines = run('/usr/bin/otool', '-L', str(source)).splitlines()[1:]
    dependencies = [line.strip().rsplit(' (compatibility version', 1)[0] for line in lines]
    for reference in dependencies:
        if system(reference):
            continue
        dependency = resolve(reference, source)
        if dependency == source:  # dylib install name, not a dependency
            continue
        child = copy(dependency, dependency.name)
        subprocess.run(['/usr/bin/install_name_tool', '-change', reference, '@loader_path/' + child.name, str(target)], check=True)
    if source != engine:
        subprocess.run(['/usr/bin/install_name_tool', '-id', '@loader_path/' + name, str(target)], check=True)
    # Remove Homebrew rpaths after all dependency references have been rewritten.
    for path in re.findall(r'cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.*?) \(offset', run('/usr/bin/otool', '-l', str(target))):
        subprocess.run(['/usr/bin/install_name_tool', '-delete_rpath', path, str(target)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(target)], check=True)
    return target

copy(engine, 'openconnect')
for target in copied.values():
    for line in run('/usr/bin/otool', '-L', str(target)).splitlines()[1:]:
        reference = line.strip().rsplit(' (compatibility version', 1)[0]
        if not system(reference) and not (reference.startswith('@loader_path/') and (destination / reference[13:]).is_file()):
            raise RuntimeError('Unbundled dependency: ' + reference)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(target)], check=True)
print('Prepared OpenConnect and %d libraries.' % (len(copied) - 1))
PY
for helper in vpn-keychain vpn-profile-keychain vpn-sleep-setting; do
  /usr/bin/codesign --force --sign - "$STAGE/service/$helper"
done
# Keep the previous runtime until preparation and signing have completed.
if [[ -e "$ROOT/dist/service" ]]; then mv "$ROOT/dist/service" "$STAGE/previous-service"; fi
if ! mv "$STAGE/service" "$ROOT/dist/service"; then
  [[ ! -e "$STAGE/previous-service" ]] || mv "$STAGE/previous-service" "$ROOT/dist/service"
  exit 1
fi
echo "Prepared local runtime and Swift helpers in dist/service."

#!/bin/bash
# Build replaceable arm64 libraries from pinned upstream sources; no installation.
set -euo pipefail
umask 022
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$(uname -s)/$(uname -m)" == Darwin/arm64 ]] || { echo "arm64 macOS is required" >&2; exit 1; }
SDK="$(xcrun --show-sdk-path)"
mkdir -p "$ROOT/tmp" "$ROOT/dist"
WORK="$(mktemp -d "$ROOT/tmp/release-runtime.XXXXXX")"
OUT="$WORK/output"
mkdir -p "$OUT/runtime" "$OUT/sources" "$OUT/licenses" "$WORK/prefix"
trap 'rm -rf "$WORK"' EXIT
fetch() {
  local name="$1" url="$2" digest="$3"
  curl --fail --location --retry 2 "$url" -o "$OUT/sources/$name"
  echo "$digest  $OUT/sources/$name" | shasum -a 256 --check
  echo "$digest  $name" >> "$OUT/sources/SHA256SUMS"
  tar -xzf "$OUT/sources/$name" -C "$WORK"
}
fetch openssl-3.5.8.tar.gz https://github.com/openssl/openssl/releases/download/openssl-3.5.8/openssl-3.5.8.tar.gz a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2
fetch openconnect-9.21.tar.gz https://www.infradead.org/openconnect/download/openconnect-9.21.tar.gz 5b32369467db6e5f317aa1ed12cfcbb81ed00bdbc765450b6bfcbdc300944a58
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH
export CPPFLAGS=""
export MACOSX_DEPLOYMENT_TARGET=11.0
export CC="$(xcrun -f clang)"
export CFLAGS="-O2 -arch arm64 -mmacosx-version-min=11.0 -isysroot $SDK"
export LDFLAGS="-arch arm64 -mmacosx-version-min=11.0 -isysroot $SDK -Wl,-headerpad_max_install_names"
# Exclude Homebrew's dependency catalogue; only these bundled TLS libraries and SDK libraries.
export PKG_CONFIG_LIBDIR="$WORK/prefix/lib/pkgconfig"
export PKG_CONFIG_PATH=""
cd "$WORK/openssl-3.5.8"
./Configure darwin64-arm64-cc shared no-tests no-module --prefix=/usr/local/sn-vpn-runtime --libdir=lib --openssldir=/etc/ssl
make -j "$(sysctl -n hw.ncpu)"
make install_sw DESTDIR="$WORK/install"
cp -R "$WORK/install/usr/local/sn-vpn-runtime/." "$WORK/prefix/"
export DYLD_LIBRARY_PATH="$WORK/prefix/lib"
cd "$WORK/openconnect-9.21"
OPENSSL_CFLAGS="-I$WORK/prefix/include" OPENSSL_LIBS="-L$WORK/prefix/lib -lssl -lcrypto" \
LIBXML2_CFLAGS="-I$SDK/usr/include/libxml2" LIBXML2_LIBS="-lxml2" \
ZLIB_CFLAGS="-I$SDK/usr/include" ZLIB_LIBS="-lz" \
./configure --prefix="$WORK/prefix" --disable-nls --disable-static --enable-shared \
  --without-gnutls --without-libproxy --without-stoken --without-libpcsclite \
  --without-libpskc --without-gssapi --without-lz4 --with-vpnc-script=/usr/bin/false
make -j "$(sysctl -n hw.ncpu)"
cp .libs/openconnect "$OUT/runtime/openconnect"
cp .libs/libopenconnect.5.dylib "$OUT/runtime/"
cp "$WORK/prefix/lib/libssl.3.dylib" "$WORK/prefix/lib/libcrypto.3.dylib" "$OUT/runtime/"
cp "$WORK/openssl-3.5.8/LICENSE.txt" "$OUT/licenses/OpenSSL-LICENSE.txt"
cp COPYING.LGPL "$OUT/licenses/OpenConnect-COPYING.LGPL"
unset DYLD_LIBRARY_PATH
python3 - "$OUT/runtime" <<'CHECK'
from pathlib import Path
import re, subprocess, sys
runtime = Path(sys.argv[1])
def run(*args):
    return subprocess.check_output(args, text=True)
for path in runtime.iterdir():
    for line in run('otool', '-L', str(path)).splitlines()[1:]:
        ref = line.strip().split(' (compatibility version')[0]
        if ref.startswith(('/usr/lib/', '/System/Library/')):
            continue
        name = Path(ref).name
        if not (runtime / name).is_file():
            raise RuntimeError('Unbundled dependency: ' + ref)
        subprocess.run(['install_name_tool', '-change', ref, '@loader_path/' + name, str(path)], check=True)
    if path.suffix == '.dylib':
        subprocess.run(['install_name_tool', '-id', '@loader_path/' + path.name, str(path)], check=True)
    commands = run('otool', '-l', str(path))
    for rpath in re.findall(r'cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.*?) \(offset', commands):
        subprocess.run(['install_name_tool', '-delete_rpath', rpath, str(path)], check=True)
    minimum = re.findall(r'\bminos (\S+)', commands)
    if minimum != ['11.0'] or run('lipo', '-archs', str(path)).strip() != 'arm64':
        raise RuntimeError('Unexpected architecture/minimum OS: ' + path.name + str(minimum))
    subprocess.run(['codesign', '--force', '--sign', '-', str(path)], check=True)
    subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
version = run(str(runtime / 'openconnect'), '--version')
if 'OpenConnect version v9.21' not in version or not re.search(r'^Supported protocols:.*fortinet', version, re.M):
    raise RuntimeError('Unexpected OpenConnect capabilities: ' + version)
print(version)
CHECK
cp "$ROOT/scripts/build-release-runtime.sh" "$OUT/sources/build-release-runtime.sh"
# Publish only a fully checked output; retain any previous output until this succeeds.
if [[ -e "$ROOT/dist/release-runtime" ]]; then mv "$ROOT/dist/release-runtime" "$WORK/previous"; fi
if ! mv "$OUT" "$ROOT/dist/release-runtime"; then
  [[ ! -e "$WORK/previous" ]] || mv "$WORK/previous" "$ROOT/dist/release-runtime"
  exit 1
fi
echo "Built and verified dist/release-runtime (arm64, macOS 11.0)."

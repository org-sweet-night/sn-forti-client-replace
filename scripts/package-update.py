#!/usr/bin/python3
"""Build a Sparkle package that applies the complete managed release."""
import argparse
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_zip(archive):
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        links = {}
        for item in bundle.infolist():
            name = item.filename
            path = PurePosixPath(name)
            components = name.rstrip('/').split('/')
            if (path.is_absolute() or not path.parts or not name or
                    any(part in ('', '.', '..') for part in components) or '\\' in name):
                raise RuntimeError('unsafe release archive path')
            canonical = path.as_posix().rstrip('/')
            if canonical in seen:
                raise RuntimeError('duplicate release archive path')
            seen.add(canonical)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                try:
                    target = bundle.read(item).decode('utf-8')
                except UnicodeDecodeError as error:
                    raise RuntimeError('unsafe release archive link') from error
                links[name] = target
            elif mode and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeError('unsupported release archive entry')

        roots = {name.split('/', 1)[0] for name in seen}
        if len(roots) != 1:
            raise RuntimeError('unexpected release archive layout')
        framework = (next(iter(roots)) + '/dist/SN VPN.app/Contents/Helpers/'
                     'SN VPN Updater.app/Contents/Frameworks/Sparkle.framework')

        def inside(path):
            return path == framework or path.startswith(framework + '/')

        def resolve(path, active):
            current = ''
            for part in path.split('/'):
                if not part:
                    continue
                current = posixpath.join(current, part)
                if current in links:
                    current = resolve_link(current, active)
            return current

        def resolve_link(path, active):
            if path in active or not path.startswith(framework + '/'):
                raise RuntimeError('unsafe release archive link')
            base = resolve(posixpath.dirname(path), active | {path})
            target = links[path]
            if target.startswith('/') or '\\' in target or not target:
                raise RuntimeError('unsafe release archive link')
            for part in target.split('/'):
                if part in ('', '.'):
                    continue
                base = posixpath.dirname(base) if part == '..' else posixpath.join(base, part)
                if not inside(base):
                    raise RuntimeError('unsafe release archive link')
                if base in links:
                    base = resolve_link(base, active | {path})
            return base

        for path in links:
            resolve_link(path, set())


def postinstall_script():
    return (
        '#!/bin/sh\nset -eu\n'
        'case "${2:-}" in ""|/) ;; *) echo "SN VPN update requires the root install location" >&2; exit 1 ;; esac\n'
        'case "${3:-}" in /) ;; *) echo "SN VPN update requires the startup volume" >&2; exit 1 ;; esac\n'
        'SCRIPT_DIR=$(dirname "$0")\n'
        '/usr/bin/python3 -IB - "$SCRIPT_DIR" <<\'PY\'\n'
        'import os, stat, sys\nfrom pathlib import Path\n'
        'base = Path(os.path.abspath(sys.argv[1]))\nseen = set()\n'
        'for name in ("sparkle-install.py", "package-update.py", "release.zip"):\n'
        '    path = base / name\n'
        '    for item in (path, *path.parents):\n'
        '        if item in seen: continue\n'
        '        seen.add(item)\n        info = item.lstat()\n'
        '        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or (info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX)):\n'
        '            raise SystemExit("Unsafe package source")\n'
        '    if not stat.S_ISREG(path.lstat().st_mode): raise SystemExit("Unsafe package source")\n'
        'PY\n'
        'exec /usr/bin/python3 -IB "$SCRIPT_DIR/sparkle-install.py" --payload "$SCRIPT_DIR/release.zip"\n')


def package(archive, tag, output):
    if output.exists() or output.is_symlink():
        raise RuntimeError('package output already exists')
    with tempfile.TemporaryDirectory(prefix='snvpn-pkg-') as temporary:
        stage = Path(temporary)
        staged_archive = stage / 'release.zip'
        shutil.copy2(archive, staged_archive)
        validate_zip(staged_archive)
        payload = stage / 'payload'
        payload.mkdir()
        subprocess.run(['/usr/bin/ditto', '-x', '-k', str(staged_archive), str(payload)], check=True)
        roots = list(payload.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise RuntimeError('unexpected release archive layout')
        release = roots[0]
        subprocess.run(['/usr/bin/python3', '-IB', str(release / 'scripts/release.py'),
                        '--verify', str(release)], check=True)
        manifest = json.loads((release / 'RELEASE.json').read_text())
        version = manifest.get('version')
        if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
                or tag != 'v' + version):
            raise RuntimeError('Sparkle tag does not match release version')
        if (manifest.get('minimum_macos') != '15.0' or manifest.get('architecture') != 'arm64'
                or not isinstance(manifest.get('build'), str) or not manifest['build'].isdecimal()):
            raise RuntimeError('release manifest is not a supported Sparkle update')
        info = plistlib.loads((release / 'dist/SN VPN.app/Contents/Info.plist').read_bytes())
        bundle_version = manifest.get('bundle_version', manifest['build'])
        if (info.get('CFBundleShortVersionString') != version
                or info.get('CFBundleVersion') != bundle_version
                or ('bundle_version' in manifest and info.get('SNVPNBuildNumber') != manifest['build'])):
            raise RuntimeError('release manifest does not match app version and build')
        scripts = stage / 'scripts'
        scripts.mkdir()
        shutil.copy2(ROOT / 'scripts/sparkle-install.py', scripts / 'sparkle-install.py')
        shutil.copy2(ROOT / 'scripts/package-update.py', scripts / 'package-update.py')
        shutil.copy2(staged_archive, scripts / 'release.zip')
        (scripts / 'postinstall').write_text(postinstall_script())
        (scripts / 'postinstall').chmod(0o755)
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['/usr/bin/pkgbuild', '--nopayload', '--scripts', str(scripts),
                        '--identifier', 'local.snvpn.managed-update', '--version', bundle_version,
                        '--install-location', '/', str(output)], check=True)
        return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    archive, output = args.archive.resolve(), args.output.resolve()
    sidecar = output.with_suffix(output.suffix + '.json')
    if sidecar.exists() or sidecar.is_symlink():
        raise RuntimeError('package metadata output already exists')
    manifest = package(archive, args.tag, output)
    metadata = {'tag': args.tag, 'version': manifest['version'], 'build': manifest['build'],
                'minimum_macos': manifest['minimum_macos'], 'architecture': manifest['architecture'],
                'release_zip_sha256': sha256(archive), 'package_sha256': sha256(output)}
    if 'bundle_version' in manifest:
        metadata['bundle_version'] = manifest['bundle_version']
    sidecar.write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n')
    print(json.dumps(metadata, sort_keys=True))


if __name__ == '__main__':
    main()

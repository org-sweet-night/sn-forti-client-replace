#!/usr/bin/python3
"""Package reviewed binaries; verify the same payload before installation."""
import hashlib
import json
from pathlib import Path
import plistlib
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('symlink in release payload')
        if path.is_file() and path != root / 'RELEASE.json':
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def check_binaries(root):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or int(platform.mac_ver()[0].split('.')[0]) < 15:
        raise RuntimeError('This release requires Apple Silicon and macOS 15 or later')
    app = root / 'dist/SN VPN.app'
    plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if plist['LSMinimumSystemVersion'] != '15.0':
        raise RuntimeError('incorrect application minimum system version')
    binaries = [app / 'Contents/MacOS/SN VPN',
                app / 'Contents/Library/HelperTools/local.snvpn.helper']
    binaries += list((root / 'dist/service').glob('vpn-*'))
    runtime = root / 'dist/service/runtime'
    binaries += [p for p in runtime.iterdir() if p.is_file()]
    for path in binaries:
        if run('/usr/bin/lipo', '-archs', str(path)).strip() != 'arm64':
            raise RuntimeError('release requires arm64 binaries: ' + path.name)
        commands = run('/usr/bin/otool', '-l', str(path))
        versions = re.findall(r'\bminos (\d+(?:\.\d+)+)', commands)
        if not versions or any(tuple(map(int, v.split('.'))) > (15, 0, 0) for v in versions):
            raise RuntimeError('incompatible binary minimum OS: ' + path.name)
        for line in run('/usr/bin/otool', '-L', str(path)).splitlines()[1:]:
            dependency = line.strip().split(' (compatibility version')[0]
            if dependency.startswith(('/usr/lib/', '/System/Library/')):
                continue
            if not dependency.startswith('@loader_path/') or not (path.parent / dependency[13:]).is_file():
                raise RuntimeError('unbundled dependency: ' + path.name)
        run('/usr/bin/codesign', '--verify', '--strict', str(path))
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(app))
    run(str(binaries[0]), '--self-test')
    run(str(binaries[1]), '--self-test')
    version = run(str(runtime / 'openconnect'), '--version')
    if not re.search(r'^Supported protocols:.*fortinet', version, re.M):
        raise RuntimeError('Fortinet support missing')
    return plist


def verify(root):
    manifest = json.loads((root / 'RELEASE.json').read_text())
    if manifest['files'] != inventory(root):
        raise RuntimeError('release files changed or are incomplete; download again')
    check_binaries(root)
    print('Release payload, arm64/macOS 15 compatibility, signatures and self-tests verified.')


def package():
    plist = plistlib.loads((ROOT / 'dist/SN VPN.app/Contents/Info.plist').read_bytes())
    name = 'SN-VPN-' + plist['CFBundleShortVersionString'] + '-macOS-arm64'
    stage = ROOT / 'dist' / name
    if stage.exists():
        raise RuntimeError('release staging already exists')
    stage.mkdir()
    try:
        for folder, names in {
            'scripts': ['install.sh', 'install.py', 'update.sh', 'update.py', 'release.py'],
            'service': ['connect.py', 'control.py', 'setup.py', 'vpnc-script'],
            'docs': ['DISTRIBUTION.md', 'THIRD_PARTY_NOTICES.md'],
        }.items():
            (stage / folder).mkdir()
            for filename in names:
                shutil.copy2(ROOT / folder / filename, stage / folder / filename)
        shutil.copytree(ROOT / 'LICENSES', stage / 'LICENSES')
        shutil.copytree(ROOT / 'dist/release-runtime/sources', stage / 'sources')
        shutil.copytree(ROOT / 'dist/release-runtime/licenses', stage / 'runtime-licenses')
        (stage / 'dist/service').mkdir(parents=True)
        shutil.copytree(ROOT / 'dist/SN VPN.app', stage / 'dist/SN VPN.app')
        shutil.copytree(ROOT / 'dist/release-runtime/runtime', stage / 'dist/service/runtime')
        for helper in ('vpn-keychain', 'vpn-profile-keychain', 'vpn-sleep-setting'):
            shutil.copy2(ROOT / 'dist/service' / helper, stage / 'dist/service' / helper)
        shutil.copy2(ROOT / 'docs/DISTRIBUTION.md', stage / 'README.md')
        for label, command in [('Install', 'install.sh'), ('Update', 'update.sh')]:
            path = stage / (label + '.command')
            path.write_text('#!/bin/bash\ncd "$(dirname "$0")" || exit 1\n'
                            'bash scripts/' + command + '\nresult=$?\n'
                            'read -r -p "Press Return to close."\nexit "$result"\n')
            path.chmod(0o755)
        metadata = {'version': plist['CFBundleShortVersionString'], 'build': plist['CFBundleVersion'],
                    'minimum_macos': '15.0', 'architecture': 'arm64', 'signing': 'ad-hoc; not notarized',
                    'source_commit': run('git', '-C', str(ROOT), 'rev-parse', 'HEAD').strip(),
                    'files': inventory(stage)}
        (stage / 'RELEASE.json').write_text(json.dumps(metadata, indent=2) + '\n')
        verify(stage)
        archive = ROOT / 'dist' / (name + '.zip')
        if archive.exists():
            raise RuntimeError('release archive already exists')
        run('/usr/bin/ditto', '-c', '-k', '--norsrc', '--noextattr', '--keepParent', str(stage), str(archive))
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(checksum + '  ' + archive.name + '\n')
        print(archive.name + '\nSHA256 ' + checksum)
    finally:
        shutil.rmtree(stage)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--verify':
        verify(Path(sys.argv[2]).resolve())
    elif sys.argv[1:] == ['--package']:
        package()
    else:
        raise SystemExit('usage: release.py --package | --verify DIRECTORY')

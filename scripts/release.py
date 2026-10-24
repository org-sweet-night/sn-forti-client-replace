#!/usr/bin/python3
"""Package reviewed binaries; verify the same payload before installation."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            if not install.safe_framework_link(path, root):
                raise RuntimeError('unsafe symlink in release payload')
            files[path.relative_to(root).as_posix()] = hashlib.sha256(('symlink:' + os.readlink(path)).encode()).hexdigest()
            continue
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
    updater = app / 'Contents/Helpers/SN VPN Updater.app'
    framework = updater / 'Contents/Frameworks'
    if updater.exists():
        for path in updater.rglob('*'):
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as stream:
                    magic = stream.read(4)
                if magic in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf'):
                    binaries.append(path)
    for path in binaries:
        architectures = run('/usr/bin/lipo', '-archs', str(path)).strip().split()
        if 'arm64' not in architectures or (not path.is_relative_to(updater) and architectures != ['arm64']):
            raise RuntimeError('release requires arm64 binaries: ' + path.name)
        commands = run('/usr/bin/otool', '-arch', 'arm64', '-l', str(path))
        versions = re.findall(r'\bminos (\d+(?:\.\d+)+)', commands)
        if not versions or any(tuple(map(int, v.split('.'))) > (15, 0, 0) for v in versions):
            raise RuntimeError('incompatible binary minimum OS: ' + path.name)
        for line in run('/usr/bin/otool', '-arch', 'arm64', '-L', str(path)).splitlines()[1:]:
            dependency = line.strip().split(' (compatibility version')[0]
            if dependency.startswith(('/usr/lib/', '/System/Library/')):
                continue
            if dependency.startswith('@rpath/Sparkle.framework/') and (framework / dependency[7:]).is_file():
                continue
            if not dependency.startswith('@loader_path/') or not (path.parent / dependency[13:]).is_file():
                raise RuntimeError('unbundled dependency: ' + path.name)
        run('/usr/bin/codesign', '--verify', '--strict', str(path))
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(app))
    install.code_signature(app, 'local.snvpn.desktop')
    install.code_signature(app / install.APP_HELPER, install.HELPER_LABEL)
    if updater.exists():
        run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(updater))
        entitlements = run('/usr/bin/codesign', '-d', '--entitlements', '-', '--xml', str(updater))
        document = re.search(r'<plist(?:\s[^>]*)?>.*?</plist>', entitlements, re.S)
        if not document or plistlib.loads(document.group().encode()) != {'com.apple.security.cs.disable-library-validation': True}:
            raise RuntimeError('unexpected updater entitlements')
        run(str(updater / 'Contents/MacOS/SN VPN Updater'), '--self-test')
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
    if plist.get('SNVPNUpdateTestConfiguration'):
        raise RuntimeError('test update configuration cannot be released')
    if not plist.get('SUPublicEDKey'):
        raise RuntimeError('configure the permanent update signing public key before releasing')
    name = 'SN-VPN-' + plist['CFBundleShortVersionString'] + '-macOS-arm64'
    stage = ROOT / 'dist' / name
    if stage.exists():
        raise RuntimeError('release staging already exists')
    stage.mkdir()
    try:
        for folder, names in {
            'scripts': ['install.sh', 'install.py', 'update.sh', 'update.py', 'release.py'],
            'service': ['connect.py', 'control.py', 'setup.py', 'vpnc-script'],
            'docs': ['DISTRIBUTION.md', 'THIRD_PARTY_NOTICES.md', 'AUTOMATIC_UPDATES.md'],
        }.items():
            (stage / folder).mkdir()
            for filename in names:
                shutil.copy2(ROOT / folder / filename, stage / folder / filename)
        shutil.copytree(ROOT / 'LICENSES', stage / 'LICENSES')
        shutil.copy2(ROOT / 'dist/sparkle/LICENSE', stage / 'LICENSES/Sparkle.txt')
        shutil.copytree(ROOT / 'dist/release-runtime/sources', stage / 'sources')
        shutil.copytree(ROOT / 'dist/release-runtime/licenses', stage / 'runtime-licenses')
        (stage / 'dist/service').mkdir(parents=True)
        shutil.copytree(ROOT / 'dist/SN VPN.app', stage / 'dist/SN VPN.app', symlinks=True)
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
        metadata = {'version': plist['CFBundleShortVersionString'], 'build': plist['SNVPNBuildNumber'],
                    'bundle_version': plist['CFBundleVersion'],
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

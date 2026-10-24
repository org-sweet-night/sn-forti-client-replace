#!/usr/bin/python3
"""Apply Sparkle's full managed-release package without touching saved VPN state."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import importlib.util
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
_spec = importlib.util.spec_from_file_location('package_update', Path(__file__).with_name('package-update.py'))
package_update = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(package_update)

TARGET = Path('/Library/Application Support/Local VPN')
POLICY = TARGET / 'helper-policy.plist'
TRANSACTION = TARGET / '.update-transaction'
TMPDIR = '/private/tmp'


def safe_root_path(path, *, allow_sticky=False):
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise RuntimeError('unsafe package source')
        writable = info.st_mode & 0o022
        if writable and not (allow_sticky and stat.S_ISDIR(info.st_mode)
                             and info.st_mode & stat.S_ISVTX):
            raise RuntimeError('unsafe package source')
    return path.lstat()


def trusted_uid():
    uid = int(subprocess.check_output(['/usr/bin/stat', '-f', '%u', '/dev/console'], text=True).strip())
    if uid <= 0 or uid >= 2**32 - 1:
        raise RuntimeError('no logged-in console user')
    safe_root_path(POLICY)
    policy = plistlib.loads(POLICY.read_bytes())
    if policy.get('userID') != uid:
        raise RuntimeError('console user does not match installed helper policy')
    return uid


def run(args, *, capture=False):
    return subprocess.run(args, check=False, timeout=180,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.PIPE if capture else subprocess.DEVNULL, text=True)


def user_command(uid, executable, *args):
    return ['/bin/launchctl', 'asuser', str(uid), executable, *map(str, args)]


def status(uid, control):
    result = run(user_command(uid, '/usr/bin/python3', '-IB', control, 'status'), capture=True)
    if result.returncode:
        raise RuntimeError('could not read VPN state')
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError('could not read VPN state')
    if not isinstance(data, dict) or data.get('state') not in (
            'connected', 'degraded', 'connecting', 'reconnecting', 'stopped', 'error'):
        raise RuntimeError('could not read VPN state')
    if not isinstance(data.get('autostart'), bool):
        raise RuntimeError('could not read VPN state')
    return data['state'] in ('connected', 'degraded', 'connecting', 'reconnecting') or data['autostart'] is True


def control_action(uid, control, action):
    try:
        result = run(user_command(uid, '/usr/bin/python3', '-IB', control, action), capture=True)
        if result.returncode:
            return False
        return json.loads(result.stdout).get('ok') is True
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return False


def validate_release(archive, destination, uid):
    package_update.validate_zip(archive)
    destination.mkdir()
    subprocess.run(['/usr/bin/tar', '--no-same-owner', '-xf', str(archive), '-C', str(destination)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError('invalid release archive')
    release = roots[0]
    safe_root_path(release / 'scripts/update.py', allow_sticky=True)
    safe_root_path(release / 'service/control.py', allow_sticky=True)
    subprocess.run(user_command(uid, '/usr/bin/python3', '-IB', release / 'scripts/release.py', '--verify', release), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return release


def transaction_exists():
    return TRANSACTION.exists() or TRANSACTION.is_symlink()


def install(archive):
    script = Path(__file__).resolve()
    uid = None
    was_active = False
    restore_attempted = False
    control = TARGET / 'control.py'
    try:
        safe_root_path(script, allow_sticky=True)
        safe_root_path(archive, allow_sticky=True)
        uid = trusted_uid()
        safe_root_path(control)
        with tempfile.TemporaryDirectory(prefix='snvpn-sparkle-', dir=TMPDIR) as temporary:
            release = validate_release(archive, Path(temporary) / 'release', uid)
            updater = release / 'scripts/update.py'
            if transaction_exists():
                raise RuntimeError('unfinished update transaction')
            was_active = status(uid, control)
            if was_active:
                if not control_action(uid, control, 'stop'):
                    raise RuntimeError('could not stop VPN for update')
            for argument in ('--apply', '--check'):
                result = run(user_command(uid, '/usr/bin/python3', '-IB', updater, argument, uid))
                if result.returncode:
                    raise RuntimeError('managed update ' + ('failed' if argument == '--apply' else 'verification failed'))
                if transaction_exists():
                    raise RuntimeError('unfinished update transaction')
        if was_active:
            restore_attempted = True
            if not control_action(uid, control, 'start'):
                raise RuntimeError('update completed, but VPN could not be restored')
        print('SN VPN managed update installed and verified.')
        return 0
    except Exception as error:
        if uid is not None and was_active and not restore_attempted and not transaction_exists():
            restore_attempted = True
            if not control_action(uid, control, 'start'):
                error = RuntimeError(str(error) + '; VPN connection could not be restored')
        print('SN VPN update failed: ' + str(error), file=sys.stderr)
        return 1


def main(argv):
    if os.geteuid() != 0:
        print('SN VPN update failed: installer administrator privileges required', file=sys.stderr)
        return 1
    if len(argv) != 2 or argv[0] != '--payload':
        print('usage: sparkle-install.py --payload RELEASE.zip', file=sys.stderr)
        return 2
    return install(Path(argv[1]))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

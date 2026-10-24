#!/usr/bin/python3
"""Install the reviewed local VPN runtime; enable it only after authentication."""
import os
import fcntl
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'service'
ARTIFACTS = ROOT / 'dist' / 'service'
TARGET = Path('/Library/Application Support/Local VPN')
LABEL = 'local.vpn.autoconnect'
DAEMON = TARGET / (LABEL + '.plist')
LEGACY_DAEMON = Path('/Library/LaunchDaemons') / (LABEL + '.plist')
CONTROL = TARGET / 'control.py'
ARGS = ['/usr/bin/python3', '-I', str(TARGET / 'connect.py')]
HELPER_LABEL = 'local.snvpn.helper'
HELPER = TARGET / 'vpn-helper'
HELPER_POLICY = TARGET / 'helper-policy.plist'
HELPER_DAEMON = Path('/Library/LaunchDaemons') / (HELPER_LABEL + '.plist')
APP_PATH = Path('/Applications/SN VPN.app')
APP_HELPER = Path('Contents/Library/HelperTools/local.snvpn.helper')


def safe_installed(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('unsafe installed path')
    return info


def validate_plist(path):
    if not stat.S_ISREG(safe_installed(path).st_mode):
        raise RuntimeError('unexpected service file')
    with path.open('rb') as stream:
        value = plistlib.load(stream)
    if not isinstance(value, dict) or value.get('Label') != LABEL or value.get('ProgramArguments') != ARGS:
        raise RuntimeError('unexpected service definition')


def atomic_copy(source, destination, mode):
    fd, temporary = tempfile.mkstemp(prefix='.' + destination.name + '.', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_bytes(data, destination, mode):
    fd, temporary = tempfile.mkstemp(prefix='.' + destination.name + '.', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def check_app_path(path, owners):
    try:
        path.relative_to(APP_PATH)
    except ValueError:
        raise RuntimeError('unexpected application path')
    if APP_PATH != APP_PATH.resolve():
        raise RuntimeError('unsafe application path')
    if path == APP_PATH:
        parent = APP_PATH.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid not in owners or parent.st_mode & 0o002):
            raise RuntimeError('unsafe application parent')
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in owners or info.st_mode & 0o022):
            raise RuntimeError('unsafe application bundle')
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                info = (Path(root) / name).lstat()
                if stat.S_ISLNK(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
                    raise RuntimeError('unsafe application contents')
        return
    current = APP_PATH
    for part in path.parts[len(APP_PATH.parts):]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
            raise RuntimeError('unsafe application path')


def code_signature(path, identifier):
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(path)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(['/usr/bin/codesign', '-dv', '--verbose=4', str(path)],
                            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    details = result.stderr + result.stdout
    values = dict(line.split('=', 1) for line in details.splitlines() if '=' in line)
    digest = values.get('CDHash', '')
    if (values.get('Identifier') != identifier or not re.fullmatch(r'[0-9a-fA-F]{40}', digest)
            or not re.search(r'flags=.*\bruntime\b', details)):
        raise RuntimeError('invalid code signature')
    entitlements = subprocess.run(['/usr/bin/codesign', '-d', '--entitlements', '-', str(path)],
                                  check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoded = (entitlements.stdout + entitlements.stderr).strip()
    if encoded:
        try:
            document = re.search(rb'<plist(?:\s[^>]*)?>.*?</plist>', encoded.encode(), re.S)
            if document:
                values = plistlib.loads(document.group())
            elif all(line.startswith('Executable=') for line in encoded.splitlines()):
                values = {}
            else:
                raise ValueError('missing plist')
        except Exception as error:
            raise RuntimeError('invalid code entitlements') from error
        forbidden = {'get-task-allow', 'com.apple.security.get-task-allow', 'task_for_pid-allow',
                     'com.apple.security.cs.debugger', 'allow-dyld-environment-variables',
                     'com.apple.security.cs.allow-dyld-environment-variables',
                     'com.apple.security.cs.disable-library-validation',
                     'com.apple.security.cs.allow-unsigned-executable-memory',
                     'com.apple.security.cs.disable-executable-page-protection'}
        if not isinstance(values, dict) or forbidden.intersection(values):
            raise RuntimeError('unsafe code entitlements')
    return digest.lower()


def helper_launch_state():
    loaded = subprocess.run(['/bin/launchctl', 'print', 'system/' + HELPER_LABEL],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    disabled = subprocess.run(['/bin/launchctl', 'print-disabled', 'system'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    values = re.findall(rb'"local\.snvpn\.helper"\s*=>\s*([^\s;,}]+)', disabled.stdout)
    states = {b'enabled': False, b'disabled': True, b'true': True, b'false': False}
    if not values:
        return loaded, False
    if len(values) != 1 or values[0] not in states:
        raise RuntimeError('unable to confirm helper service state')
    return loaded, states[values[0]]


def helper_install(app_arg, uid_arg):
    if not uid_arg.isdecimal() or not 0 < int(uid_arg) < 2**32 - 1:
        raise RuntimeError('invalid user id')
    uid = int(uid_arg)
    if str(APP_PATH) != app_arg:
        raise RuntimeError('unexpected application path')
    owners = {0, int(os.environ.get('SUDO_UID', '0'))}
    check_app_path(APP_PATH, owners)
    app_helper = APP_PATH / APP_HELPER
    check_app_path(app_helper, owners)
    if not stat.S_ISREG(app_helper.lstat().st_mode):
        raise RuntimeError('unexpected helper executable')
    app_hash = code_signature(APP_PATH, 'local.snvpn.desktop')
    code_signature(app_helper, HELPER_LABEL)
    if not stat.S_ISDIR(safe_installed(TARGET).st_mode):
        raise RuntimeError('unexpected installation')
    for path in (HELPER_DAEMON.parent, TARGET):
        if not stat.S_ISDIR(safe_installed(path).st_mode):
            raise RuntimeError('unsafe install directory')

    lock_path = TARGET / 'control.lock'
    safe_installed(lock_path)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another VPN operation is in progress')
        loaded, disabled = helper_launch_state()
        paths = (HELPER, HELPER_POLICY, HELPER_DAEMON)
        previous = {}
        for path in paths:
            if path.exists() or path.is_symlink():
                info = safe_installed(path)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError('unsafe existing helper file')
                previous[path] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
            else:
                previous[path] = None
        daemon = {
            'Label': HELPER_LABEL,
            'ProgramArguments': [str(HELPER)],
            'MachServices': {HELPER_LABEL: True},
            'ProcessType': 'Interactive', 'Umask': 63, 'ExitTimeOut': 10,
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
        }
        policy = {'version': 1, 'userID': uid,
                  'clientRequirement': 'identifier "local.snvpn.desktop" and cdhash H"' + app_hash + '"'}
        backups = {path: previous[path] for path in paths}
        try:
            if loaded:
                subprocess.run(['/bin/launchctl', 'bootout', 'system/' + HELPER_LABEL], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            atomic_copy(app_helper, HELPER, 0o755)
            atomic_bytes(plistlib.dumps(policy), HELPER_POLICY, 0o644)
            atomic_bytes(plistlib.dumps(daemon), HELPER_DAEMON, 0o644)
            subprocess.run(['/bin/launchctl', 'enable', 'system/' + HELPER_LABEL], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(HELPER_DAEMON)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            subprocess.run(['/bin/launchctl', 'bootout', 'system/' + HELPER_LABEL],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for path, saved in backups.items():
                if saved is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(saved[0], path, saved[1])
            subprocess.run(['/bin/launchctl', 'enable', 'system/' + HELPER_LABEL],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if loaded and backups[HELPER_DAEMON] is not None:
                subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(HELPER_DAEMON)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if disabled:
                subprocess.run(['/bin/launchctl', 'disable', 'system/' + HELPER_LABEL],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise


def upgrade():
    if not stat.S_ISDIR(safe_installed(TARGET).st_mode):
        raise RuntimeError('unexpected installation')
    for name in ('control.py', 'connect.py', 'setup.py', 'vpn-keychain', 'vpn-profile-keychain', 'vpnc-script'):
        if not stat.S_ISREG(safe_installed(TARGET / name).st_mode):
            raise RuntimeError('unexpected installed file')
    runtime = TARGET / 'runtime'
    if not stat.S_ISDIR(safe_installed(runtime).st_mode):
        raise RuntimeError('unexpected runtime')
    for path in runtime.rglob('*'):
        info = safe_installed(path)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError('unexpected runtime file')
    if not stat.S_ISREG(safe_installed(runtime / 'openconnect').st_mode):
        raise RuntimeError('runtime missing')
    source = SOURCE / 'control.py'
    source_info = source.lstat()
    invoking_uid = os.environ.get('SUDO_UID', '')
    owners = {0, int(invoking_uid)} if invoking_uid.isdecimal() else {0}
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_uid not in owners or source_info.st_mode & 0o022:
        raise RuntimeError('unsafe installation source')

    legacy = LEGACY_DAEMON.exists() or LEGACY_DAEMON.is_symlink()
    current = DAEMON.exists() or DAEMON.is_symlink()
    if not legacy and not current:
        raise RuntimeError('service definition missing')
    for path, exists in ((LEGACY_DAEMON, legacy), (DAEMON, current)):
        if exists:
            validate_plist(path)
    if legacy and current and LEGACY_DAEMON.read_bytes() != DAEMON.read_bytes():
        raise RuntimeError('conflicting service definitions')
    # Prepare the new path before control starts validating it; retain the boot file on failure.
    if not current:
        atomic_copy(LEGACY_DAEMON, DAEMON, 0o644)
    try:
        atomic_copy(source, CONTROL, 0o644)
    except Exception:
        if not current:
            DAEMON.unlink()
        raise
    if legacy:
        LEGACY_DAEMON.unlink()


def initialize():
    """Install the runtime and idle service definition without importing a profile."""
    if TARGET.exists() or TARGET.is_symlink() or LEGACY_DAEMON.exists() or LEGACY_DAEMON.is_symlink():
        raise RuntimeError('existing installation requires review')

    files = ('connect.py', 'setup.py', 'control.py', 'vpn-keychain', 'vpn-profile-keychain', 'vpnc-script')
    invoking_uid = os.environ.get('SUDO_UID', '')
    owners = {0, int(invoking_uid)} if invoking_uid.isdecimal() else {0, os.getuid()}
    sources = {}
    for name in files:
        source = (ARTIFACTS if name in ('vpn-keychain', 'vpn-profile-keychain') else SOURCE) / name
        info = source.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in owners
                or info.st_mode & 0o022):
            raise RuntimeError('unexpected installation source')
        sources[name] = source
    runtime = ARTIFACTS / 'runtime'
    runtime_info = runtime.lstat()
    if not stat.S_ISDIR(runtime_info.st_mode) or runtime_info.st_uid not in owners or runtime_info.st_mode & 0o022:
        raise RuntimeError('unexpected installation source')
    for path in runtime.rglob('*'):
        info = path.lstat()
        if (info.st_uid not in owners or info.st_mode & 0o022
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
            raise RuntimeError('unexpected installation source')
    executable = runtime / 'openconnect'
    if not stat.S_ISREG(executable.lstat().st_mode):
        raise RuntimeError('runtime missing')
    subprocess.run([str(executable), '--version'], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)

    daemon = {
        'Label': LABEL,
        'ProgramArguments': ['/usr/bin/python3', '-I', str(TARGET / 'connect.py')],
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 60,
        'ExitTimeOut': 25,
        'Umask': 63,
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': '/dev/null',
    }
    stage = Path(tempfile.mkdtemp(prefix='.local-vpn-', dir=TARGET.parent))
    try:
        for name, source in sources.items():
            destination = stage / name
            shutil.copyfile(source, destination)
            destination.chmod(0o755 if name in ('vpn-keychain', 'vpn-profile-keychain', 'vpnc-script') else 0o644)
        shutil.copytree(runtime, stage / 'runtime', symlinks=False)
        (stage / 'runtime').chmod(0o755)
        for path in (stage / 'runtime').rglob('*'):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                path.chmod(0o755)
            else:
                path.chmod(0o755 if path == stage / 'runtime/openconnect' else 0o644)
        (stage / 'control.lock').touch(mode=0o600)
        (stage / 'control.lock').chmod(0o600)
        with (stage / DAEMON.name).open('xb') as stream:
            plistlib.dump(daemon, stream)
        (stage / DAEMON.name).chmod(0o644)
        stage.chmod(0o755)
        if TARGET.exists() or TARGET.is_symlink():
            raise RuntimeError('existing installation requires review')
        os.rename(stage, TARGET)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    if sys.argv[1:] == ['--upgrade']:
        upgrade()
        print('install: reviewed control update installed; active service left running', flush=True)
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == '--install-helper':
        helper_install(sys.argv[2], sys.argv[3])
        print('install: restricted VPN helper installed', flush=True)
        return 0
    if sys.argv[1:] in ([], ['--initialize']):
        initialize()
        print('install: idle VPN runtime initialized; import a profile in the app to connect', flush=True)
        return 0
    if len(sys.argv) != 2:
        raise RuntimeError('profile required')
    initialize()
    log_path = TARGET / 'setup-status.log'
    with log_path.open('w') as log:
        log_path.chmod(0o644)
        result = subprocess.run(['/usr/bin/python3', '-I', str(TARGET / 'setup.py'), sys.argv[1]],
                                stdout=log, stderr=log, env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
    if result.returncode:
        print('install: setup incomplete; automatic connection is disabled', flush=True)
        return 1
    subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(DAEMON)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('install: authenticated configuration saved; automatic VPN service started', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('install: stopped; inspect safe setup status before retrying', flush=True)
        sys.exit(1)

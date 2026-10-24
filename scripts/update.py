#!/usr/bin/python3
"""Transactional updater for the managed SN VPN app and runtime."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'service'
ARTIFACTS = ROOT / 'dist' / 'service'
SOURCE_APP = ROOT / 'dist' / 'SN VPN.app'
APP = install.APP_PATH
RESIDUE = ('app-upgrade-backup', 'multi-profile-upgrade-backup', 'app-validation.json',
           'multi-profile-validation.json', 'validation.json', 'setup-status.log')
MANIFEST = install.TARGET / 'update-manifest.json'
SOURCE_UID = int(os.environ.get('SUDO_UID', os.getuid()))


def transaction_path():
    return install.TARGET / '.update-transaction'


def source_ok(path, owners=None, tree_root=None):
    info = path.lstat()
    owners = owners or {0, SOURCE_UID}
    link = stat.S_ISLNK(info.st_mode)
    if (info.st_uid not in owners or (not link and info.st_mode & 0o022)
            or (link and (tree_root is None or not install.safe_framework_link(path, tree_root)))):
        raise RuntimeError('unsafe update source')
    return info


def tree_files(root, owners=None):
    source_ok(root, owners)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise RuntimeError('unexpected update directory')
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            info = source_ok(path, owners, tree_root=root)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise RuntimeError('unsafe update source')
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                result[path.relative_to(root).as_posix()] = path
    return result


def digest(path):
    if path.is_symlink():
        return hashlib.sha256(('symlink:' + os.readlink(path)).encode()).hexdigest()
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def expected():
    files = {}
    for name in ('control.py', 'connect.py', 'setup.py', 'vpnc-script'):
        if not stat.S_ISREG(source_ok(SOURCE / name).st_mode):
            raise RuntimeError("unsafe update source")
        files['runtime/' + name] = SOURCE / name
    for name in ('vpn-keychain', 'vpn-profile-keychain'):
        if not stat.S_ISREG(source_ok(ARTIFACTS / name).st_mode):
            raise RuntimeError("unsafe update source")
        files['runtime/' + name] = ARTIFACTS / name
    for name, path in tree_files(ARTIFACTS / 'runtime').items():
        files['runtime/runtime/' + name] = path
    for name, path in tree_files(SOURCE_APP).items():
        files['app/' + name] = path
    return files


def installed_map():
    result = {}
    for name in ('control.py', 'connect.py', 'setup.py', 'vpnc-script', 'vpn-keychain', 'vpn-profile-keychain'):
        path = install.TARGET / name
        if path.exists() or path.is_symlink():
            info = install.safe_installed(path)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('unsafe installed file')
            result['runtime/' + name] = path
    runtime = install.TARGET / 'runtime'
    if runtime.exists() or runtime.is_symlink():
        install.safe_installed(runtime)
    for name, path in tree_files(runtime, {0}).items():
        result['runtime/runtime/' + name] = path
    install.check_app_path(APP, {0, SOURCE_UID})
    for name, path in tree_files(APP, {0, SOURCE_UID}).items():
        result['app/' + name] = path
    return result


def managed_hashes(files):
    result = {k: digest(v) for k, v in sorted(files.items())}
    for name, path in (('helper/vpn-helper', install.HELPER), ('helper/helper-policy.plist', install.HELPER_POLICY),
                       ('helper/helper-daemon.plist', install.HELPER_DAEMON), ('service/daemon.plist', install.DAEMON)):
        info = install.safe_installed(path)
        if not stat.S_ISREG(info.st_mode): raise RuntimeError('unsafe managed metadata')
        result[name] = digest(path)
    return dict(sorted(result.items()))


def check(uid):
    want, have = expected(), installed_map()
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    changed = sorted(name for name in want.keys() & have.keys() if digest(want[name]) != digest(have[name]))
    mismatch = sorted(set(missing + extra + changed))
    integrity = []
    try:
        app_hash = install.code_signature(APP, 'local.snvpn.desktop')
        install.code_signature(APP / install.APP_HELPER, install.HELPER_LABEL)
        if install.HELPER.read_bytes() != (APP / install.APP_HELPER).read_bytes():
            integrity.append('helper_binary')
        policy = plistlib.loads(install.HELPER_POLICY.read_bytes())
        if (policy.get('userID') != uid or policy.get('clientRequirement') !=
                'identifier "local.snvpn.desktop" and cdhash H"' + app_hash + '"'):
            integrity.append('helper_policy')
        if plistlib.loads(install.HELPER_DAEMON.read_bytes()).get('ProgramArguments') != [str(install.HELPER)]:
            integrity.append('helper_daemon')
        if plistlib.loads(install.DAEMON.read_bytes()).get('ProgramArguments') != install.ARGS:
            integrity.append('service_daemon')
        install.safe_installed(MANIFEST)
        manifest = json.loads(MANIFEST.read_text())
        if manifest != managed_hashes(have):
            integrity.append('manifest')
    except Exception:
        integrity.append('managed_metadata')
    if transaction_path().exists(): integrity.append('unfinished_transaction')
    if install.LEGACY_DAEMON.exists() or install.LEGACY_DAEMON.is_symlink():
        integrity.append('legacy_service_definition')
    residue = []
    for name in RESIDUE:
        path = install.TARGET / name
        if path.exists() or path.is_symlink():
            validate_residue(path)
            residue.append(name)
    known = {'control.py', 'connect.py', 'setup.py', 'vpnc-script', 'vpn-keychain',
             'vpn-profile-keychain', 'runtime', 'control.lock', 'settings.json',
             'profiles.json', 'status.json', 'profiles', 'status', 'helper-policy.plist',
             'vpn-helper', 'update-manifest.json', 'local.snvpn.helper.plist',
             'runner.lock', '.update-transaction', install.DAEMON.name} | set(RESIDUE)
    unknown = sorted(p.name for p in install.TARGET.iterdir() if p.name not in known)
    print(json.dumps({'managed_files': len(want), 'mismatches': mismatch,
                      'integrity_errors': sorted(set(integrity)), 'legacy_residue': residue,
                      'unknown_siblings': unknown}, sort_keys=True))
    return not mismatch and not residue and not integrity


def validate_residue(path):
    if path.is_symlink():
        raise RuntimeError('unsafe legacy residue')
    info = path.lstat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('unsafe legacy residue')
    if stat.S_ISDIR(info.st_mode):
        for base, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                child = Path(base) / name
                item = child.lstat()
                if stat.S_ISLNK(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
                    raise RuntimeError('unsafe legacy residue')


def vpn_idle():
    result = subprocess.run(['/bin/launchctl', 'print', 'system/' + install.LABEL],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        raise RuntimeError('VPN service is active')
    if 'Could not find service' not in result.stderr + result.stdout:
        raise RuntimeError('unable to confirm VPN service is idle')
    app = subprocess.run(['/usr/bin/pgrep', '-x', 'SN VPN'], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    if app.returncode == 0:
        raise RuntimeError('VPN app is running')
    if app.returncode != 1:
        raise RuntimeError('unable to confirm VPN app is stopped')


def credential_commands():
    commands = set()

    def add(settings):
        if not isinstance(settings, dict): raise RuntimeError('invalid saved VPN state')
        identity = settings.get('profile_id', 'primary')
        account = settings.get('credential_account')
        if identity == 'primary' and account is None:
            commands.add((str(install.TARGET / 'vpn-keychain'), '--read'))
        elif (isinstance(identity, str) and re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity)
                and account == 'profile-' + identity):
            commands.add((str(install.TARGET / 'vpn-profile-keychain'), '--read', account))
        else:
            raise RuntimeError('invalid saved VPN state')

    for name in ('settings.json', 'profiles.json'):
        path = install.TARGET / name
        if path.exists() or path.is_symlink():
            if not stat.S_ISREG(install.safe_installed(path).st_mode):
                raise RuntimeError('invalid saved VPN state')
            value = json.loads(path.read_text())
            if name == 'settings.json':
                add(value)
            else:
                if not isinstance(value, dict): raise RuntimeError('invalid saved VPN state')
                for identity, saved in value.items():
                    if not isinstance(saved, dict) or saved.get('profile_id') != identity:
                        raise RuntimeError('invalid saved VPN state')
                    add(saved)
    return sorted(commands)


def verify_credentials(commands):
    try:
        for command in commands:
            subprocess.run(command, check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as error:
        raise RuntimeError('saved VPN credential access failed') from error


def make_daemon():
    paths = [p for p in (install.DAEMON, install.LEGACY_DAEMON) if p.exists() or p.is_symlink()]
    if not paths: raise RuntimeError('service definition missing')
    for path in paths:
        install.validate_plist(path)
    data = paths[0].read_bytes()
    if any(path.read_bytes() != data for path in paths[1:]):
        raise RuntimeError('conflicting service definitions')
    return data


def copy_tree(source, destination):
    shutil.copytree(source, destination, symlinks=True)
    for path in [destination, *destination.rglob('*')]:
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode):
            os.chmod(path, 0o755 if stat.S_ISDIR(info.st_mode) or os.access(path, os.X_OK) else 0o644)


def write_file(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    install.atomic_copy(source, destination, mode)


def snapshot(paths, backup):
    saved = []
    for path in paths:
        if path.exists() or path.is_symlink():
            if path == APP:
                install.check_app_path(path, {0, SOURCE_UID})
            else:
                validate_owned_tree(path)
            info = path.lstat()
            saved.append((path, stat.S_ISDIR(info.st_mode), stat.S_IMODE(info.st_mode)))
            target = backup / str(len(saved))
            if stat.S_ISDIR(info.st_mode): shutil.copytree(path, target, symlinks=True)
            else: shutil.copy2(path, target)
        else:
            saved.append((path, None, None))
    return saved


def managed_paths():
    return [install.TARGET / n for n in ('control.py', 'connect.py', 'setup.py', 'vpnc-script',
            'vpn-keychain', 'vpn-profile-keychain', 'runtime', install.DAEMON.name,
            install.HELPER, install.HELPER_POLICY, install.HELPER_DAEMON, MANIFEST, APP,
            install.LEGACY_DAEMON, *(install.TARGET / n for n in RESIDUE))]


def validate_owned_tree(root):
    info = source_ok(root, {0})
    if stat.S_ISDIR(info.st_mode):
        tree_files(root, {0})
    elif not stat.S_ISREG(info.st_mode):
        raise RuntimeError('unsafe managed file type')


def finish_cleanup(transaction):
    for name in ('backup', 'app'):
        path = transaction / name
        if path.exists(): shutil.rmtree(path)
    (transaction / 'journal.json').unlink(missing_ok=True)
    (transaction / 'committed').unlink(missing_ok=True)
    (transaction / 'rolledback').unlink(missing_ok=True)
    transaction.rmdir()


def recover(uid):
    if os.geteuid() != 0: raise RuntimeError('root required')
    install.safe_installed(install.TARGET)
    transaction = transaction_path()
    validate_owned_tree(transaction)
    info = transaction.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError('unsafe update transaction')
    if not {p.name for p in transaction.iterdir()} <= {'backup', 'journal.json', 'app', 'committed'}:
        if not {p.name for p in transaction.iterdir()} <= {'backup', 'journal.json', 'app', 'committed', 'rolledback'}:
            raise RuntimeError('unknown update transaction files')
    if ((transaction / 'committed').exists() or (transaction / 'rolledback').exists()
            or not any(transaction.iterdir())):
        lock_path = install.TARGET / 'control.lock'
        with lock_path.open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('another VPN operation is in progress')
            vpn_idle()
            finish_cleanup(transaction)
        print('update: completed update cleanup')
        return
    journal_path = transaction / 'journal.json'
    journal_info = install.safe_installed(journal_path)
    if not stat.S_ISREG(journal_info.st_mode): raise RuntimeError('unsafe update journal')
    journal = json.loads(journal_path.read_text())
    paths = managed_paths()
    if journal.get('count') != len(paths) or len(journal.get('saved', [])) != len(paths):
        raise RuntimeError('invalid update journal')
    backup = transaction / 'backup'
    install.safe_installed(backup)
    saved = []
    expected_backup = set()
    for index, entry in enumerate(journal['saved'], 1):
        if (set(entry) != {'present', 'directory', 'mode'} or not isinstance(entry['present'], bool)
                or not isinstance(entry['directory'], bool)
                or (entry['present'] and (not isinstance(entry['mode'], int) or not 0 <= entry['mode'] <= 0o777))
                or (not entry['present'] and (entry['directory'] or entry['mode'] is not None))):
            raise RuntimeError('invalid update journal')
        if entry['present']:
            expected_backup.add(str(index))
            source = backup / str(index)
            info = source.lstat()
            if (stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) != entry['directory']
                    or info.st_uid != 0 or info.st_mode & 0o022):
                raise RuntimeError('incomplete update backup')
        elif (backup / str(index)).exists():
            raise RuntimeError('invalid update backup')
        saved.append((paths[index - 1], entry['directory'] if entry['present'] else None,
                      entry['mode'] if entry['present'] else None))
    if {p.name for p in backup.iterdir()} != expected_backup:
        raise RuntimeError('unknown update backup files')
    lock_path = install.TARGET / 'control.lock'
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another VPN operation is in progress')
        vpn_idle()
        loaded_now, _ = install.helper_launch_state()
        if loaded_now:
            subprocess.run(['/bin/launchctl', 'bootout', 'system/' + install.HELPER_LABEL], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restore(saved, backup)
        if journal.get('helper_loaded'):
            if journal.get('helper_disabled'):
                subprocess.run(['/bin/launchctl', 'enable', 'system/' + install.HELPER_LABEL], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(install.HELPER_DAEMON)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if journal.get('helper_disabled'):
                subprocess.run(['/bin/launchctl', 'disable', 'system/' + install.HELPER_LABEL], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif journal.get('helper_disabled'):
            subprocess.run(['/bin/launchctl', 'disable', 'system/' + install.HELPER_LABEL], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        install.atomic_bytes(b'restored', transaction / 'rolledback', 0o600)
        finish_cleanup(transaction)
    print('update: previous managed files restored')


def restore(saved, backup):
    for index, (path, is_dir, mode) in reversed(list(enumerate(saved, 1))):
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
            else: path.unlink()
        if is_dir is not None:
            source = backup / str(index)
            if is_dir: shutil.copytree(source, path, symlinks=True)
            else: shutil.copy2(source, path)
            os.chmod(path, mode)


def apply(uid):
    if os.geteuid() != 0: raise RuntimeError('root required')
    want = expected()
    subprocess.run([str(ARTIFACTS / 'runtime/openconnect'), '--version'], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    install.code_signature(SOURCE_APP, 'local.snvpn.desktop')
    install.code_signature(SOURCE_APP / install.APP_HELPER, install.HELPER_LABEL)
    if not stat.S_ISDIR(install.safe_installed(install.TARGET).st_mode): raise RuntimeError('unexpected installation')
    vpn_idle()
    lock_path = install.TARGET / 'control.lock'
    lock_info = install.safe_installed(lock_path)
    if not stat.S_ISREG(lock_info.st_mode): raise RuntimeError('unsafe update lock')
    transaction = transaction_path()
    prior_daemon = make_daemon()
    paths = managed_paths()
    installed_map()  # Fail on unsafe installed service, runtime, or app files before taking the backup.
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another VPN operation is in progress')
        vpn_idle()
        if transaction.exists() or transaction.is_symlink():
            raise RuntimeError('unfinished update transaction; run --recover UID')
        helper_was_loaded, helper_was_disabled = install.helper_launch_state()
        saved_credentials = credential_commands()
        verify_credentials(saved_credentials)
        transaction.mkdir(mode=0o700)
        backup = transaction / 'backup'; backup.mkdir()
        try:
            saved = snapshot(paths, backup)
            journal = {'count': len(paths), 'saved': [{'present': kind is not None,
                       'directory': bool(kind), 'mode': mode} for _, kind, mode in saved],
                       'helper_loaded': helper_was_loaded, 'helper_disabled': helper_was_disabled}
            install.atomic_bytes(json.dumps(journal, separators=(',', ':')).encode(), transaction / 'journal.json', 0o600)
        except Exception:
            shutil.rmtree(transaction)
            raise
        try:
            for name in ('control.py', 'connect.py', 'setup.py', 'vpnc-script'):
                write_file(SOURCE / name, install.TARGET / name, 0o644 if name != 'vpnc-script' else 0o755)
            for name in ('vpn-keychain', 'vpn-profile-keychain'):
                write_file(ARTIFACTS / name, install.TARGET / name, 0o755)
            runtime = install.TARGET / 'runtime'
            if runtime.exists(): shutil.rmtree(runtime)
            copy_tree(ARTIFACTS / 'runtime', runtime)
            staged_app = transaction / 'app'
            copy_tree(SOURCE_APP, staged_app)
            if APP.exists(): shutil.rmtree(APP)
            shutil.copytree(staged_app, APP, symlinks=True)
            if install.DAEMON.exists(): install.DAEMON.unlink()
            install.atomic_bytes(prior_daemon, install.DAEMON, 0o644)
            if install.LEGACY_DAEMON.exists(): install.LEGACY_DAEMON.unlink()
            install.helper_install(str(APP), str(uid), already_locked=True)
            install.code_signature(APP, 'local.snvpn.desktop')
            install.code_signature(APP / install.APP_HELPER, install.HELPER_LABEL)
            app_hash = install.code_signature(APP, 'local.snvpn.desktop')
            policy = plistlib.loads(install.HELPER_POLICY.read_bytes())
            helper_job = plistlib.loads(install.HELPER_DAEMON.read_bytes())
            if (install.HELPER.read_bytes() != (APP / install.APP_HELPER).read_bytes()
                    or policy.get('userID') != uid
                    or policy.get('clientRequirement') != 'identifier "local.snvpn.desktop" and cdhash H"' + app_hash + '"'
                    or helper_job.get('Label') != install.HELPER_LABEL
                    or helper_job.get('ProgramArguments') != [str(install.HELPER)]
                    or plistlib.loads(install.DAEMON.read_bytes()).get('ProgramArguments') != install.ARGS):
                raise RuntimeError('installed helper verification failed')
            verify_credentials(saved_credentials)
            actual = installed_map()
            if set(actual) != set(want) or any(digest(want[k]) != digest(actual[k]) for k in want):
                raise RuntimeError('installed file verification failed')
            for name in RESIDUE:
                path = install.TARGET / name
                if path.exists():
                    validate_residue(path)
                    if path.is_dir(): shutil.rmtree(path)
                    else: path.unlink()
            manifest = managed_hashes(actual)
            install.atomic_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode(), MANIFEST, 0o600)
            run_as_user(uid, ['--self-test'])
            run_as_user(uid, ['--helper-check'])
            install.atomic_bytes(b'verified', transaction / 'committed', 0o600)
        except Exception:
            try:
                loaded_now, _ = install.helper_launch_state()
                if loaded_now:
                    subprocess.run(['/bin/launchctl', 'bootout', 'system/' + install.HELPER_LABEL], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                restore(saved, backup)
                if helper_was_loaded:
                    if helper_was_disabled:
                        subprocess.run(['/bin/launchctl', 'enable', 'system/' + install.HELPER_LABEL],
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(install.HELPER_DAEMON)],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if helper_was_disabled:
                        subprocess.run(['/bin/launchctl', 'disable', 'system/' + install.HELPER_LABEL],
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif helper_was_disabled:
                    subprocess.run(['/bin/launchctl', 'disable', 'system/' + install.HELPER_LABEL],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                install.atomic_bytes(b'restored', transaction / 'rolledback', 0o600)
                finish_cleanup(transaction)
            except Exception:
                raise RuntimeError('update failed; recovery transaction retained')
            raise
        finish_cleanup(transaction)


def cleanup(uid):
    if os.geteuid() != 0: raise RuntimeError('root required')
    lock_path = install.TARGET / 'control.lock'
    if not stat.S_ISREG(install.safe_installed(lock_path).st_mode):
        raise RuntimeError('unsafe update lock')
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another VPN operation is in progress')
        if transaction_path().exists() or transaction_path().is_symlink():
            raise RuntimeError('unfinished update transaction; recover before cleanup')
        managed_hashes(installed_map())
        app_hash = install.code_signature(APP, 'local.snvpn.desktop')
        install.code_signature(APP / install.APP_HELPER, install.HELPER_LABEL)
        policy = plistlib.loads(install.HELPER_POLICY.read_bytes())
        if (policy.get('userID') != uid or policy.get('clientRequirement') !=
                'identifier "local.snvpn.desktop" and cdhash H"' + app_hash + '"'
                or install.HELPER.read_bytes() != (APP / install.APP_HELPER).read_bytes()):
            raise RuntimeError('installed helper verification failed')
        install.validate_plist(install.DAEMON)
        verify_credentials(credential_commands())
        paths = [install.TARGET / name for name in RESIDUE
                 if (install.TARGET / name).exists() or (install.TARGET / name).is_symlink()]
        for path in paths: validate_owned_tree(path)
        for path in paths:
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
        print(json.dumps({'removed_legacy_residue': [p.name for p in paths],
                          'installed_components_changed': False}))


def run_as_user(uid, argument):
    cmd = ['/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-u', '#' + str(uid),
           str(APP / 'Contents/MacOS/SN VPN'), *argument]
    subprocess.run(cmd, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv):
    if len(argv) != 2 or argv[0] not in ('--check', '--apply', '--recover', '--cleanup') or not argv[1].isdecimal() or not 0 < int(argv[1]) < 2**32 - 1:
        raise RuntimeError('usage: update.py --check|--apply|--recover|--cleanup UID')
    uid = int(argv[1])
    if argv[0] == '--check': return 0 if check(uid) else 1
    if argv[0] == '--cleanup':
        cleanup(uid)
        return 0
    if argv[0] == '--recover':
        recover(uid)
        return 0
    apply(uid)
    print('update: verified app and runtime updated')
    return 0


if __name__ == '__main__':
    try: sys.exit(main(sys.argv[1:]))
    except Exception as error:
        print('update: stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)

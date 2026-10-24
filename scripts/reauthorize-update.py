#!/usr/bin/python3
"""Run a managed update with narrowly authorized legacy credential ACL repair."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update

STATUS = re.compile(rb'(?:OSStatus:\s*|status\s+)(-?\d+)')
PROFILE = re.compile(r'^profile-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$')


def os_status(stderr):
    match = STATUS.search(stderr or b'')
    return match.group(1).decode() if match else 'unknown'


def helper_for(update_module, command):
    args = tuple(map(str, command))
    if len(args) == 2 and args[1] == '--read':
        name, account = 'vpn-keychain', 'primary'
    elif (len(args) == 3 and args[1] == '--read'
          and (match := PROFILE.fullmatch(args[2]))):
        name, account = 'vpn-profile-keychain', match.group(1)
    else:
        raise RuntimeError('unexpected credential command')
    source = update_module.ARTIFACTS / name
    installed = update_module.install.TARGET / name
    if Path(args[0]) != installed:
        raise RuntimeError('unexpected credential command')
    update_module.source_ok(source, {0, update_module.SOURCE_UID})
    if not stat.S_ISREG(source.lstat().st_mode):
        raise RuntimeError('unsafe credential helper source')
    update_module.install.safe_installed(installed)
    if update_module.digest(source) != update_module.digest(installed):
        raise RuntimeError('installed credential helper hash mismatch')
    return source, installed, account


def run_authorization(update_module, command, runner=subprocess.run):
    source, installed, account = helper_for(update_module, command)
    tool = update_module.ROOT / 'tools' / 'authorization-tool'
    update_module.source_ok(tool, {0, update_module.SOURCE_UID})
    if not stat.S_ISREG(tool.lstat().st_mode):
        raise RuntimeError('unsafe credential authorization tool')
    expected = update_module.digest(source)
    result = runner([str(tool), '--authorize', str(installed), account,
                     '--expected-sha256', expected],
                    check=False, timeout=180, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('saved VPN credential authorization failed (OSStatus ' +
                           os_status(result.stderr) + ')')
    result = runner(command, check=False, timeout=180, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('saved VPN credential access failed (OSStatus ' +
                           os_status(result.stderr) + ')')


def apply_with_reauthorization(update_module, uid, runner=subprocess.run):
    original_verify = update_module.verify_credentials
    verify_calls = 0

    def verify(commands):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            return original_verify(commands)
        if verify_calls != 2:
            raise RuntimeError('unexpected credential validation phase')
        for command in commands:
            run_authorization(update_module, command, runner)

    update_module.verify_credentials = verify
    try:
        update_module.apply(uid)
    finally:
        update_module.verify_credentials = original_verify
    if verify_calls != 2:
        raise RuntimeError('unexpected credential validation phases')
    with contextlib.redirect_stdout(io.StringIO()):
        if not update_module.check(uid):
            raise RuntimeError('managed update verification failed')


def control_action(operation):
    control = update.install.TARGET / 'control.py'
    update.install.safe_installed(control)
    result = subprocess.run(['/usr/bin/python3', '-I', str(control), operation],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=60)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError('VPN maintenance operation failed')
    if result.returncode or not isinstance(value, dict) or value.get('ok') is not True:
        raise RuntimeError('VPN maintenance operation failed')


def main(argv):
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    restore = len(argv) == 3 and argv[2] == '--restore-connection'
    if (len(argv) not in (2, 3) or (len(argv) == 3 and not restore) or argv[0] != '--apply' or not argv[1].isdecimal()
            or not 0 < int(argv[1]) < 2**32 - 1):
        raise RuntimeError('usage: reauthorize-update.py --apply UID')
    if restore:
        transaction = update.transaction_path()
        if transaction.exists() or transaction.is_symlink():
            raise RuntimeError('unfinished update transaction; run --recover UID')
        control_action('stop')
    try:
        apply_with_reauthorization(update, int(argv[1]))
    except Exception:
        if restore and not update.transaction_path().exists():
            try:
                control_action('start')
            except Exception:
                pass
        raise
    if restore:
        control_action('start')
    print('managed update verified; saved credential access retained')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception as error:
        message = str(error)
        if message.startswith(('saved VPN credential authorization failed',
                               'saved VPN credential access failed',
                               'update failed; recovery transaction retained')):
            print('reauthorize update failed: ' + message, file=sys.stderr)
        else:
            print('reauthorize update failed', file=sys.stderr)
        raise SystemExit(1)

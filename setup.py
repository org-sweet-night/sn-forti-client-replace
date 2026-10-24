#!/usr/bin/python3
"""Interactive certificate confirmation and one-shot VPN authentication."""
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import ssl
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import connect


class SetupError(Exception):
    pass


def dialog(message, password=False):
    message = json.dumps(message, ensure_ascii=False)
    if password:
        script = ('text returned of (display dialog ' + message +
                  ' default answer "" with hidden answer buttons {"取消", "连接并保存"}'
                  ' default button "连接并保存" cancel button "取消" with title "VPN 配置")')
    else:
        script = ('button returned of (display dialog ' + message +
                  ' buttons {"取消", "已核对，继续"} default button "取消"'
                  ' cancel button "取消" with title "确认 VPN 证书")')
    result = subprocess.run(['/usr/bin/osascript', '-e', script], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=300)
    if result.returncode:
        raise SetupError('cancelled')
    return result.stdout.removesuffix(b'\n')


def keychain(action, secret=None, account=None):
    settings = {'profile_id': account[8:], 'credential_account': account} if isinstance(account, str) and account.startswith('profile-') else {}
    if account is not None and not settings:
        raise SetupError('keychain')
    command = connect.credential_command(settings, action)
    # Both modules are installed together; tests can relocate the safe helper root.
    command[0] = str(BASE / Path(command[0]).name)
    result = subprocess.run(command,
                            input=None if secret is None else secret + b'\n',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30)
    if result.returncode:
        if action in ('read', 'delete') and b'OSStatus: -25300' in result.stderr:
            return None
        raise SetupError('keychain')
    return result.stdout


def prepare(name, profile):
    """No existing password is read or sent to a new destination."""
    connect.command(profile, 'pin-sha256:' + base64.b64encode(bytes(32)).decode(), BASE)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Public-certificate discovery only; no credentials sent.
    with socket.create_connection((profile['Server'], int(profile['ServerPort'])), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=profile['Server']) as tls:
            certificate = tls.getpeercert(binary_form=True)
    public_pem = subprocess.run(['/usr/bin/openssl', 'x509', '-inform', 'DER', '-pubkey', '-noout'],
                                input=certificate, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=10).stdout
    public_der = subprocess.run(['/usr/bin/openssl', 'pkey', '-pubin', '-outform', 'DER'],
                                input=public_pem, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=10).stdout
    pin = 'pin-sha256:' + base64.b64encode(hashlib.sha256(public_der).digest()).decode()
    fingerprint = ':'.join(hashlib.sha256(certificate).hexdigest()[i:i + 2].upper()
                           for i in range(0, 64, 2))
    dialog('VPN 网关：' + profile['Server'] + '\n\n请向网关管理员核对以下证书 SHA-256，'
           '确认一致后继续：\n\n' + fingerprint + '\n\n之后固定该公钥，变更时停止连接。')
    password = dialog('请输入 ' + name + ' 的 VPN 密码（不是 Mac 登录密码）。\n'
                      '认证成功后仅保存到系统钥匙串。', True)
    if not password or len(password) > 4096 or any(c in password for c in (0, 10, 13)):
        raise SetupError('invalid_password')
    password.decode('utf-8')
    argv = connect.command(profile, pin, BASE)
    argv.insert(1, '--authenticate')
    result = subprocess.run(argv, input=password + b'\n', stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=connect.ENV, timeout=60)
    error = None
    for line in result.stderr.decode('utf-8', errors='replace').splitlines():
        error = connect.merge_error(error, connect.classify(line))
    if (result.returncode or error in ('authentication', 'certificate', 'configuration', 'interactive_auth') or
            not any(line.startswith(b'COOKIE=') and len(line) > 7 for line in result.stdout.splitlines())):
        raise SetupError(error or 'authentication')
    return {'profile': name, 'servercert': pin, 'connection': profile}, password


def atomic_settings(data, filename='settings.json'):
    if filename not in ('settings.json', 'profiles.json'):
        raise SetupError('configuration')
    fd, name = tempfile.mkstemp(prefix='.settings-', dir=str(BASE))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, BASE / filename)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot():
    path = BASE / 'settings.json'
    old = path.read_bytes() if path.exists() else None
    secret = keychain('read')
    if old is not None and secret is None:
        raise SetupError('keychain')
    return old, secret


def restore(old, secret):
    if secret is None:
        keychain('delete')
    else:
        keychain('store', secret)
        if keychain('read') != secret:
            raise SetupError('rollback')
    if old is None:
        (BASE / 'settings.json').unlink(missing_ok=True)
    else:
        atomic_settings(old)


def commit(settings, password, previous):
    try:
        keychain('store', password)
        if keychain('read') != password:
            raise SetupError('keychain')
        atomic_settings(json.dumps(settings, ensure_ascii=False).encode())
    except Exception:
        try:
            restore(*previous)
        except Exception:
            raise SetupError('rollback')
        raise SetupError('commit')


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2:
        return 1
    name = sys.argv[1]
    with connect.PLIST.open('rb') as stream:
        profile = plistlib.load(stream)['Profiles'][name]
    profile = {key: profile[key] for key in ('VpnType', 'Server', 'ServerPort', 'User')}
    settings, password = prepare(name, profile)
    previous = snapshot()
    commit(settings, password, previous)
    print('setup: authenticated configuration saved', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('setup: configuration not completed; check previous connection', flush=True)
        sys.exit(1)

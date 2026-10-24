#!/usr/bin/python3
"""Fixed-service local control. stdout is one safe JSON object."""
import contextlib
import io
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import setup
import connect

LABEL = 'local.vpn.autoconnect'
JOB = 'system/' + LABEL
DAEMON = BASE / (LABEL + '.plist')
ENV = connect.ENV


class ControlError(Exception):
    pass


def execute(args, timeout=2):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env=ENV, timeout=timeout)


def job():
    result = execute(['/bin/launchctl', 'print', JOB])
    if result.returncode:
        return False, None
    match = re.search(rb'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    return True, int(match[1]) if match else None


def autostart():
    result = execute(['/bin/launchctl', 'print-disabled', 'system'])
    if result.returncode:
        raise ControlError('无法查询自动启动状态')
    values = re.findall(rb'"local\.vpn\.autoconnect"\s*=>\s*([^\s;,}]+)', result.stdout)
    if not values:
        return True  # No override: launchd uses the enabled default.
    states = {b'enabled': True, b'disabled': False, b'true': False, b'false': True}
    if len(values) != 1 or values[0] not in states:
        raise ControlError('无法确认自动启动状态')
    return states[values[0]]


def processes():
    result = execute(['/bin/ps', '-axo', 'pid=,ppid=,comm='])
    if result.returncode:
        raise ControlError('无法查询连接进程')
    entries = []
    for line in result.stdout.decode(errors='replace').splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            entries.append((int(fields[0]), int(fields[1]), fields[2]))
    return entries


def children(pid, entries):
    return [p for p, parent, executable in entries
            if parent == pid and executable == str(BASE / 'runtime/openconnect')]


def reachable():
    """Accept only a private DNS resolver actually routed through a VPN interface."""
    deadline = time.monotonic() + 3
    result = execute(['/usr/sbin/scutil', '--dns'], timeout=1)
    if result.returncode:
        return False
    candidates = []
    for block in re.split(r'resolver #\d+', result.stdout.decode(errors='replace')):
        for value in re.findall(r'nameserver\[\d+\]\s*:\s*(\S+)', block):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4 and any(address in network for network in (
                    ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                    ipaddress.ip_network('192.168.0.0/16'))):
                candidates.append(str(address))
    for address in list(dict.fromkeys(candidates))[:8]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        route = execute(['/sbin/route', '-n', 'get', address], timeout=min(0.5, remaining))
        if route.returncode or not re.search(r'interface:\s*utun\d+\b',
                                            route.stdout.decode(errors='replace')):
            continue
        try:
            with socket.create_connection((address, 53), timeout=max(0.01, min(0.5, deadline - time.monotonic()))):
                return True
        except OSError:
            pass
    return False


def status():
    enabled = autostart()
    loaded, pid = job()
    autoconnect = enabled and loaded
    try:
        record = json.loads((BASE / 'status.json').read_text())
    except (OSError, ValueError):
        record = {}
    entries = processes() if pid else []
    runner_live = pid is not None and any(p == pid for p, _, _ in entries)
    if not runner_live:
        if not autoconnect:
            state, message = 'stopped', '连接已停止'
        elif record.get('error') in ('network', 'session_expired'):
            state, message = 'reconnecting', '正在等待自动恢复连接'
        elif record.get('error'):
            state, message = 'error', '连接已停止重试，请检查认证或配置后重新连接'
        elif record.get('state') in (None, 'connecting', 'reconnecting'):
            state, message = 'connecting', '正在等待连接服务启动'
        else:
            state, message = 'error', '连接服务已退出，请重新连接'
        return {'state': state, 'reachable': False, 'autostart': autoconnect, 'message': message,
                'profile_id': active_id(record)}
    has_child = bool(children(pid, entries))
    good = has_child and reachable()
    if good and record.get('state') == 'connected':
        state, message = 'connected', 'VPN 已连接，内网可达'
    elif record.get('state') in ('connecting', 'reconnecting'):
        state, message = record['state'], '正在连接' if record['state'] == 'connecting' else '正在恢复连接'
    else:
        state, message = 'degraded', ('内网可达，连接状态尚未确认' if good else '连接进程运行中，内网尚不可达')
    return {'state': state, 'reachable': good, 'autostart': autoconnect, 'message': message,
            'profile_id': active_id(record)}


def validate_daemon():
    with DAEMON.open('rb') as stream:
        config = plistlib.load(stream)
    if (config.get('Label') != LABEL or config.get('ProgramArguments') !=
            ['/usr/bin/python3', '-I', str(BASE / 'connect.py')]):
        raise ControlError('服务路径校验失败')


def launch(args):
    if execute(['/bin/launchctl'] + args, timeout=10).returncode:
        raise ControlError('服务操作失败')


def validate_active():
    raw = read_file('settings.json')
    if raw is None:
        raise ControlError('请先选择并验证一项 VPN 配置')
    try:
        settings = json.loads(raw)
        connect.command(connect.load_profile(settings), settings['servercert'], BASE)
        connect.credential_command(settings)
    except Exception:
        raise ControlError('当前配置无效，请重新验证后连接')


def start():
    validate_active()
    validate_daemon()
    launch(['enable', JOB])
    loaded, pid = job()
    if not loaded:
        launch(['bootstrap', 'system', str(DAEMON)])
    elif not pid:
        launch(['kickstart', JOB])
    # Give launchd a short bounded opportunity to assign the initial runner PID.
    if not pid:
        deadline = time.monotonic() + 3
        while not job()[1] and time.monotonic() < deadline:
            time.sleep(0.1)
    # A live service is not restarted; the app can continue polling status.
    current = status()
    if current['state'] in ('stopped', 'error'):
        raise ControlError('服务未能保持运行，请检查配置')
    return {'ok': True, 'message': '连接已启动', **current}


def stop():
    launch(['disable', JOB])
    loaded, pid = job()
    before = processes()
    tracked = {p for p, parent, executable in before if parent == pid and executable == '/usr/bin/caffeinate'} if pid else set()
    if loaded:
        result = execute(['/bin/launchctl', 'bootout', JOB], timeout=35)
        if result.returncode and job()[0]:
            raise ControlError('未能停止连接服务')
    deadline = time.monotonic() + 18
    while True:
        entries = processes()
        remains = any(executable == str(BASE / 'runtime/openconnect') or
                      (p in tracked and executable == '/usr/bin/caffeinate') or p == pid
                      for p, _, executable in entries)
        if not remains and not job()[0]:
            publish('stopped')
            return {'ok': True, 'state': 'stopped', 'reachable': False,
                    'autostart': False, 'message': '连接已停止'}
        if time.monotonic() >= deadline:
            raise ControlError('连接仍在退出，请稍后重试')
        time.sleep(0.25)


def input_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ControlError('配置文件无效')
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ControlError('配置文件过大')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ControlError('配置字段重复')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ControlError('配置文件无效')
    return value


def validate_profile(value):
    if set(value) != {'name', 'server', 'port', 'username'}:
        raise ControlError('请填写名称、服务器、端口和用户名')
    for key, limit in [('name', 128), ('server', 253), ('username', 256)]:
        text = value[key]
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ControlError('配置字段无效')
    if type(value['port']) is not int:
        raise ControlError('端口必须为整数')
    profile = {'VpnType': 0, 'Server': value['server'], 'ServerPort': value['port'], 'User': value['username']}
    connect.command(profile, 'pin-sha256:' + 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', BASE)
    return value, profile


def input_profile(path):
    return validate_profile(input_json(path))


def configure(path):
    value, profile = input_profile(path)
    result = activate(dict(value, id='primary', reauth=True), profile)
    result['profile'] = value
    return result


def valid_id(identity):
    return isinstance(identity, str) and (identity == 'primary' or bool(re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity)))


def account_for(identity):
    if not valid_id(identity):
        raise ControlError('配置标识无效')
    return None if identity == 'primary' else 'profile-' + identity


def active_id(record=None):
    try:
        settings = json.loads((BASE / 'settings.json').read_bytes())
        identity = settings.get('profile_id', 'primary')
    except FileNotFoundError:
        return None
    except PermissionError:
        identity = (record or {}).get('profile_id', 'primary')
    except (OSError, ValueError, AttributeError):
        return None
    return identity if valid_id(identity) else None


def publish(state, error=None):
    # The control protocol has exactly one JSON response; runner progress stays private.
    with contextlib.redirect_stdout(io.StringIO()):
        connect.write_status(state, error, BASE)


def read_file(name):
    try:
        return (BASE / name).read_bytes()
    except FileNotFoundError:
        return None


def write_file(name, data):
    if data is None:
        (BASE / name).unlink(missing_ok=True)
    else:
        setup.atomic_settings(data, name)


def normalized_connection(settings):
    profile = connect.load_profile(settings)
    connect.command(profile, settings['servercert'], BASE)
    return {'VpnType': 0, 'Server': profile['Server'], 'ServerPort': int(profile['ServerPort']),
            'User': profile['User']}


def registry():
    raw = read_file('profiles.json')
    if raw is not None:
        profiles = json.loads(raw)
        if not isinstance(profiles, dict):
            raise ControlError('已保存配置无效')
        for identity, saved in profiles.items():
            if not valid_id(identity) or not isinstance(saved, dict) or saved.get('profile_id') != identity:
                raise ControlError('已保存配置无效')
            if saved.get('credential_account') != account_for(identity):
                raise ControlError('配置凭据标识无效')
            profiles[identity] = dict(saved, connection=normalized_connection(saved))
        return profiles
    old = read_file('settings.json')
    if old is None:
        return {}
    saved = json.loads(old)
    if saved.get('profile_id', 'primary') != 'primary':
        raise ControlError('配置索引缺失，请恢复后重试')
    saved = dict(saved, profile_id='primary', credential_account=None,
                 connection=normalized_connection(saved))
    connect.command(saved['connection'], saved['servercert'], BASE)
    return {'primary': saved}


def restore_service(enabled, loaded):
    if enabled:
        launch(['enable', JOB])
    if loaded and not job()[0]:
        launch(['enable', JOB])
        launch(['bootstrap', 'system', str(DAEMON)])
    if not enabled:
        launch(['disable', JOB])


def pause_failure():
    try:
        launch(['disable', JOB])
    except Exception:
        raise ControlError('恢复及暂停未完成，请检查服务')
    raise ControlError('恢复未完成，自动连接已暂停，请检查配置')


def restore_data(files, account, secret, restore_secret):
    failed = False
    if restore_secret:
        try:
            if secret is None:
                setup.keychain('delete', account=account)
            else:
                setup.keychain('store', secret, account=account)
                if setup.keychain('read', account=account) != secret:
                    raise ControlError('凭据恢复失败')
        except Exception:
            failed = True
    for name, raw in files.items():
        try:
            write_file(name, raw)
        except Exception:
            failed = True
    if failed:
        raise ControlError('配置恢复失败')


def activate(value, profile):
    identity = value['id']
    account = account_for(identity)
    profiles = registry()
    cached = profiles.get(identity)
    password = None
    if cached and cached['connection'] == profile and value.get('reauth') is not True:
        settings = dict(cached, profile=value['name'])
        if setup.keychain('read', account=account) is None:
            settings, password = setup.prepare(value['name'], profile)
    else:
        settings, password = setup.prepare(value['name'], profile)
    settings = dict(settings, profile_id=identity, credential_account=account)
    # Read rollback credentials only after authenticating a changed destination.
    old_secret = setup.keychain('read', account=account) if password is not None else None
    files = {name: read_file(name) for name in ('settings.json', 'profiles.json')}
    enabled, loaded = autostart(), job()[0]
    validate_daemon()
    secret_touched = data_touched = started = False
    try:
        stop()
        if password is not None:
            secret_touched = True
            setup.keychain('store', password, account=account)
            if setup.keychain('read', account=account) != password:
                raise ControlError('凭据保存验证失败')
        profiles[identity] = settings
        data_touched = True
        write_file('profiles.json', json.dumps(profiles, ensure_ascii=False).encode())
        write_file('settings.json', json.dumps(settings, ensure_ascii=False).encode())
        publish('connecting')
        started = True
        start()
    except Exception:
        try:
            if started:
                stop()
            if secret_touched or data_touched:
                restore_data(files, account, old_secret, secret_touched)
            publish('connecting' if loaded and enabled else 'stopped')
            restore_service(enabled, loaded)
        except Exception:
            pause_failure()
        raise ControlError('连接切换失败，已恢复原配置')
    public = {key: value[key] for key in ('id', 'name', 'server', 'port', 'username')}
    return {'ok': True, 'message': '配置已启用，正在连接', 'profile_id': identity, 'profile': public}


def connect_profile(path):
    value = input_json(path)
    if set(value) not in ({'id', 'name', 'server', 'port', 'username'},
                          {'id', 'name', 'server', 'port', 'username', 'reauth'}):
        raise ControlError('配置字段无效')
    if not valid_id(value['id']) or ('reauth' in value and type(value['reauth']) is not bool):
        raise ControlError('配置标识或认证选项无效')
    _, profile = validate_profile({key: value[key] for key in ('name', 'server', 'port', 'username')})
    return activate(value, profile)


def forget_profile(path):
    value = input_json(path)
    if set(value) != {'id'} or not valid_id(value['id']):
        raise ControlError('配置标识无效')
    identity = value['id']
    profiles = registry()
    current = active_id()
    loaded, pid = job()
    if current is None and (loaded or pid):
        raise ControlError('无法确认活动配置，请先断开连接后再删除')
    if current == identity and (loaded or pid):
        raise ControlError('请先断开当前连接，再删除')
    account = account_for(identity)
    old_secret = setup.keychain('read', account=account)
    files = {name: read_file(name) for name in ('settings.json', 'profiles.json')}
    touched = False
    try:
        if current == identity:
            stop()
        touched = True
        setup.keychain('delete', account=account)
        if setup.keychain('read', account=account) is not None:
            raise ControlError('凭据删除未完成')
        profiles.pop(identity, None)
        write_file('profiles.json', json.dumps(profiles, ensure_ascii=False).encode())
        if current == identity:
            write_file('settings.json', None)
            publish('stopped')
    except Exception:
        try:
            if touched:
                restore_data(files, account, old_secret, True)
            if current == identity:
                publish('stopped')
        except Exception:
            raise ControlError('删除失败，恢复未完成，请检查配置')
        raise ControlError('删除失败，原配置已保留')
    return {'ok': True, 'message': '配置及其保存凭据已删除', 'profile_id': active_id()}


def validate_installation():
    if BASE != Path('/Library/Application Support/Local VPN'):
        raise ControlError('服务安装路径无效')
    for path in (BASE, BASE / 'connect.py', BASE / 'setup.py', BASE / 'control.py', BASE / 'vpnc-script',
                 BASE / 'runtime', BASE / 'runtime/openconnect', BASE / 'vpn-keychain',
                 BASE / 'vpn-profile-keychain', DAEMON):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ControlError('服务文件权限无效')


def main(args):
    if args == ['status']:
        return status()
    if os.geteuid() != 0:
        return {'ok': False, 'message': '此操作需要管理员授权'}
    if not args or args[0] not in ('start', 'stop', 'configure', 'connect-profile', 'forget-profile') or len(args) != (2 if args[0] in ('configure', 'connect-profile', 'forget-profile') else 1):
        return {'ok': False, 'message': '无效操作'}
    validate_installation()
    validate_daemon()
    # Serialize mutations without a daemon, socket, or concurrent credential writes.
    with (BASE / 'control.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': False, 'message': '另一操作正在进行，请稍后重试'}
        if args[0] in ('configure', 'connect-profile', 'forget-profile'):
            return {'configure': configure, 'connect-profile': connect_profile,
                    'forget-profile': forget_profile}[args[0]](args[1])
        return {'start': start, 'stop': stop}[args[0]]()


if __name__ == '__main__':
    try:
        output = main(sys.argv[1:])
    except ControlError as error:
        output = {'ok': False, 'message': str(error)}
    except setup.SetupError as error:
        messages = {'cancelled': '已取消，原配置保持不变', 'authentication': '认证失败，原配置保持不变',
                    'certificate': '证书验证失败，原配置保持不变', 'interactive_auth': '需要额外认证，未保存配置'}
        output = {'ok': False, 'message': messages.get(str(error), '未能完成配置，请检查后重试')}
    except Exception:
        output = {'ok': False, 'message': '操作未完成，请检查配置或稍后重试'}
    if sys.argv[1:] == ['status'] and 'state' not in output:
        output = {'state': 'error', 'reachable': False, 'autostart': False,
                  'message': '暂时无法确认服务状态', 'profile_id': None}
    print(json.dumps(output, ensure_ascii=False))

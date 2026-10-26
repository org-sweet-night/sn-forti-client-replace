#!/usr/bin/python3
"""Foreground VPN runner. launchd owns restart policy."""
import base64
import binascii
import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlunsplit

BASE = Path(__file__).resolve().parent
PLIST = Path('/Library/Application Support/Fortinet/FortiClient/conf/vpn.plist')
ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': '/var/root', 'LANG': 'C'}
HELPER_POLICY = BASE / 'helper-policy.plist'


def load_profile(settings):
    if 'connection' in settings:
        if not isinstance(settings['connection'], dict):
            raise ValueError('configuration')
        return settings['connection']
    with PLIST.open('rb') as stream:
        return plistlib.load(stream)['Profiles'][settings['profile']]


def credential_command(settings, action='read'):
    if action not in ('read', 'store', 'delete'):
        raise ValueError('configuration')
    identity = settings.get('profile_id', 'primary')
    if not isinstance(identity, str) or not (identity == 'primary' or re.fullmatch(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity)):
        raise ValueError('configuration')
    account = settings.get('credential_account')
    if identity == 'primary':
        if account is not None:
            raise ValueError('configuration')
        return [str(BASE / 'vpn-keychain'), '--' + action]
    if account != 'profile-' + identity:
        raise ValueError('configuration')
    return [str(BASE / 'vpn-profile-keychain'), '--' + action, account]


def credential_user_id():
    """Read the installer-owned GUI bootstrap UID; fail closed if policy is unsafe."""
    try:
        info = HELPER_POLICY.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022):
            raise ValueError('credential policy')
        with HELPER_POLICY.open('rb') as stream:
            policy = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ValueError('credential policy') from error
    version = policy.get('version') if isinstance(policy, dict) else None
    if isinstance(version, bool) or version != 1:
        raise ValueError('credential policy')
    uid = policy.get('userID')
    if isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid < 2**32 - 1:
        raise ValueError('credential policy')
    requirement = policy.get('clientRequirement')
    if (not isinstance(requirement, str) or not re.fullmatch(
            r'identifier "local\.[a-z][a-z0-9]{0,23}vpn\.desktop" and cdhash H"[0-9A-Fa-f]{40}"',
            requirement)):
        raise ValueError('credential policy')
    return uid


def credential_run_command(settings):
    return ['/bin/launchctl', 'asuser', str(credential_user_id())] + credential_command(settings)


def command(profile, pin, base=BASE):
    if not isinstance(pin, str) or not pin.startswith('pin-sha256:'):
        raise ValueError('configuration')
    try:
        digest = base64.b64decode(pin[11:], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('configuration')
    if len(digest) != 32 or base64.b64encode(digest).decode('ascii') != pin[11:]:
        raise ValueError('configuration')
    if profile.get('VpnType') != 0:
        raise ValueError('configuration')
    host, port, user = (profile.get(k) for k in ('Server', 'ServerPort', 'User'))
    # Only a DNS hostname or IPv4 literal belongs here; never accept URL syntax.
    if (not isinstance(host, str) or len(host) > 253 or
            not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or
            any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                for label in host.split('.'))):
        raise ValueError('configuration')
    if isinstance(port, bool) or not re.fullmatch(r'[0-9]{1,5}', str(port)) or not 1 <= int(port) <= 65535:
        raise ValueError('configuration')
    if not isinstance(user, str) or not user or user.startswith('-') or any(ord(c) < 32 for c in user):
        raise ValueError('configuration')
    return [str(base / 'runtime/openconnect'), '--protocol=fortinet', '--non-inter',
            '--passwd-on-stdin', '--servercert=' + pin,
            '--script=' + shlex.quote(str(base / 'vpnc-script')), '--reconnect-timeout=300',
            '--user=' + user, urlunsplit(('https', host + ':' + str(int(port)), '', '', ''))]


def classify(line):
    line = line.lower()
    if line.startswith('script ') and ' returned error ' in line:
        return 'configuration'
    # OpenConnect's pin callback may accept a leaf despite these CA-chain warnings.
    if re.fullmatch(
            r'server certificate verify failed: (?:signer not found|self-signed certificate '
            r'in certificate chain|self-signed certificate|unable to get local issuer certificate|'
            r'unable to verify the first certificate)', line.strip()):
        return None
    if "fingerprint(s) specified via --servercert match server's certificate:" in line:
        return 'certificate'
    if (line.strip() in ('failed to complete authentication', 'failed to obtain webvpn cookie',
                        'failed to open https connection') or
            line.startswith('failed to open https connection to ')):
        return None
    if any(s in line for s in ('certificate verification failed', 'server certificate verify failed',
                               'certificate does not match', 'certificate expired',
                               'certificate has expired', 'servercert mismatch',
                               "server ssl certificate didn't match")):
        return 'certificate'
    if any(s in line for s in ('authentication failed', 'login failed', 'incorrect password',
                               'invalid password', 'permission denied', '401 unauthorized', '403 forbidden')):
        return 'authentication'
    if any(s in line for s in ('two-factor', 'two factor', 'tokeninfo', 'otp', 'saml',
                               'challenge', 'user input required', 'cannot ask for')):
        return 'interactive_auth'
    if any(s in line for s in ('could not resolve', 'failed to resolve', 'name or service not known',
                               'nodename nor servname', 'network is unreachable', 'no route to host',
                               'connection refused', 'connection timed out', 'connect timeout',
                               'ssl connection failure: the operation timed out',
                               'connection reset by peer', 'temporary failure in name resolution')):
        return 'network'
    if any(s in line for s in ('error', 'failed', 'failure', 'fatal')):
        return 'unknown'
    return None


def merge_error(current, new):
    # Authentication/certificate failures always override a later network timeout.
    rank = {None: 0, 'network': 1, 'unknown': 2, 'session_expired': 3,
            'configuration': 4, 'interactive_auth': 4,
            'authentication': 5, 'certificate': 6}
    return new if rank[new] > rank[current] else current


def write_status(state, error=None, base=BASE):
    try:
        settings = json.loads((base / 'settings.json').read_text())
        identity = settings.get('profile_id', 'primary')
        if not isinstance(identity, str) or not (identity == 'primary' or re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity)):
            identity = None
    except (OSError, ValueError, AttributeError):
        identity = None
    record = {'profile_id': identity, 'state': state, 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'error': error}
    fd, name = tempfile.mkstemp(prefix='.status-', dir=str(base))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, base / 'status.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print('vpn: ' + state + ((' (' + error + ')') if error else ''), flush=True)


def acquire_lock(base=BASE):
    stream = open(base / 'runner.lock', 'a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    return stream


def stop(child):
    if child is not None and child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run():
    if os.geteuid() != 0:
        print('vpn: stopped (root_required)', flush=True)
        return 0
    lock = acquire_lock()
    if lock is None:
        print('vpn: stopped (already_running)', flush=True)
        return 0
    child = awake = None
    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
        raise InterruptedError

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupt)
    try:
        settings = json.loads((BASE / 'settings.json').read_text())
        profile = load_profile(settings)
        argv = command(profile, settings['servercert'])
        write_status('connecting')
        credential = subprocess.run(credential_run_command(settings),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=ENV, timeout=30, check=True).stdout
        if not credential or len(credential) > 4096 or b'\n' in credential or b'\r' in credential or b'\0' in credential:
            raise ValueError('credential')
        credential.decode('utf-8')
        if interrupted:
            write_status('stopped')
            return 0
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=ENV)
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(child.pid)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
        try:
            child.stdin.write(credential + b'\n')
            child.stdin.close()
        finally:
            del credential
        error = None
        session_established = False
        pending = b''
        while True:
            chunk = os.read(child.stdout.fileno(), 4096)
            if not chunk:
                break
            pending += chunk
            lines = pending.split(b'\n')
            pending = lines.pop()
            if len(pending) > 8192:
                error = merge_error(error, 'unknown')
                stop(child)
                pending = b''
            for raw in lines:
                line = raw.decode('utf-8', errors='replace')
                kind = classify(line)
                # Some gateways reject a reconnect cookie; a fresh login gets one attempt.
                if kind == 'authentication' and session_established:
                    kind = 'session_expired'
                error = merge_error(error, kind)
                if kind in ('authentication', 'certificate', 'interactive_auth', 'configuration', 'session_expired'):
                    write_status('stopping', kind)
                    stop(child)
                elif kind == 'network':
                    write_status('reconnecting', kind)
                elif re.search(r'\bConfigured as\b|\bConnected as\b|\bESP session established\b', line) and error not in ('authentication', 'certificate', 'interactive_auth', 'configuration', 'session_expired'):
                    error = None
                    session_established = True
                    write_status('connected')
        if pending:
            error = merge_error(error, classify(pending.decode('utf-8', errors='replace')))
        child.wait()
        if interrupted:
            write_status('stopped')
            return 0
        error = error or 'unknown'
        write_status('disconnected', error)
        return 1 if error in ('network', 'session_expired') else 0
    except InterruptedError:
        write_status('stopped')
        return 0
    except Exception:
        # Never print exception text: subprocess and parser errors may contain credentials.
        write_status('stopped', 'configuration_or_credential')
        return 0
    finally:
        stop(child)
        stop(awake)
        lock.close()


if __name__ == '__main__':
    sys.exit(run())

"""Offline checks: no Keychain, VPN, or system configuration access."""
import base64
import importlib.util
import json
import io
import plistlib
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('vpn_runner', Path(__file__).resolve().parents[1] / 'service' / 'connect.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Checks(unittest.TestCase):
    def test_credential_policy_requires_safe_root_owned_gui_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / 'helper-policy.plist'
            policy = {'version': 1, 'userID': 501,
                      'clientRequirement': 'identifier "local.snvpn.desktop" and cdhash H"' + 'a' * 40 + '"'}
            policy_path.write_bytes(plistlib.dumps(policy))
            original_lstat = Path.lstat

            def root_owned(path):
                info = original_lstat(path)
                if path == policy_path:
                    return mock.Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)
                return info

            with mock.patch.object(runner, 'HELPER_POLICY', policy_path), \
                 mock.patch.object(Path, 'lstat', root_owned):
                self.assertEqual(runner.credential_user_id(), 501)
                self.assertEqual(runner.credential_run_command({}), [
                    '/bin/launchctl', 'asuser', '501', str(runner.BASE / 'vpn-keychain'), '--read'])

                for invalid in (
                        dict(policy, userID=True), dict(policy, userID=0),
                        dict(policy, userID=2**32 - 1), dict(policy, version=2),
                        dict(policy, version=True),
                        dict(policy, clientRequirement='allow any')):
                    policy_path.write_bytes(plistlib.dumps(invalid))
                    with self.assertRaises(ValueError):
                        runner.credential_user_id()

                policy_path.write_bytes(plistlib.dumps(policy))
                for attrs in ({'st_mode': stat.S_IFREG | 0o664, 'st_uid': 0},
                              {'st_mode': stat.S_IFREG | 0o644, 'st_uid': 501},
                              {'st_mode': stat.S_IFLNK | 0o777, 'st_uid': 0}):
                    with mock.patch.object(Path, 'lstat', return_value=mock.Mock(**attrs)):
                        with self.assertRaises(ValueError):
                            runner.credential_user_id()

                missing = policy_path.with_name('missing.plist')
                with mock.patch.object(runner, 'HELPER_POLICY', missing):
                    with self.assertRaises(ValueError):
                        runner.credential_user_id()

    def test_standalone_profile(self):
        profile = {'VpnType': 0, 'Server': 'vpn.example.test', 'ServerPort': 443, 'User': 'person'}
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / 'missing.plist'
            with mock.patch.object(runner, 'PLIST', legacy):
                self.assertEqual(runner.load_profile({'connection': profile}), profile)
                with self.assertRaises(ValueError):
                    runner.load_profile({'connection': None})
            with legacy.open('wb') as stream:
                plistlib.dump({'Profiles': {'test': profile}}, stream)
            with mock.patch.object(runner, 'PLIST', legacy):
                self.assertEqual(runner.load_profile({'profile': 'test'}), profile)

    def test_arguments(self):
        profile = {'VpnType': 0, 'Server': 'vpn.example.test', 'ServerPort': 443, 'User': 'person'}
        pin = 'pin-sha256:' + base64.b64encode(b'a' * 32).decode('ascii')
        args = runner.command(profile, pin)
        self.assertIn('--passwd-on-stdin', args)
        self.assertIn('--non-inter', args)
        self.assertIn('--servercert=' + pin, args)
        self.assertEqual(args[-1], 'https://vpn.example.test:443')
        self.assertFalse(any('secret' in arg for arg in args))
        for bad in ('pin-sha256:abcd', 'sha256:' + 'ab' * 32, pin + '\n',
                    'pin-sha256:' + base64.b64encode(b'a' * 31).decode('ascii'),
                    pin[:-2] + 'F=', None):
            with self.assertRaises(ValueError):
                runner.command(profile, bad)
        for field, bad in [('Server', 'user@vpn.example'), ('Server', 'vpn.example/path'),
                           ('Server', '--option'), ('Server', 'vpn..test'),
                           ('Server', 'vpn.example\n'), ('ServerPort', 0),
                           ('ServerPort', 65536), ('ServerPort', True),
                           ('ServerPort', '443/path'), ('User', '-x'), ('VpnType', 1)]:
            with self.assertRaises(ValueError):
                runner.command(dict(profile, **{field: bad}), pin)

    def test_error_priority(self):
        self.assertEqual(runner.classify("Script '/path with spaces/vpnc-script' returned error 127"), 'configuration')
        self.assertEqual(runner.classify('Connection timed out'), 'network')
        self.assertEqual(runner.classify('SSL connection failure: The operation timed out'), 'network')
        self.assertEqual(runner.classify('Authentication failed: connection timed out'), 'authentication')
        self.assertEqual(runner.classify('Server certificate verify failed'), 'certificate')
        for warning in ('signer not found', 'self-signed certificate in certificate chain',
                        'self-signed certificate', 'unable to get local issuer certificate',
                        'unable to verify the first certificate'):
            self.assertIsNone(runner.classify('Server certificate verify failed: ' + warning))
        self.assertEqual(runner.classify(
            "None of the 1 fingerprint(s) specified via --servercert match server's certificate: deadbeef"),
            'certificate')
        self.assertEqual(runner.classify('Server certificate has expired at yesterday'), 'certificate')
        self.assertIsNone(runner.classify('Client certificate expires soon at tomorrow'))
        self.assertEqual(runner.classify('Server certificate verify failed: certificate does not match hostname'),
                         'certificate')
        self.assertEqual(runner.classify("Server SSL certificate didn't match"), 'certificate')
        for summary in ('Failed to complete authentication', 'Failed to obtain WebVPN cookie',
                        'Failed to open HTTPS connection', 'Failed to open HTTPS connection to example.test'):
            self.assertIsNone(runner.classify(summary))
        self.assertEqual(runner.classify('Other failed security check'), 'unknown')
        self.assertEqual(runner.classify('OTP challenge required'), 'interactive_auth')
        self.assertEqual(runner.classify('Unexpected error'), 'unknown')
        self.assertEqual(runner.merge_error('authentication', 'network'), 'authentication')
        self.assertEqual(runner.merge_error('network', 'unknown'), 'unknown')
        self.assertEqual(runner.merge_error('session_expired', 'configuration'), 'configuration')
        self.assertEqual(runner.merge_error('session_expired', 'interactive_auth'), 'interactive_auth')
        self.assertEqual(runner.merge_error(None, None), None)

    def test_atomic_status_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runner.write_status('disconnected', 'authentication', base)
            data = json.loads((base / 'status.json').read_text())
            self.assertEqual(set(data), {'profile_id', 'state', 'time', 'error'})
            self.assertNotIn('secret', json.dumps(data))
            self.assertEqual(list(base.glob('.status-*')), [])
            lock = runner.acquire_lock(base)
            self.assertIsNotNone(lock)
            self.assertIsNone(runner.acquire_lock(base))
            lock.close()
            next_lock = runner.acquire_lock(base)
            self.assertIsNotNone(next_lock)
            next_lock.close()

    def test_run_failure_policy(self):
        profile = {'VpnType': 0, 'Server': 'vpn.example.test', 'ServerPort': 443, 'User': 'person'}
        for output, expected in [(b'Connection timed out\n', 1),
                                 (b'Connection timed out\nFailed to complete authentication\nFailed to obtain WebVPN cookie\nFailed to open HTTPS connection\n', 1),
                                 (b'Connection timed out\nFailed to open HTTPS connection to example.test\n', 1),
                                 (b'SSL connection failure: The operation timed out\nFailed to open HTTPS connection to example.test\n', 1),
                                 (b'401 Unauthorized\nFailed to open HTTPS connection to example.test\n', 0),
                                 (b'Authentication failed\nConnection timed out\n', 0),
                                 (b'Unexpected error\n', 0), (b'', 0), (b'__signal__', 0),
                                 (b'Server certificate verify failed: signer not found\nConnected to HTTPS example\nConnected as example\n', 0),
                                 (b'Server certificate verify failed: self-signed certificate in certificate chain\nConnected to HTTPS example\nConnected as example\n', 0),
                                 (b'Server certificate verify failed: unable to get local issuer certificate\nConnected to HTTPS example\nConnected as example\n', 0),
                                 (b'Configured as example, with SSL connected\n', 0),
                                 (b"None of the 1 fingerprint(s) specified via --servercert match server's certificate: deadbeef\n", 0),
                                 (b'Server certificate has expired at yesterday\n', 0),
                                 (b'Server certificate verify failed: certificate does not match hostname\n', 0),
                                 (b'Configured as example\n401 Unauthorized\nFailed to open HTTPS connection to example.test\n', 1),
                                 (b'Configured as example\nConnection timed out\n401 Unauthorized\n', 1),
                                 (b'Configured as example\n401 Unauthorized\nOTP challenge required\n', 0),
                                 (b"Configured as example\n401 Unauthorized\nScript '/path with spaces/vpnc-script' returned error 127\n", 0),
                                 (b"Configured as example\nServer SSL certificate didn't match\n", 0),
                                 (b'Configured as example\nOTP challenge required\n', 0),
                                 (b'Transient error\nConfigured as example, with SSL connected\n', 0),
                                 (b"Script '/path with spaces/vpnc-script' returned error 127\nConfigured as example\n", 0),
                                 (b"Server SSL certificate didn't match\n", 0)]:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / 'settings.json').write_text(json.dumps({
                    'profile': 'test', 'servercert': 'pin-sha256:' + base64.b64encode(b'a' * 32).decode()}))
                with (base / 'profiles.plist').open('wb') as stream:
                    plistlib.dump({'Profiles': {'test': profile}}, stream)
                with tempfile.TemporaryFile() as stream:
                    stream.write(output)
                    stream.seek(0)
                    child = mock.Mock(stdout=stream, stdin=io.BytesIO(), pid=123)
                    child.poll.return_value = 1
                    awake = mock.Mock()
                    awake.poll.return_value = 0
                    with mock.patch.object(runner, 'BASE', base), \
                         mock.patch.object(runner, 'PLIST', base / 'profiles.plist'), \
                         mock.patch.object(runner.os, 'geteuid', return_value=0), \
                         mock.patch.object(runner, 'acquire_lock', return_value=mock.Mock()), \
                         mock.patch.object(runner.signal, 'signal') as installed_signal, \
                         mock.patch.object(runner.os, 'read', wraps=runner.os.read) as read, \
                         mock.patch.object(runner, 'write_status') as status, \
                         mock.patch.object(runner, 'credential_run_command',
                                           return_value=['/bin/launchctl', 'asuser', '501', '/helper', '--read']) as credential_command, \
                         mock.patch.object(runner.subprocess, 'run', return_value=mock.Mock(stdout=b'secret')), \
                         mock.patch.object(runner.subprocess, 'Popen', side_effect=[child, awake]) as spawn:
                        if output == b'__signal__':
                            child.poll.return_value = None
                            def interrupt_read(fd, size):
                                installed_signal.call_args_list[0].args[1](runner.signal.SIGTERM, None)
                            read.side_effect = interrupt_read
                        self.assertEqual(runner.run(), expected)
                        credential_command.assert_called_once_with(mock.ANY)
                        if output == b'__signal__':
                            self.assertIn(mock.call('stopped'), status.call_args_list)
                            child.send_signal.assert_any_call(runner.signal.SIGINT)
                            child.wait.assert_called_with(timeout=15)
                        self.assertNotIn('secret', repr(spawn.call_args_list))
                        self.assertNotIn('secret', repr(status.call_args_list))
                        if (b'Connected as' in output or b'Configured as' in output) and not output.startswith(b'Script '):
                            self.assertIn(mock.call('connected'), status.call_args_list)
                            child.send_signal.assert_not_called()
                            child.kill.assert_not_called()
                        else:
                            self.assertNotIn(mock.call('connected'), status.call_args_list)

    def test_cleanup(self):
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner.stop(child)
        self.assertIsNotNone(child.poll())

    def test_script_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='vpn script test ') as directory:
            base = Path(directory)
            script = base / 'vpnc-script'
            script.write_text('#!/bin/sh\nprintf script_reached')
            script.chmod(0o700)
            profile = {'VpnType': 0, 'Server': 'vpn.example.test', 'ServerPort': 443, 'User': 'person'}
            pin = 'pin-sha256:' + base64.b64encode(b'a' * 32).decode('ascii')
            argument = next(a for a in runner.command(profile, pin, base) if a.startswith('--script='))
            result = subprocess.run(['/bin/sh', '-c', argument[len('--script='):]],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, 'script_reached')


if __name__ == '__main__':
    unittest.main()

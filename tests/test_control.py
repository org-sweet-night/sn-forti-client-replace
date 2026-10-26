"""Offline control checks. Never execute launchctl, Keychain, or VPN."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1] / 'service'
spec = importlib.util.spec_from_file_location('vpn_control', BASE / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
setup = control.setup


class Tests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(control, 'publish')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_autostart_override_formats(self):
        for value, expected in [(b'enabled', True), (b'disabled', False),
                                (b'true', False), (b'false', True)]:
            with patch.object(control, 'execute', return_value=Mock(
                    returncode=0, stdout=b'{ "local.vpn.autoconnect" => ' + value + b' }')):
                self.assertEqual(control.autostart(), expected)
        with patch.object(control, 'execute', return_value=Mock(returncode=0, stdout=b'{}')):
            self.assertTrue(control.autostart())
        for value in (b'unknown', b'trueish', b'enabled-extra'):
            with patch.object(control, 'execute', return_value=Mock(
                    returncode=0, stdout=b'{ "local.vpn.autoconnect" => ' + value + b' }')):
                with self.assertRaises(control.ControlError):
                    control.autostart()

    def test_nonroot_mutation(self):
        with patch.object(control.os, 'geteuid', return_value=501), patch.object(control, 'execute') as execute:
            self.assertFalse(control.main(['start'])['ok'])
            self.assertFalse(control.main(['configure', '/unused'])['ok'])
            execute.assert_not_called()

    def test_stale_connected_and_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for loaded, enabled, error, expected in [
                    (True, True, 'network', 'reconnecting'),
                    (True, True, 'session_expired', 'reconnecting'),
                    (True, True, 'authentication', 'error'),
                    (True, True, 'certificate', 'error'),
                    (True, True, None, 'error'),
                    (True, False, 'network', 'stopped'),
                    (False, True, 'network', 'stopped')]:
                (base / 'status.json').write_text(json.dumps({'state': 'disconnected' if error else 'connected', 'error': error}))
                with patch.object(control, 'BASE', base), \
                     patch.object(control, 'autostart', return_value=enabled), \
                     patch.object(control, 'job', return_value=(loaded, None)), \
                     patch.object(control, 'reachable') as reachable:
                    result = control.status()
                    self.assertEqual(result['state'], expected)
                    self.assertEqual(result['autostart'], loaded and enabled)
                    self.assertFalse(result['reachable'])
                    reachable.assert_not_called()
        with patch.object(control, 'autostart', return_value=True), \
             patch.object(control, 'job', return_value=(True, 10)), \
             patch.object(control, 'processes', return_value=[(10, 1, '/usr/bin/python3')]), \
             patch.object(control, 'reachable') as reachable:
            self.assertEqual(control.status()['state'], 'degraded')
            reachable.assert_not_called()

    def test_dns_route_proof_without_interface_index(self):
        dns = b'resolver #1\n nameserver[0] : 10.1.2.3\n'
        with patch.object(control, 'execute', side_effect=[
                Mock(returncode=0, stdout=dns),
                Mock(returncode=0, stdout=b'interface: utun8\n')]), \
             patch.object(control.socket, 'create_connection') as socket:
            self.assertTrue(control.reachable())
            socket.assert_called_once_with(('10.1.2.3', 53), timeout=0.5)
        with patch.object(control, 'execute', side_effect=[
                Mock(returncode=0, stdout=dns),
                Mock(returncode=0, stdout=b'interface: en0\n')]), \
             patch.object(control.socket, 'create_connection') as socket:
            self.assertFalse(control.reachable())
            socket.assert_not_called()

    def test_start_idempotent(self):
        with patch.object(control, 'validate_active'), patch.object(control, 'validate_daemon'), patch.object(control, 'launch') as launch, \
             patch.object(control, 'job', return_value=(True, 10)), \
             patch.object(control, 'status', return_value={'state': 'connected'}):
            self.assertTrue(control.start()['ok'])
            launch.assert_called_once_with(['enable', control.JOB])
        with patch.object(control, 'validate_active'), patch.object(control, 'validate_daemon'), patch.object(control, 'launch') as launch, \
             patch.object(control, 'job', side_effect=[(True, None), (True, 10)]), \
             patch.object(control, 'status', return_value={'state': 'connecting'}):
            control.start()
            self.assertIn((['kickstart', control.JOB],), [c.args for c in launch.call_args_list])
            self.assertNotIn('-k', str(launch.call_args_list))

    def test_stop_idempotent(self):
        with patch.object(control, 'launch') as launch, patch.object(control, 'job', return_value=(False, None)), \
             patch.object(control, 'processes', return_value=[]):
            self.assertEqual(control.stop()['state'], 'stopped')
            launch.assert_called_once_with(['disable', control.JOB])

    def test_input_boundaries(self):
        valid = {'name': 'Office VPN', 'server': 'vpn.example.test', 'port': 443, 'username': 'person'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            path.write_text(json.dumps(valid))
            self.assertEqual(control.input_profile(path)[0], valid)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                control.input_profile(link)
            for changes in [{'server': 'x;touch /tmp/no'}, {'username': '-option'}, {'port': True},
                            {'port': 70000}, {'name': 'x\n'}, {'password': 'secret'}]:
                path.write_text(json.dumps(dict(valid, **changes)))
                with self.assertRaises((ValueError, control.ControlError)):
                    control.input_profile(path)
            for raw in ['{"name":"x","name":"y"}', 'x' * 4097, '[]']:
                path.write_text(raw)
                with self.assertRaises((ValueError, control.ControlError)):
                    control.input_profile(path)

    def test_auth_failure_unchanged(self):
        with patch.object(control, 'input_profile', return_value=({'name': 'test'}, {})), \
             patch.object(setup, 'prepare', side_effect=setup.SetupError('authentication')), \
             patch.object(setup, 'snapshot') as snapshot, patch.object(control, 'stop') as stop:
            with self.assertRaises(setup.SetupError):
                control.configure('/unused')
            snapshot.assert_not_called()
            stop.assert_not_called()

    def test_commit_failure_restores_password_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            old = b'{"profile":"old"}'
            (base / 'settings.json').write_bytes(old)
            secrets = [b'old-password']
            def keychain(action, secret=None):
                if action == 'store':
                    secrets[0] = secret
                    return b''
                if action == 'read':
                    return secrets[0]
                self.fail('unexpected deletion')
            real_atomic = setup.atomic_settings
            def atomic(data):
                if data != old:
                    raise OSError('simulated disk error')
                real_atomic(data)
            with patch.object(setup, 'BASE', base), patch.object(setup, 'keychain', side_effect=keychain), \
                 patch.object(setup, 'atomic_settings', side_effect=atomic):
                with self.assertRaises(setup.SetupError):
                    setup.commit({'profile': 'new'}, b'new-password', (old, b'old-password'))
            self.assertEqual(secrets[0], b'old-password')
            self.assertEqual((base / 'settings.json').read_bytes(), old)

    def test_prepare_certificate_and_mfa_stop(self):
        profile = {'VpnType': 0, 'Server': 'vpn.example.test', 'ServerPort': 443, 'User': 'person'}
        for error in (b"Server SSL certificate didn't match", b'OTP challenge required', b'401 Unauthorized'):
            raw = Mock()
            tls = Mock()
            tls.getpeercert.return_value = b'certificate'
            context = Mock()
            context.wrap_socket.return_value.__enter__ = Mock(return_value=tls)
            context.wrap_socket.return_value.__exit__ = Mock(return_value=False)
            raw.__enter__ = Mock(return_value=raw)
            raw.__exit__ = Mock(return_value=False)
            with patch.object(setup.socket, 'create_connection', return_value=raw), \
                 patch.object(setup.ssl, 'SSLContext', return_value=context), \
                 patch.object(setup, 'dialog', side_effect=[b'confirmed', b'password']), \
                 patch.object(setup.subprocess, 'run', side_effect=[
                     Mock(stdout=b'public'), Mock(stdout=b'public-der'),
                     Mock(returncode=0, stdout=b"COOKIE='cookie'\n", stderr=error)]) as run, \
                 patch.object(setup, 'keychain') as keychain:
                with self.assertRaises(setup.SetupError):
                    setup.prepare('test', profile)
                keychain.assert_not_called()
                self.assertNotIn('password', repr(run.call_args.args))
                self.assertEqual(run.call_args.kwargs['input'], b'password\n')

    def test_stop_failure_restores_autostart_without_credentials(self):
        for enabled in (True, False):
            with patch.object(control, 'input_profile', return_value=({'name': 'new'}, {})), \
                 patch.object(setup, 'prepare', return_value=({}, b'new')), \
                 patch.object(control, 'registry', return_value={}), \
                 patch.object(control, 'read_file', return_value=b'old-settings'), \
                 patch.object(setup, 'keychain', return_value=b'old-secret'), \
                 patch.object(control, 'autostart', return_value=enabled), \
                 patch.object(control, 'job', return_value=(True, 10)), \
                 patch.object(control, 'validate_daemon'), \
                 patch.object(control, 'stop', side_effect=control.ControlError('stop failed')) as stop, \
                 patch.object(setup, 'commit') as commit, patch.object(setup, 'restore') as restore, \
                 patch.object(control, 'launch') as launch:
                with self.assertRaisesRegex(control.ControlError, '已恢复原配置'):
                    control.configure('/unused')
                commit.assert_not_called()
                restore.assert_not_called()
                stop.assert_called_once()
                launch.assert_called_once_with(['enable' if enabled else 'disable', control.JOB])

    def test_launch_timeout(self):
        with patch.object(control, 'execute', return_value=Mock(returncode=0)) as execute:
            control.launch(['enable', control.JOB])
            execute.assert_called_once_with(['/bin/launchctl', 'enable', control.JOB], timeout=10)
        with patch.object(control, 'launch'), \
             patch.object(control, 'job', side_effect=[(True, 10), (False, None)]), \
             patch.object(control, 'processes', return_value=[]), \
             patch.object(control, 'execute', return_value=Mock(returncode=0)) as execute:
            control.stop()
            execute.assert_called_once_with(['/bin/launchctl', 'bootout', control.JOB], timeout=35)

    def test_rollback_if_start_fails(self):
        with patch.object(control, 'input_profile', return_value=({'name': 'new'}, {})), \
             patch.object(setup, 'prepare', return_value=({}, b'new')), \
             patch.object(control, 'registry', return_value={}), \
             patch.object(control, 'read_file', return_value=b'old-settings'), \
             patch.object(control, 'write_file'), \
             patch.object(setup, 'keychain', side_effect=[b'old-secret', b'', b'new']), \
             patch.object(control, 'autostart', return_value=True), \
             patch.object(control, 'job', return_value=(True, 10)), \
             patch.object(control, 'validate_daemon'), patch.object(control, 'stop'), \
             patch.object(setup, 'commit'), patch.object(control, 'start', side_effect=OSError()), \
             patch.object(control, 'restore_data') as restore, patch.object(control, 'launch'):
            with self.assertRaises(control.ControlError):
                control.configure('/unused')
            restore.assert_called_once_with({'settings.json': b'old-settings', 'profiles.json': b'old-settings'},
                                            None, b'old-secret', True)


if __name__ == '__main__':
    unittest.main()

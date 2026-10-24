"""Offline multi-profile transactions; secrets and service operations are fakes."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('profile_control', Path(__file__).with_name('control.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
s = c.setup
REAL_START = c.start
REAL_KEYCHAIN = s.keychain
A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
PIN = 'pin-sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='


def connection(server='vpn.example.test'):
    return {'VpnType': 0, 'Server': server, 'ServerPort': 443, 'User': 'person'}


def saved(identity='primary', server='vpn.example.test'):
    return {'profile': 'SN-IDC', 'connection': connection(server), 'servercert': PIN,
            'profile_id': identity, 'credential_account': c.account_for(identity)}


class Profiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module in (c, s, c.connect):
            self.stack.enter_context(patch.object(module, 'BASE', self.base))
        self.enabled = self.loaded = True
        self.secrets = {None: b'primary-secret', c.account_for(A): b'a-secret', c.account_for(B): b'b-secret'}
        self.events = []
        self.keychain = self.stack.enter_context(patch.object(s, 'keychain', side_effect=self.key))
        self.prepare = self.stack.enter_context(patch.object(s, 'prepare', side_effect=self.auth))
        self.stop = self.stack.enter_context(patch.object(c, 'stop', side_effect=self.stop_service))
        self.start = self.stack.enter_context(patch.object(c, 'start', side_effect=self.start_service))
        self.stack.enter_context(patch.object(c, 'autostart', side_effect=lambda: self.enabled))
        self.stack.enter_context(patch.object(c, 'job', side_effect=lambda: (self.loaded, 10 if self.loaded else None)))
        self.stack.enter_context(patch.object(c, 'launch', side_effect=self.launch))
        self.stack.enter_context(patch.object(c, 'validate_daemon'))
        self.stack.enter_context(patch.object(c, 'execute', side_effect=AssertionError('unexpected system operation')))
        self.put('settings.json', saved())

    def put(self, name, value):
        s.atomic_settings(json.dumps(value).encode(), name)

    def get(self, name):
        return json.loads((self.base / name).read_bytes())

    def key(self, action, secret=None, account=None):
        self.events.append(('key', action, account))
        if action == 'read':
            return self.secrets.get(account)
        if action == 'store':
            self.secrets[account] = secret
        elif action == 'delete':
            self.secrets.pop(account, None)
        else:
            self.fail('unknown key action')
        return b''

    def auth(self, name, profile):
        self.events.append(('auth', name))
        return {'profile': name, 'connection': profile, 'servercert': PIN}, b'fresh-secret'

    def stop_service(self):
        self.events.append(('stop',))
        self.loaded = self.enabled = False

    def start_service(self):
        self.events.append(('start',))
        self.loaded = self.enabled = True
        return {'ok': True}

    def launch(self, args):
        if args[0] == 'enable':
            self.enabled = True
        elif args[0] == 'disable':
            self.enabled = False
        elif args[0] == 'bootstrap':
            self.loaded = True
        else:
            self.fail('unexpected service command')

    def payload(self, identity='primary', **changes):
        value = {'id': identity, 'name': 'SN-IDC', 'server': 'vpn.example.test', 'port': 443, 'username': 'person'}
        value.update(changes)
        path = self.base / 'input.json'
        path.write_text(json.dumps(value))
        return path

    def test_migration_cache_and_name_change(self):
        old = saved()
        old.pop('profile_id')
        old.pop('credential_account')
        old['connection']['ServerPort'] = '443'
        self.put('settings.json', old)
        result = c.connect_profile(self.payload(name='Renamed'))
        self.assertTrue(result['ok'])
        self.prepare.assert_not_called()
        self.assertEqual(self.get('settings.json')['profile'], 'Renamed')
        self.assertEqual(self.get('profiles.json')['primary']['credential_account'], None)
        self.assertEqual(os.stat(self.base / 'profiles.json').st_mode & 0o777, 0o600)
        self.assertEqual(self.get('status.json')['profile_id'], 'primary')

    def test_changed_host_authenticates_before_reading_old_secret(self):
        c.connect_profile(self.payload(server='new.example.test'))
        self.prepare.assert_called_once_with('SN-IDC', connection('new.example.test'))
        self.assertEqual(self.events[0], ('auth', 'SN-IDC'))
        self.assertEqual(self.secrets[None], b'fresh-secret')

    def test_cached_profile_isolation_and_reauth(self):
        self.put('profiles.json', {'primary': saved(), A: saved(A), B: saved(B)})
        before = dict(self.secrets)
        c.connect_profile(self.payload(A))
        self.prepare.assert_not_called()
        self.assertEqual(self.secrets, before)
        self.assertEqual(self.get('settings.json')['credential_account'], 'profile-' + A)
        self.assertEqual({event[2] for event in self.events if event[0] == 'key'}, {'profile-' + A})
        c.connect_profile(self.payload(B, reauth=True))
        self.prepare.assert_called_once()
        self.assertEqual(self.secrets['profile-' + A], b'a-secret')
        self.assertEqual(self.secrets['profile-' + B], b'fresh-secret')
        self.assertEqual(self.secrets[None], b'primary-secret')

    def test_auth_failure_changes_nothing(self):
        before = (self.base / 'settings.json').read_bytes()
        self.prepare.side_effect = s.SetupError('certificate')
        with self.assertRaises(s.SetupError):
            c.connect_profile(self.payload(A))
        self.assertEqual((self.base / 'settings.json').read_bytes(), before)
        self.assertFalse((self.base / 'profiles.json').exists())
        self.stop.assert_not_called()
        self.keychain.assert_not_called()

    def test_stop_failure_does_not_write(self):
        before = (self.base / 'settings.json').read_bytes()
        def failure():
            self.enabled = False
            raise c.ControlError('stop failed')
        self.stop.side_effect = failure
        with self.assertRaisesRegex(c.ControlError, '已恢复原配置'):
            c.connect_profile(self.payload(A))
        self.assertTrue(self.enabled)
        self.assertTrue(self.loaded)
        self.assertEqual((self.base / 'settings.json').read_bytes(), before)
        self.assertEqual(self.secrets['profile-' + A], b'a-secret')
        self.assertFalse(any(e[0] == 'key' and e[1] != 'read' for e in self.events))

    def test_start_failure_rolls_back_files_and_target_secret(self):
        before = (self.base / 'settings.json').read_bytes()
        self.start.side_effect = c.ControlError('start failed')
        with self.assertRaisesRegex(c.ControlError, '已恢复原配置'):
            c.connect_profile(self.payload(A))
        self.assertEqual((self.base / 'settings.json').read_bytes(), before)
        self.assertFalse((self.base / 'profiles.json').exists())
        self.assertEqual(self.secrets['profile-' + A], b'a-secret')
        self.assertEqual(self.secrets[None], b'primary-secret')
        self.assertTrue(self.loaded)
        self.assertTrue(self.enabled)
        self.assertEqual(self.get('status.json')['profile_id'], 'primary')

    def test_write_failure_rolls_back_secret(self):
        before = (self.base / 'settings.json').read_bytes()
        original = c.write_file
        failed = False
        def write(name, data):
            nonlocal failed
            if name == 'profiles.json' and not failed:
                failed = True
                raise OSError('disk error')
            original(name, data)
        with patch.object(c, 'write_file', side_effect=write):
            with self.assertRaises(c.ControlError):
                c.connect_profile(self.payload(A))
        self.assertEqual((self.base / 'settings.json').read_bytes(), before)
        self.assertEqual(self.secrets['profile-' + A], b'a-secret')
        self.assertFalse((self.base / 'profiles.json').exists())

    def test_forget_unknown_active_identity_is_blocked_before_keychain(self):
        self.put('profiles.json', {'primary': saved(), A: saved(A)})
        registry_before = (self.base / 'profiles.json').read_bytes()
        path = self.base / 'forget.json'
        path.write_text(json.dumps({'id': A}))
        for malformed, enabled, loaded, pid in (
                (b'{broken', True, True, 10), (b'[]', True, True, None),
                (b'null', False, True, None), (b'{"profile_id":"invalid"}', False, False, 10)):
            self.enabled = enabled
            (self.base / 'settings.json').write_bytes(malformed)
            before_secrets = dict(self.secrets)
            self.keychain.reset_mock()
            with patch.object(c, 'job', return_value=(loaded, pid)), \
                 self.assertRaisesRegex(c.ControlError, '无法确认活动配置'):
                c.forget_profile(path)
            self.keychain.assert_not_called()
            self.stop.assert_not_called()
            self.assertEqual(self.secrets, before_secrets)
            self.assertEqual((self.base / 'settings.json').read_bytes(), malformed)
            self.assertEqual((self.base / 'profiles.json').read_bytes(), registry_before)

    def test_forget_active_refused_and_inactive_isolated(self):
        self.put('profiles.json', {'primary': saved(), A: saved(A)})
        path = self.base / 'forget.json'
        path.write_text('{"id":"primary"}')
        with self.assertRaises(c.ControlError):
            c.forget_profile(path)
        self.keychain.assert_not_called()
        path.write_text(json.dumps({'id': A}))
        self.assertTrue(c.forget_profile(path)['ok'])
        self.assertNotIn('profile-' + A, self.secrets)
        self.assertEqual(self.secrets[None], b'primary-secret')
        self.assertEqual(c.active_id(), 'primary')
        self.stop.assert_not_called()

    def test_forget_stopped_current_clears_active_and_is_idempotent(self):
        self.enabled = self.loaded = False
        path = self.base / 'forget.json'
        path.write_text('{"id":"primary"}')
        self.assertTrue(c.forget_profile(path)['ok'])
        self.assertFalse((self.base / 'settings.json').exists())
        self.assertIsNone(c.active_id())
        self.assertIsNone(self.get('status.json')['profile_id'])
        with patch.object(c, 'launch') as launch:
            with self.assertRaises(c.ControlError):
                REAL_START()
            launch.assert_not_called()
        self.assertTrue(c.forget_profile(path)['ok'])
        self.assertFalse(self.enabled)
        self.assertFalse(self.loaded)

    def test_forget_current_when_enabled_but_unloaded(self):
        self.enabled, self.loaded = True, False
        path = self.base / 'forget.json'
        path.write_text('{"id":"primary"}')
        self.assertTrue(c.forget_profile(path)['ok'])
        self.assertFalse((self.base / 'settings.json').exists())
        self.assertNotIn(None, self.secrets)
        self.stop.assert_called_once()

    def test_forget_write_failure_restores_credential(self):
        self.put('profiles.json', {'primary': saved(), A: saved(A)})
        before = (self.base / 'profiles.json').read_bytes()
        path = self.base / 'forget.json'
        path.write_text(json.dumps({'id': A}))
        original = c.write_file
        calls = 0
        def write(name, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('disk error')
            original(name, data)
        with patch.object(c, 'write_file', side_effect=write):
            with self.assertRaises(c.ControlError):
                c.forget_profile(path)
        self.assertEqual(self.secrets['profile-' + A], b'a-secret')
        self.assertEqual((self.base / 'profiles.json').read_bytes(), before)

    def test_input_id_and_reauth_boundaries(self):
        for identity, changes in [('PRIMARY', {}), (A.upper(), {}), ('../primary', {}),
                                  (A, {'reauth': 'yes'}), (A, {'extra': True})]:
            with self.assertRaises(c.ControlError):
                c.connect_profile(self.payload(identity, **changes))
        self.prepare.assert_not_called()
        self.stop.assert_not_called()

    def test_safe_id_nonroot_and_no_secret_metadata(self):
        self.put('settings.json', saved(A))
        c.publish('connecting')
        record = self.get('status.json')
        self.assertEqual(record['profile_id'], A)
        self.assertEqual(set(record), {'profile_id', 'state', 'time', 'error'})
        with patch.object(Path, 'read_bytes', side_effect=PermissionError):
            self.assertEqual(c.active_id(record), A)
            self.assertEqual(c.active_id({}), 'primary')
            self.assertIsNone(c.active_id({'profile_id': None}))
        (self.base / 'settings.json').unlink()
        c.publish('stopped')
        self.assertIsNone(self.get('status.json')['profile_id'])

    def test_forget_stop_failure_leaves_credentials_unchanged(self):
        self.enabled = self.loaded = False
        path = self.base / 'forget.json'
        path.write_text('{"id":"primary"}')
        self.stop.side_effect = c.ControlError('stop failed')
        before = dict(self.secrets)
        with self.assertRaises(c.ControlError):
            c.forget_profile(path)
        self.assertEqual(self.secrets, before)
        self.assertTrue((self.base / 'settings.json').exists())
        self.assertFalse(any(e[0] == 'key' and e[1] != 'read' for e in self.events))

    def test_helpers_cannot_cross_accounts(self):
        legacy = c.connect.credential_command(saved())
        named = c.connect.credential_command(saved(A))
        self.assertEqual(Path(legacy[0]).name, 'vpn-keychain')
        self.assertEqual(named[1:], ['--read', 'profile-' + A])
        self.assertEqual(Path(named[0]).name, 'vpn-profile-keychain')
        mismatch = dict(saved(A), credential_account='profile-' + B)
        with self.assertRaises(ValueError):
            c.connect.credential_command(mismatch)
        with patch.object(s.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'fake')) as run:
            REAL_KEYCHAIN('read', account='profile-' + A)
            self.assertEqual(run.call_args.args[0][-1], 'profile-' + A)


if __name__ == '__main__':
    unittest.main()

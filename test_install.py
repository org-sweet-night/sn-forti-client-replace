import os
from pathlib import Path
import plistlib
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import install


CANONICAL_DAEMON = install.DAEMON
CANONICAL_TARGET = install.TARGET


class InitializeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'; self.source.mkdir()
        self.target = root / 'installed'
        self.daemon = self.target / (install.LABEL + '.plist')
        for name in ('connect.py', 'setup.py', 'control.py', 'vpn-keychain', 'vpn-profile-keychain', 'vpnc-script'):
            (self.source / name).write_text(name)
        (self.source / 'runtime').mkdir()
        (self.source / 'runtime/openconnect').write_text('runtime')
        for key, value in dict(SOURCE=self.source, TARGET=self.target, DAEMON=self.daemon,
                               LEGACY_DAEMON=root / 'LaunchDaemons' / (install.LABEL + '.plist')).items():
            p = patch.object(install, key, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'SUDO_UID': str(os.getuid())}); p.start(); self.addCleanup(p.stop)

    def test_initializes_idle_runtime_without_forticlient_or_vpn_actions(self):
        with patch.object(install.os, 'geteuid', return_value=0), patch.object(sys, 'argv',
                ['install.py', '--initialize']), patch.object(install.subprocess, 'run') as run:
            self.assertEqual(install.main(), 0)
        run.assert_called_once_with([str(self.source / 'runtime/openconnect'), '--version'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.assertFalse((self.target / 'settings.json').exists())
        self.assertEqual((self.target / 'control.lock').stat().st_mode & 0o777, 0o600)
        self.assertEqual(plistlib.loads(self.daemon.read_bytes())['ProgramArguments'],
                         ['/usr/bin/python3', '-I', str(self.target / 'connect.py')])
        self.assertTrue((self.target / 'runtime/openconnect').is_file())

    def test_positional_import_bootstraps_only_after_setup_and_has_lock(self):
        events = []
        def run(args, **kwargs):
            if args[0] == str(self.source / 'runtime/openconnect'):
                events.append('version')
            elif args[0] == '/usr/bin/python3':
                events.append('setup')
                self.assertEqual(args[-1], 'imported-profile')
                self.assertTrue((self.target / 'control.lock').is_file())
                self.assertTrue(self.daemon.is_file())
                return subprocess.CompletedProcess(args, 0)
            elif args[:2] == ['/bin/launchctl', 'bootstrap']:
                events.append('bootstrap')
                self.assertTrue((self.target / 'control.lock').is_file())
            return subprocess.CompletedProcess(args, 0)
        with patch.object(install.os, 'geteuid', return_value=0), patch.object(sys, 'argv',
                ['install.py', 'imported-profile']), patch.object(install.subprocess, 'run', side_effect=run):
            self.assertEqual(install.main(), 0)
        self.assertEqual(events, ['version', 'setup', 'bootstrap'])

    def test_positional_setup_failure_keeps_initialized_idle_runtime(self):
        events = []
        def run(args, **kwargs):
            if args[0] == str(self.source / 'runtime/openconnect'):
                events.append('version')
                return subprocess.CompletedProcess(args, 0)
            if args[0] == '/usr/bin/python3':
                events.append('setup')
                self.assertTrue((self.target / 'control.lock').is_file())
                self.assertTrue(self.daemon.is_file())
                return subprocess.CompletedProcess(args, 1)
            events.append('unexpected')
            return subprocess.CompletedProcess(args, 0)
        with patch.object(install.os, 'geteuid', return_value=0), patch.object(sys, 'argv',
                ['install.py', 'imported-profile']), patch.object(install.subprocess, 'run', side_effect=run):
            self.assertEqual(install.main(), 1)
        self.assertEqual(events, ['version', 'setup'])
        self.assertTrue((self.target / 'control.lock').is_file())
        self.assertTrue(self.daemon.is_file())

    def test_missing_source_or_runtime_failure_leaves_no_installation(self):
        (self.source / 'control.py').unlink()
        with patch.object(install.subprocess, 'run') as run, self.assertRaises(FileNotFoundError):
            install.initialize()
        run.assert_not_called()
        self.assertFalse(self.target.exists())
        (self.source / 'control.py').write_text('control.py')
        with patch.object(install.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'version')):
            with self.assertRaises(subprocess.CalledProcessError): install.initialize()
        self.assertFalse(self.target.exists())

    def test_staging_failure_removes_only_its_temporary_tree(self):
        with patch.object(install.subprocess, 'run'), patch.object(install.shutil, 'copytree',
                                                                  side_effect=OSError('copy failed')):
            with self.assertRaisesRegex(OSError, 'copy failed'): install.initialize()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob('.local-vpn-*')), [])
        self.assertEqual((self.source / 'runtime/openconnect').read_text(), 'runtime')

    def test_existing_installation_is_refused_and_preserved(self):
        self.target.mkdir(); sentinel = self.target / 'sentinel'; sentinel.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'existing installation requires review'):
            install.initialize()
        self.assertEqual(sentinel.read_text(), 'keep')


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.target, self.source = root / 'installed', root / 'source'
        self.target.mkdir(); self.source.mkdir()
        self.legacy = root / 'LaunchDaemons' / (install.LABEL + '.plist')
        self.legacy.parent.mkdir()
        self.daemon = self.target / self.legacy.name
        self.control = self.target / 'control.py'
        for name in ('control.py', 'connect.py', 'setup.py', 'vpn-keychain', 'vpn-profile-keychain', 'vpnc-script'):
            (self.target / name).write_text('old')
        (self.target / 'runtime').mkdir()
        (self.target / 'runtime/openconnect').write_text('runtime')
        (self.target / 'settings.json').write_text('preserved')
        (self.source / 'control.py').write_text('updated')
        self.args = ['/usr/bin/python3', '-I', str(self.target / 'connect.py')]
        self.definition = {'Label': install.LABEL, 'ProgramArguments': self.args,
                           'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}}
        self.legacy.write_bytes(plistlib.dumps(self.definition))
        for key, value in dict(TARGET=self.target, SOURCE=self.source, DAEMON=self.daemon,
                               LEGACY_DAEMON=self.legacy, CONTROL=self.control, ARGS=self.args).items():
            p = patch.object(install, key, value); p.start(); self.addCleanup(p.stop)
        # Temporary files belong to the test user; production still requires root ownership.
        def checked(path):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
                raise RuntimeError('unsafe installed path')
            return info
        p = patch.object(install, 'safe_installed', side_effect=checked); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'SUDO_UID': str(os.getuid())}); p.start(); self.addCleanup(p.stop)

    def test_fresh_path_is_outside_launch_daemons(self):
        self.assertEqual(CANONICAL_DAEMON.parent, CANONICAL_TARGET)

    def test_migration_and_repeat_preserve_state_without_service_calls(self):
        with patch.object(install.subprocess, 'run') as run:
            install.upgrade(); install.upgrade()
        run.assert_not_called()
        self.assertFalse(self.legacy.exists())
        self.assertEqual(plistlib.loads(self.daemon.read_bytes()), self.definition)
        self.assertEqual(self.control.read_text(), 'updated')
        self.assertEqual((self.target / 'settings.json').read_text(), 'preserved')
        self.assertEqual((self.target / 'runtime/openconnect').read_text(), 'runtime')
        self.assertEqual((self.target / 'vpn-keychain').read_text(), 'old')

    def test_unsafe_or_foreign_files_are_refused(self):
        for name in ('symlink', 'permissions', 'foreign'):
            with self.subTest(name=name):
                if self.legacy.is_symlink(): self.legacy.unlink()
                self.legacy.write_bytes(plistlib.dumps(self.definition)); self.legacy.chmod(0o644)
                if name == 'symlink':
                    self.legacy.unlink(); self.legacy.symlink_to(self.control)
                elif name == 'permissions': self.legacy.chmod(0o666)
                else: self.legacy.write_bytes(plistlib.dumps({'Label': 'foreign', 'ProgramArguments': self.args}))
                with self.assertRaises(RuntimeError): install.upgrade()
                self.assertEqual(self.control.read_text(), 'old')
                self.assertFalse(self.daemon.exists())

    def test_unsafe_source_is_refused(self):
        source = self.source / 'control.py'
        source.chmod(0o666)
        with self.assertRaises(RuntimeError): install.upgrade()
        source.chmod(0o644); source.unlink(); source.symlink_to(self.control)
        with self.assertRaises(RuntimeError): install.upgrade()
        self.assertTrue(self.legacy.exists())
        self.assertEqual(self.control.read_text(), 'old')

    def test_control_write_failure_preserves_legacy(self):
        copy = install.atomic_copy
        def fail(source, destination, mode):
            if destination == self.control: raise OSError('simulated write failure')
            copy(source, destination, mode)
        with patch.object(install, 'atomic_copy', side_effect=fail):
            with self.assertRaises(OSError): install.upgrade()
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.daemon.exists())
        self.assertEqual(self.control.read_text(), 'old')

    def test_identical_pending_descriptor_recovers(self):
        self.daemon.write_bytes(self.legacy.read_bytes())
        install.upgrade()
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.control.read_text(), 'updated')

    def test_conflicting_pending_descriptor_refused(self):
        value = dict(self.definition, ThrottleInterval=1)
        self.daemon.write_bytes(plistlib.dumps(value))
        with self.assertRaises(RuntimeError): install.upgrade()
        self.assertTrue(self.legacy.exists())
        self.assertEqual(self.control.read_text(), 'old')


class HelperInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = root / 'Applications' / 'SN VPN.app'
        self.app.mkdir(parents=True)
        self.app = self.app.resolve()
        self.app_helper = self.app / install.APP_HELPER
        self.app_helper.parent.mkdir(parents=True)
        self.app_helper.write_bytes(b'signed helper')
        self.target = root / 'installed'; self.target.mkdir()
        (self.target / 'control.lock').touch()
        self.launch = root / 'LaunchDaemons'; self.launch.mkdir()
        self.helper = self.target / 'vpn-helper'
        self.policy = self.target / 'helper-policy.plist'
        self.daemon = self.launch / (install.HELPER_LABEL + '.plist')
        for key, value in dict(APP_PATH=self.app, TARGET=self.target, HELPER=self.helper,
                               HELPER_POLICY=self.policy, HELPER_DAEMON=self.daemon).items():
            p = patch.object(install, key, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'SUDO_UID': str(os.getuid())}); p.start(); self.addCleanup(p.stop)
        p = patch.object(install, 'safe_installed', side_effect=lambda path: path.lstat()); p.start(); self.addCleanup(p.stop)
        self.calls = []
        def run(args, **kwargs):
            self.calls.append(args)
            if args[0] == '/usr/bin/codesign':
                if args[1] == '--verify':
                    return subprocess.CompletedProcess(args, 0)
                if args[1] == '-dv':
                    identifier = ('local.snvpn.desktop' if args[-1] == str(self.app)
                                  else install.HELPER_LABEL)
                    details = getattr(self, 'signature_details', {}).get(
                        identifier, f'Identifier={identifier}\nCDHash={"ab" * 20}\nflags=0x10000(runtime)\n')
                    return subprocess.CompletedProcess(args, 0, stdout='', stderr=details)
                if args[1] == '-d':
                    encoded = getattr(self, 'entitlement_output', 'Executable=/signed/path')
                    return subprocess.CompletedProcess(args, 0, stdout=encoded, stderr='')
            if args[1:3] == ['-dv', '--verbose=4']:
                raise AssertionError('unexpected codesign invocation')
            if args[1] == 'print':
                return type('Result', (), {'returncode': 0 if self.initially_loaded else 113})()
            if args[1] == 'print-disabled':
                value = getattr(self, 'disabled_value', 'enabled')
                return type('Result', (), {'stdout': ('"' + install.HELPER_LABEL + '" => ' + value).encode()})()
            return type('Result', (), {'returncode': 0})()
        p = patch.object(install.subprocess, 'run', side_effect=run); p.start(); self.addCleanup(p.stop)
        self.initially_loaded = False
        self.run_side_effect = run

    def test_installs_exact_demand_service_and_policy_without_vpn_service_calls(self):
        install.helper_install(str(self.app), '501')
        daemon = plistlib.loads(self.daemon.read_bytes())
        self.assertEqual(daemon, {'Label': install.HELPER_LABEL, 'ProgramArguments': [str(self.helper)],
            'MachServices': {install.HELPER_LABEL: True}, 'ProcessType': 'Interactive', 'Umask': 63,
            'ExitTimeOut': 10, 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'})
        self.assertNotIn('RunAtLoad', daemon); self.assertNotIn('KeepAlive', daemon)
        self.assertEqual(plistlib.loads(self.policy.read_bytes()), {'version': 1, 'userID': 501,
            'clientRequirement': 'identifier "local.snvpn.desktop" and cdhash H"' + 'ab' * 20 + '"'})
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.policy.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.daemon.stat().st_mode), 0o644)
        self.assertTrue(all(install.HELPER_LABEL in call[-1] or call[-1] == str(self.daemon)
                            for call in self.calls if call[1] in ('enable', 'bootstrap')))
        self.assertFalse(any('autoconnect' in str(call) for call in self.calls))

    def test_rejects_bad_uid_path_symlink_and_signature(self):
        for uid in ('0', '-1', 'abc', str(2**32 - 1)):
            with self.subTest(uid=uid), self.assertRaises(RuntimeError): install.helper_install(str(self.app), uid)
        with self.assertRaises(RuntimeError): install.helper_install('/tmp/not-the-app', '501')
        link = self.app.parent / 'linked.app'; link.symlink_to(self.app)
        with patch.object(install, 'APP_PATH', link):
            with self.assertRaises(RuntimeError): install.helper_install(str(link), '501')
        self.app_helper.chmod(0o666)
        with self.assertRaises(RuntimeError): install.helper_install(str(self.app), '501')
        self.app_helper.chmod(0o644)
        for ident, details in (
            ('local.snvpn.desktop', 'Identifier=wrong\nCDHash=' + 'ab' * 20 + '\nflags=0x10000(runtime)'),
            ('local.snvpn.desktop', 'Identifier=local.snvpn.desktop\nCDHash=bad\nflags=0x10000(runtime)'),
            ('local.snvpn.helper', 'Identifier=local.snvpn.helper\nCDHash=' + 'ab' * 20 + '\nflags=0x0'),
        ):
            self.signature_details = {ident: details}
            with self.assertRaises(RuntimeError): install.helper_install(str(self.app), '501')
        self.signature_details = {}
        self.entitlement_output = plistlib.dumps({'get-task-allow': True}).decode()
        with self.assertRaises(RuntimeError): install.helper_install(str(self.app), '501')
        self.assertFalse(self.helper.exists()); self.assertFalse(self.daemon.exists())

    def test_bootstrap_failure_restores_files_and_only_helper_label(self):
        self.helper.write_bytes(b'old helper'); self.helper.chmod(0o755)
        self.policy.write_bytes(b'old policy'); self.policy.chmod(0o640)
        self.daemon.write_bytes(b'old daemon'); self.daemon.chmod(0o600)
        old_run = self.run_side_effect
        def fail_bootstrap(args, **kwargs):
            if args[1] == 'bootstrap': raise subprocess.CalledProcessError(1, args)
            return old_run(args, **kwargs)
        with patch.object(install.subprocess, 'run', side_effect=fail_bootstrap):
            with self.assertRaises(subprocess.CalledProcessError): install.helper_install(str(self.app), '501')
        self.assertEqual(self.helper.read_bytes(), b'old helper')
        self.assertEqual(self.policy.read_bytes(), b'old policy')
        self.assertEqual(self.daemon.read_bytes(), b'old daemon')
        self.assertFalse(any('autoconnect' in str(call) for call in self.calls))

    def test_lock_contention_prevents_launchctl_and_writes(self):
        import fcntl
        with (self.target / 'control.lock').open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError): install.helper_install(str(self.app), '501')
        self.assertFalse(any(call[0] == '/bin/launchctl' for call in self.calls))
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.policy.exists())
        self.assertFalse(self.daemon.exists())

    def test_rollback_restores_loaded_disabled_helper_state(self):
        self.initially_loaded = True
        self.disabled_value = 'true'
        self.daemon.write_bytes(plistlib.dumps({'Label': install.HELPER_LABEL}))
        old_run = self.run_side_effect
        failed = False
        def fail_first_bootstrap(args, **kwargs):
            nonlocal failed
            if args[1] == 'bootstrap' and not failed:
                failed = True
                raise subprocess.CalledProcessError(1, args)
            return old_run(args, **kwargs)
        with patch.object(install.subprocess, 'run', side_effect=fail_first_bootstrap):
            with self.assertRaises(subprocess.CalledProcessError): install.helper_install(str(self.app), '501')
        commands = [call[1] for call in self.calls if call[0] == '/bin/launchctl']
        self.assertLess(commands.index('enable'), commands.index('bootstrap'))
        self.assertGreater(commands.index('disable'), commands.index('bootstrap'))

class HelperSignatureTests(unittest.TestCase):
    def signature(self, details=None, entitlements=''):
        details = details or ('Identifier=local.snvpn.desktop\nCDHash=' + 'ab' * 20 + '\nflags=0x10000(runtime)\n')
        with patch.object(install.subprocess, 'run', side_effect=[
                Mock(), Mock(stderr=details, stdout=''),
                Mock(stderr='Executable=app\n', stdout=entitlements)]):
            return install.code_signature(Path('/Applications/SN VPN.app'), 'local.snvpn.desktop')

    def test_signed_runtime_without_entitlements(self):
        self.assertEqual(self.signature(), 'ab' * 20)

    def test_wrong_identity_hash_or_runtime_is_refused(self):
        valid = 'Identifier=local.snvpn.desktop\nCDHash=' + 'ab' * 20 + '\nflags=0x10000(runtime)\n'
        for details in (valid.replace('desktop', 'other'), valid.replace('ab' * 20, 'invalid'),
                        valid.replace('flags=0x10000(runtime)', 'flags=0x0(none)')):
            with self.subTest(details=details), self.assertRaises(RuntimeError):
                self.signature(details)

    def test_injection_entitlements_and_invalid_document_are_refused(self):
        for key in ('com.apple.security.get-task-allow', 'com.apple.security.cs.disable-library-validation',
                    'com.apple.security.cs.allow-dyld-environment-variables',
                    'com.apple.security.cs.allow-unsigned-executable-memory'):
            value = plistlib.dumps({key: True}).decode()
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.signature(entitlements=value)
        with self.assertRaises(RuntimeError): self.signature(entitlements='invalid data')

    def test_launch_overrides_parse_and_fail_closed(self):
        for value, expected in [('enabled', False), ('disabled', True), ('true', True), ('false', False)]:
            with self.subTest(value=value), patch.object(install.subprocess, 'run', side_effect=[
                    Mock(returncode=0), Mock(stdout=('"local.snvpn.helper" => ' + value).encode())]):
                self.assertEqual(install.helper_launch_state(), (True, expected))
        for raw in (b'"local.snvpn.helper" => unknown',
                    b'"local.snvpn.helper" => true\n"local.snvpn.helper" => false'):
            with patch.object(install.subprocess, 'run', side_effect=[Mock(returncode=0), Mock(stdout=raw)]):
                with self.assertRaises(RuntimeError): install.helper_launch_state()


if __name__ == '__main__':
    unittest.main()

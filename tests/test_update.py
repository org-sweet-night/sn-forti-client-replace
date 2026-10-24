import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.source = root / 'service'; self.source.mkdir()
        self.artifacts = root / 'artifacts'; (self.artifacts / 'runtime').mkdir(parents=True)
        self.source_app = root / 'source.app'; self.source_app.mkdir()
        self.app = root / 'installed.app'; self.app.mkdir()
        self.target = root / 'installed'; self.target.mkdir()
        (self.target / 'control.lock').touch()
        for path, name in ((self.source, 'control.py'), (self.source, 'connect.py'),
                           (self.source, 'setup.py'), (self.source, 'vpnc-script'),
                           (self.artifacts, 'vpn-keychain'), (self.artifacts, 'vpn-profile-keychain')):
            (path / name).write_text(name)
        (self.artifacts / 'runtime/openconnect').write_text('runtime')
        (self.source_app / 'Contents').mkdir()
        (self.source_app / 'Contents/bin').write_text('app')
        (self.source_app / 'Contents/Library/HelperTools').mkdir(parents=True)
        (self.source_app / 'Contents/Library/HelperTools/local.snvpn.helper').write_text('helper')
        for name, path in [('SOURCE', self.source), ('ARTIFACTS', self.artifacts),
                           ('SOURCE_APP', self.source_app), ('APP', self.app)]:
            p = patch.object(update, name, path); p.start(); self.addCleanup(p.stop)
        p = patch.object(update.install, 'TARGET', self.target); p.start(); self.addCleanup(p.stop)
        p = patch.object(update, 'MANIFEST', self.target / 'update-manifest.json'); p.start(); self.addCleanup(p.stop)
        p = patch.object(update.install, 'safe_installed', side_effect=lambda p: p.lstat()); p.start(); self.addCleanup(p.stop)
        original_source_ok = update.source_ok
        p = patch.object(update, 'source_ok', side_effect=lambda path, owners=None: original_source_ok(path, {0, os.getuid()})); p.start(); self.addCleanup(p.stop)
        self.daemon = self.target / 'daemon.plist'
        self.helper = self.target / 'vpn-helper'
        self.policy = self.target / 'helper-policy.plist'
        self.helper_daemon = self.target / 'helper.plist'
        for name, path in [('APP_PATH', self.app), ('DAEMON', self.daemon), ('LEGACY_DAEMON', root / 'legacy.plist'),
                           ('HELPER', self.helper), ('HELPER_POLICY', self.policy),
                           ('HELPER_DAEMON', self.helper_daemon)]:
            p = patch.object(update.install, name, path); p.start(); self.addCleanup(p.stop)
        for name in ('control.py', 'connect.py', 'setup.py', 'vpnc-script', 'vpn-keychain', 'vpn-profile-keychain'):
            (self.target / name).write_text(name)
        (self.target / 'runtime').mkdir(); (self.target / 'runtime/openconnect').write_text('runtime')
        (self.app / 'Contents').mkdir(); (self.app / 'Contents/bin').write_text('app')

    def test_check_matches_all_shipped_files_and_reports_residue_by_name_only(self):
        (self.target / 'setup-status.log').write_text('SECRET MUST NOT PRINT')
        with patch.object(update, 'validate_residue'), patch('sys.stdout') as output:
            self.assertFalse(update.check(501))
            rendered = ''.join(call.args[0] for call in output.write.call_args_list)
        self.assertIn('setup-status.log', rendered)
        self.assertNotIn('SECRET', rendered)

    def test_check_detects_runtime_and_app_mismatch(self):
        (self.target / 'runtime/openconnect').write_text('old')
        (self.app / 'Contents/bin').write_text('old')
        with patch('sys.stdout'):
            self.assertFalse(update.check(501))

    def test_active_vpn_blocks_apply_before_mutation(self):
        with patch.object(update, 'expected', return_value={}), \
             patch.object(update.install, 'code_signature'), \
             patch.object(update.os, 'geteuid', return_value=0), \
             patch.object(update.subprocess, 'run'), \
             patch.object(update, 'vpn_idle', side_effect=RuntimeError('VPN service is active')):
            with self.assertRaisesRegex(RuntimeError, 'VPN service is active'):
                update.apply(501)

    def test_conflicting_legacy_service_definition_is_not_removed(self):
        import plistlib
        self.daemon.write_bytes(plistlib.dumps({'Label': update.install.LABEL,
                                               'ProgramArguments': update.install.ARGS}))
        legacy = update.install.LEGACY_DAEMON
        legacy.write_bytes(plistlib.dumps({'Label': update.install.LABEL,
                                          'ProgramArguments': update.install.ARGS, 'RunAtLoad': True}))
        with self.assertRaisesRegex(RuntimeError, 'conflicting service definitions'):
            update.make_daemon()
        self.assertTrue(legacy.exists())

    def test_isolated_cli_can_import_its_sibling_installer(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'update.py'
        result = subprocess.run([sys.executable, '-IB', str(script), '--invalid'],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usage: update.py', result.stderr)
        self.assertNotIn('ModuleNotFoundError', result.stderr)

    def test_unknown_launchctl_state_fails_closed(self):
        result = subprocess.CompletedProcess([], 5, stdout='', stderr='permission denied')
        with patch.object(update.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'unable to confirm VPN service is idle'):
                update.vpn_idle()

    def test_residue_symlink_is_rejected(self):
        outside = Path(self.temp.name) / 'outside'; outside.write_text('keep')
        link = self.target / update.RESIDUE[0]; link.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'unsafe legacy residue'):
            update.validate_residue(link)

    def test_credential_check_uses_only_validated_account_ids_and_hides_failures(self):
        (self.target / 'settings.json').write_text('{"profile_id":"primary"}')
        identity = '01234567-89ab-cdef-0123-456789abcdef'
        (self.target / 'profiles.json').write_text(json.dumps({identity: {
            'profile_id': identity, 'credential_account': 'profile-' + identity}}))
        profiles = json.loads((self.target / 'profiles.json').read_text())
        profiles['primary'] = {'profile_id': 'primary', 'credential_account': None}
        (self.target / 'profiles.json').write_text(json.dumps(profiles))
        commands = update.credential_commands()
        self.assertEqual(len(commands), 2)
        with patch.object(update.install, 'safe_installed', side_effect=lambda p: p.lstat()), \
             patch.object(update.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, commands[1])):
            with self.assertRaisesRegex(RuntimeError, 'saved VPN credential access failed') as error:
                update.verify_credentials(commands)
        self.assertNotIn(identity, str(error.exception))

    def _apply_patches(self):
        def helper_install(app, uid, already_locked=False):
            (update.install.HELPER).write_bytes((Path(app) / update.install.APP_HELPER).read_bytes())
            (update.install.HELPER_POLICY).write_bytes(__import__('plistlib').dumps({
                'userID': int(uid), 'clientRequirement': 'identifier "local.snvpn.desktop" and cdhash H"' + 'a' * 40 + '"'}))
            (update.install.HELPER_DAEMON).write_bytes(__import__('plistlib').dumps({
                'Label': update.install.HELPER_LABEL, 'ProgramArguments': [str(update.install.HELPER)]}))
        patches = [patch.object(update.os, 'geteuid', return_value=0),
                   patch.object(update.os, 'chown'),
                   patch.object(update.install, 'code_signature', return_value='a' * 40),
                   patch.object(update, 'vpn_idle'), patch.object(update, 'make_daemon',
                       return_value=__import__('plistlib').dumps({'ProgramArguments': update.install.ARGS})),
                   patch.object(update, 'validate_residue'),
                   patch.object(update.install, 'helper_install', side_effect=helper_install),
                   patch.object(update.install, 'helper_launch_state', return_value=(False, False)),
                   patch.object(update, 'run_as_user'),
                   patch.object(update.subprocess, 'run')]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def test_apply_replaces_complete_app_and_runtime_and_preserves_user_data(self):
        self._apply_patches()
        (self.target / 'settings.json').write_text('{"profile_id":"primary"}')
        (self.target / 'app-upgrade-backup').mkdir()
        update.apply(501)
        update.apply(501)
        self.assertEqual((self.app / 'Contents/bin').read_text(), 'app')
        self.assertEqual((self.target / 'runtime/openconnect').read_text(), 'runtime')
        self.assertEqual((self.target / 'settings.json').read_text(), '{"profile_id":"primary"}')
        self.assertFalse((self.target / 'app-upgrade-backup').exists())
        self.assertTrue((self.target / 'update-manifest.json').is_file())

    def test_verified_install_passes_audit_and_manifest_detects_metadata_drift(self):
        self._apply_patches()
        update.apply(501)
        with patch('sys.stdout'):
            self.assertTrue(update.check(501))
        self.policy.write_text('{}')
        with patch('sys.stdout'):
            self.assertFalse(update.check(501))

    def test_nested_symlink_in_legacy_backup_is_rejected_without_changing_it(self):
        self._apply_patches()
        residue = self.target / 'app-upgrade-backup'; residue.mkdir()
        outside = Path(self.temp.name) / 'outside'; outside.write_text('preserve')
        link = residue / 'old'; link.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'unsafe update source'):
            update.apply(501)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), 'preserve')
        self.assertFalse(update.transaction_path().exists())

    def test_cleanup_only_removes_known_residue_and_preserves_components(self):
        self._apply_patches()
        update.apply(501)
        backup = self.target / 'app-upgrade-backup'; backup.mkdir()
        (backup / 'old').write_text('obsolete')
        unknown = self.target / 'unrelated'; unknown.write_text('keep')
        before = update.managed_hashes(update.installed_map())
        with patch.object(update.install, 'validate_plist'), patch('sys.stdout'), \
             patch.object(update, 'vpn_idle', side_effect=AssertionError('cleanup must not stop VPN')):
            update.cleanup(501)
            update.cleanup(501)
        self.assertEqual(update.managed_hashes(update.installed_map()), before)
        self.assertFalse(backup.exists())
        self.assertEqual(unknown.read_text(), 'keep')

    def test_health_failure_rolls_back_runtime_and_app(self):
        self._apply_patches()
        (self.target / 'runtime/openconnect').write_text('old runtime')
        (self.app / 'Contents/bin').write_text('old app')
        with patch.object(update, 'run_as_user', side_effect=RuntimeError('health failed')):
            with self.assertRaisesRegex(RuntimeError, 'health failed'):
                update.apply(501)
        self.assertEqual((self.target / 'runtime/openconnect').read_text(), 'old runtime')
        self.assertEqual((self.app / 'Contents/bin').read_text(), 'old app')
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.policy.exists())

    def _root_owned_stat(self):
        original = Path.lstat
        def root_stat(path):
            fields = list(original(path)); fields[4] = 0
            return os.stat_result(fields)
        return patch.object(Path, 'lstat', root_stat)

    def test_interrupted_upgrade_recovers_original_files(self):
        self._apply_patches()
        (self.target / 'runtime/openconnect').write_text('original runtime')
        (self.app / 'Contents/bin').write_text('original app')
        with patch.object(update, 'run_as_user', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): update.apply(501)
        self.assertTrue(update.transaction_path().exists())
        with self._root_owned_stat(), patch('sys.stdout'):
            update.recover(501)
        self.assertEqual((self.target / 'runtime/openconnect').read_text(), 'original runtime')
        self.assertEqual((self.app / 'Contents/bin').read_text(), 'original app')
        self.assertFalse(update.transaction_path().exists())

    def test_cleanup_failure_keeps_verified_install_and_recovery_only_cleans(self):
        self._apply_patches()
        (self.app / 'Contents/bin').write_text('original app')
        original = update.shutil.rmtree
        def fail_backup_cleanup(path, *args, **kwargs):
            if path == update.transaction_path() / 'backup':
                raise OSError('cleanup interrupted')
            return original(path, *args, **kwargs)
        with patch.object(update.shutil, 'rmtree', side_effect=fail_backup_cleanup):
            with self.assertRaisesRegex(OSError, 'cleanup interrupted'): update.apply(501)
        self.assertEqual((self.app / 'Contents/bin').read_text(), 'app')
        self.assertTrue((update.transaction_path() / 'committed').exists())
        with self._root_owned_stat(), patch('sys.stdout'):
            update.recover(501)
        self.assertEqual((self.app / 'Contents/bin').read_text(), 'app')
        self.assertFalse(update.transaction_path().exists())

    def test_recovery_rejects_malformed_journal_without_mutation(self):
        transaction = update.transaction_path(); transaction.mkdir(mode=0o700)
        (transaction / 'backup').mkdir(mode=0o700)
        (transaction / 'journal.json').write_text('{}')
        managed = self.target / 'control.py'; managed.write_text('current')
        with patch.object(update.os, 'geteuid', return_value=0), \
             patch.object(update.install, 'safe_installed', side_effect=lambda p: p.lstat()), \
             patch.object(update, 'validate_owned_tree'), \
             patch.object(update.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'invalid update journal'):
                update.recover(501)
        run.assert_not_called()
        self.assertEqual(managed.read_text(), 'current')

    def test_recovery_rejects_missing_backup_before_mutation(self):
        transaction = update.transaction_path(); transaction.mkdir(mode=0o700)
        (transaction / 'backup').mkdir(mode=0o700)
        paths = update.managed_paths()
        records = [{'present': False, 'directory': False, 'mode': None} for _ in paths]
        records[0] = {'present': True, 'directory': False, 'mode': 0o644}
        (transaction / 'journal.json').write_text(json.dumps({'count': len(paths), 'saved': records,
                                                               'helper_loaded': False, 'helper_disabled': False}))
        managed = self.target / 'control.py'; managed.write_text('current')
        with patch.object(update.os, 'geteuid', return_value=0), \
             patch.object(update.install, 'safe_installed', side_effect=lambda p: p.lstat()), \
             patch.object(update, 'validate_owned_tree'), \
             patch.object(update.subprocess, 'run') as run:
            with self.assertRaises(FileNotFoundError): update.recover(501)
        run.assert_not_called()
        self.assertEqual(managed.read_text(), 'current')


if __name__ == '__main__':
    unittest.main()

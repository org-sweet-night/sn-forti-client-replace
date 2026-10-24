import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import stat
import warnings
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('sparkle_install', SCRIPTS / 'sparkle-install.py')
sparkle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sparkle)


class SparkleInstallTests(unittest.TestCase):
    def run_install(self, results=(), transaction=()):
        calls = []
        queue = list(results)
        transactions = list(transaction)

        def fake_run(command, **kwargs):
            calls.append(command)
            result = queue.pop(0) if queue else subprocess.CompletedProcess(command, 0)
            if result.stdout is None:
                result.stdout = '{"ok": true}'
            return result

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(sparkle, 'TMPDIR', temporary), \
                patch.object(sparkle, 'safe_root_path'), \
                patch.object(sparkle, 'trusted_uid', return_value=501), \
                patch.object(sparkle, 'validate_release', return_value=Path(temporary) / 'release'), \
                patch.object(sparkle, 'status', return_value=True), \
                patch.object(sparkle, 'run', side_effect=fake_run), \
                patch.object(sparkle, 'transaction_exists', side_effect=lambda: transactions.pop(0) if transactions else False):
            result = sparkle.install(Path(temporary) / 'release.zip')
        return result, calls

    def test_success_stops_applies_checks_and_restores_connection(self):
        result, calls = self.run_install()
        self.assertEqual(result, 0)
        self.assertEqual([call[-2:] for call in calls if call[-2] in ('--apply', '--check')],
                         [['--apply', '501'], ['--check', '501']])
        self.assertEqual([call[-1] for call in calls if call[-1] in ('stop', 'start')], ['stop', 'start'])

    def test_apply_failure_restores_after_updater_rollback(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 1)], [False])
        self.assertNotEqual(result, 0)
        self.assertEqual(calls[-1][-1], 'start')
        self.assertFalse(any('--check' in call for call in calls))

    def test_stop_failure_does_not_apply_and_attempts_restore(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0, '{"ok": false}')])
        self.assertNotEqual(result, 0)
        self.assertFalse(any('--apply' in call for call in calls))
        self.assertEqual(calls[-1][-1], 'start')

    def test_check_failure_reports_failure_and_restores(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 1)])
        self.assertNotEqual(result, 0)
        self.assertEqual(calls[-1][-1], 'start')

    def test_unfinished_transaction_suppresses_restart(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 1)], [True])
        self.assertNotEqual(result, 0)
        self.assertFalse(any(call[-1] == 'start' for call in calls))

    def test_zero_exit_with_false_stop_result_does_not_apply(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0, '{"ok": false}')])
        self.assertNotEqual(result, 0)
        self.assertFalse(any('--apply' in call for call in calls))
        self.assertEqual(sum(call[-1] == 'start' for call in calls), 1)

    def test_failed_restart_is_reported_without_retry(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 0, '{"ok": false}')])
        self.assertNotEqual(result, 0)
        self.assertEqual(sum(call[-1] == 'start' for call in calls), 1)

    def test_rollback_restart_failure_is_reported_without_retry(self):
        result, calls = self.run_install([subprocess.CompletedProcess([], 0),
                                          subprocess.CompletedProcess([], 1),
                                          subprocess.CompletedProcess([], 0, '{"ok": false}')], [False])
        self.assertNotEqual(result, 0)
        self.assertEqual(sum(call[-1] == 'start' for call in calls), 1)

    def test_status_error_fails_closed(self):
        with patch.object(sparkle, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '{"state":"error","profile_id":"must-not-be-reported"}')):
            with self.assertRaises(RuntimeError):
                sparkle.status(501, Path('/managed/control.py'))

    def test_zip_validator_accepts_internal_framework_link(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            root = 'SN-VPN-1.2.3'
            link = (root + '/dist/SN VPN.app/Contents/Helpers/SN VPN Updater.app/'
                    'Contents/Frameworks/Sparkle.framework/Versions/Current')
            with zipfile.ZipFile(archive, 'w') as output:
                symlink = zipfile.ZipInfo(link)
                symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(symlink, 'A')
            sparkle.package_update.validate_zip(archive)

    def test_zip_validator_rejects_framework_escape_and_ancestor_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            root = 'SN-VPN-1.2.3'
            framework = (root + '/dist/SN VPN.app/Contents/Helpers/SN VPN Updater.app/'
                         'Contents/Frameworks/Sparkle.framework')
            def symlink(output, name, target):
                link = zipfile.ZipInfo(name)
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(link, target)

            with zipfile.ZipFile(archive, 'w') as output:
                symlink(output, framework + '/Versions/Current', '../../../../../../outside')
            with self.assertRaises(RuntimeError):
                sparkle.package_update.validate_zip(archive)

            with zipfile.ZipFile(archive, 'w') as output:
                symlink(output, framework + '/Versions/Current', '../../../../../../outside')
                symlink(output, framework + '/Versions/Current/resource', 'A')
            with self.assertRaises(RuntimeError):
                sparkle.package_update.validate_zip(archive)

    def test_zip_validator_rejects_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr('SN-VPN-1.2.3/RELEASE.json', '{}')
                    output.writestr('SN-VPN-1.2.3/RELEASE.json', '{}')
            with self.assertRaises(RuntimeError):
                sparkle.package_update.validate_zip(archive)

    def test_generated_postinstall_preflight_runs_with_root_owned_sources(self):
        script = sparkle.package_update.postinstall_script()
        self.assertIn('case "${3:-}" in /)', script)
        self.assertIn('exec /usr/bin/python3 -IB', script)
        body = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

        def fake_lstat(path):
            mode = stat.S_IFREG | 0o644 if path.suffix in ('.py', '.zip') else stat.S_IFDIR | 0o755
            return os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, 'lstat', fake_lstat), \
                patch.object(sys, 'argv', ['postinstall-preflight', directory]):
            exec(compile(body, '<postinstall-preflight>', 'exec'), {})

    def test_update_never_invokes_credential_or_configuration_commands(self):
        result, calls = self.run_install()
        self.assertEqual(result, 0)
        self.assertFalse(any(any(word in ('configure', 'connect-profile', 'forget-profile') for word in call)
                             for call in calls))


if __name__ == '__main__':
    unittest.main()

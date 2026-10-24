import importlib.util
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reauthorize_update', ROOT / 'scripts/reauthorize-update.py')
reauth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reauth)


class ReauthorizationTests(unittest.TestCase):
    def fake_update(self, directory, fail_precheck=False):
        base = Path(directory)
        artifact = base / 'artifacts/vpn-keychain'
        installed = base / 'target/vpn-keychain'
        tool = base / 'tools/authorization-tool'
        for path in (artifact, installed, tool):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'helper' if path != tool else b'authorization-tool')
        update = types.SimpleNamespace(
            ARTIFACTS=artifact.parent, ROOT=base, SOURCE_UID=501,
            install=types.SimpleNamespace(TARGET=installed.parent,
                                          safe_installed=lambda path: path.lstat()),
            source_ok=lambda path, owners: path.lstat(),
            digest=lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest())
        update.verify_calls = []

        def verify(commands):
            update.verify_calls.append(commands)
            if fail_precheck:
                raise RuntimeError('saved VPN credential access failed')

        def apply(uid):
            update.verify_credentials([(str(installed), '--read')])
            update.verify_credentials([(str(installed), '--read')])

        update.verify_credentials = verify
        update.apply = apply
        update.check = lambda uid: True
        return update, installed

    def test_authorizes_only_between_original_precheck_and_postcheck(self):
        with tempfile.TemporaryDirectory() as directory:
            update, installed = self.fake_update(directory)
            calls = []

            def runner(args, **kwargs):
                calls.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, stderr=b'')

            reauth.apply_with_reauthorization(update, 501, runner)
            self.assertEqual(len(update.verify_calls), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0][1:4], ['--authorize', str(installed), 'primary'])
            self.assertEqual(calls[1][0], (str(installed), '--read'))
            self.assertTrue(all(call[1]['stdin'] == subprocess.DEVNULL and
                                call[1]['stdout'] == subprocess.DEVNULL and
                                call[1]['timeout'] == 180 for call in calls))

    def test_precheck_failure_never_authorizes(self):
        with tempfile.TemporaryDirectory() as directory:
            update, _ = self.fake_update(directory, fail_precheck=True)
            calls = []
            with self.assertRaisesRegex(RuntimeError, 'saved VPN credential access failed'):
                reauth.apply_with_reauthorization(update, 501,
                    lambda *args, **kwargs: calls.append(args))
            self.assertEqual(calls, [])

    def test_read_failure_reports_only_status_not_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            update, _ = self.fake_update(directory)
            calls = 0

            def runner(args, **kwargs):
                nonlocal calls
                calls += 1
                stderr = b'' if calls == 1 else b'OSStatus: -25308 secret=must-not-leak'
                return subprocess.CompletedProcess(args, 0 if calls == 1 else 1, stderr=stderr)

            with self.assertRaisesRegex(RuntimeError, r'OSStatus -25308') as error:
                reauth.apply_with_reauthorization(update, 501, runner)
            self.assertNotIn('must-not-leak', str(error.exception))


class RestoreConnectionTests(unittest.TestCase):
    def run_main(self, failure=None, transaction=False, stop_failure=False, existing=False):
        events = []
        def control(operation):
            events.append(operation)
            if stop_failure and operation == 'stop':
                raise RuntimeError('stop failed')
        def apply(module, uid):
            self.assertEqual(uid, 501)
            events.append('apply-and-check')
            if failure:
                transaction_path.return_value.exists.return_value = transaction
                raise RuntimeError(failure)
        with mock.patch.object(reauth.os, 'geteuid', return_value=0), \
             mock.patch.object(reauth, 'control_action', side_effect=control), \
             mock.patch.object(reauth, 'apply_with_reauthorization', side_effect=apply), \
             mock.patch.object(reauth.update, 'transaction_path') as transaction_path, \
             mock.patch('sys.stdout'):
            transaction_path.return_value.exists.return_value = existing
            transaction_path.return_value.is_symlink.return_value = False
            if failure or stop_failure or existing:
                with self.assertRaises(RuntimeError):
                    reauth.main(['--apply', '501', '--restore-connection'])
            else:
                reauth.main(['--apply', '501', '--restore-connection'])
        return events

    def test_success_starts_only_after_apply_and_check(self):
        self.assertEqual(self.run_main(), ['stop', 'apply-and-check', 'start'])

    def test_failure_without_transaction_restores_previous_connection(self):
        self.assertEqual(self.run_main(failure='update failed'),
                         ['stop', 'apply-and-check', 'start'])

    def test_failure_with_transaction_does_not_start_incomplete_install(self):
        self.assertEqual(self.run_main(failure='recovery required', transaction=True),
                         ['stop', 'apply-and-check'])

    def test_existing_transaction_does_not_stop_connection(self):
        self.assertEqual(self.run_main(existing=True), [])

    def test_nonobject_control_response_is_generic_failure(self):
        for payload in (b'null', b'[]', b'true', b'"value"'):
            with self.subTest(payload=payload), \
                 mock.patch.object(reauth.update.install, 'safe_installed'), \
                 mock.patch.object(reauth.subprocess, 'run', return_value=
                     subprocess.CompletedProcess([], 0, stdout=payload)):
                with self.assertRaisesRegex(RuntimeError, '^VPN maintenance operation failed$'):
                    reauth.control_action('start')

    def test_failed_stop_does_not_apply_or_start(self):
        self.assertEqual(self.run_main(stop_failure=True), ['stop'])

    def test_control_failure_ignores_stderr(self):
        with mock.patch.object(reauth.update.install, 'safe_installed'), \
             mock.patch.object(reauth.subprocess, 'run', return_value=
                 subprocess.CompletedProcess([], 0, stdout=b'{"ok": false}',
                                             stderr=b'secret')) as runner:
            with self.assertRaisesRegex(RuntimeError, '^VPN maintenance operation failed$'):
                reauth.control_action('start')
            self.assertEqual(runner.call_args.kwargs['stderr'], subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main()

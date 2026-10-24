import importlib.util
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

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


if __name__ == '__main__':
    unittest.main()

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UpdateWrapperTests(unittest.TestCase):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'update.sh'

    def test_invalid_arguments_do_not_start_authorization(self):
        result = subprocess.run(['bash', str(self.script), '--invalid'], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--apply|--check|--recover|--cleanup', result.stderr)

    def test_release_override_requires_manifest_before_authorization(self):
        before = set(Path('/private/tmp').glob('snvpn-update.*'))
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash', str(self.script), '--check'], capture_output=True,
                                    env=dict(os.environ, SNVPN_RELEASE_ROOT=directory), timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'RELEASE.json', result.stderr)
        self.assertEqual(set(Path('/private/tmp').glob('snvpn-update.*')) - before, set())

    @unittest.skipIf(os.geteuid() == 0, 'wrapper intentionally refuses root callers')
    def test_restore_requires_explicit_apply_and_reauthorization(self):
        cases = [('--apply', '0', '1'), ('--apply', '1', 'invalid'),
                 ('--check', '1', '1')]
        for mode, reauthorize, restore in cases:
            with self.subTest(mode=mode, reauthorize=reauthorize, restore=restore):
                with tempfile.TemporaryDirectory() as directory:
                    script = Path(directory) / 'scripts' / 'update.sh'
                    script.parent.mkdir()
                    shutil.copyfile(self.script, script)
                    shutil.copyfile(self.script.with_name("branding.py"), script.with_name("branding.py"))
                    (script.parent.parent / "app").mkdir()
                    shutil.copyfile(self.script.parent.parent / "app/brand.json", script.parent.parent / "app/brand.json")
                    result = subprocess.run(['bash', str(script), mode], capture_output=True,
                        env=dict(os.environ, SNVPN_REAUTHORIZE_CREDENTIALS=reauthorize,
                                 SNVPN_RESTORE_CONNECTION=restore), timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('仅支持'.encode(), result.stderr)

    @unittest.skipIf(os.geteuid() == 0, 'wrapper intentionally refuses root callers')
    def test_failed_input_copy_removes_its_staging_directory(self):
        before = set(Path('/private/tmp').glob('snvpn-update.*'))
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'scripts' / 'update.sh'
            script.parent.mkdir()
            shutil.copyfile(self.script, script)
            result = subprocess.run(['bash', str(script), '--check'], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(Path('/private/tmp').glob('snvpn-update.*')) - before, set())

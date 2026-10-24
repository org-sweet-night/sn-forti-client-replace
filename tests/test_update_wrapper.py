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

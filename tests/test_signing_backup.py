import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('signing_backup', ROOT / 'scripts/setup-update-signing.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SigningBackupTests(unittest.TestCase):
    def test_backup_permissions_and_different_existing_key_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / '.ssh/github.release-key'
            config = {'backup_directory': str(directory)}
            def export(arguments, **kwargs):
                self.assertEqual(arguments[-2], '-x')
                Path(arguments[-1]).write_bytes(b'fixture signing backup')
            with patch.object(setup.Path, 'home', return_value=home), patch.object(setup.subprocess, 'run', side_effect=export):
                setup.backup(Path('/tool'), 'account', config, 'public')
                setup.backup(Path('/tool'), 'account', config, 'public')
                private = directory / 'sn-forti-client-replace-update-ed25519.key'
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(private.with_suffix('.pub').stat().st_mode), 0o600)
                private.write_bytes(b'other project key')
                with self.assertRaisesRegex(ValueError, 'preserved'):
                    setup.backup(Path('/tool'), 'account', config, 'public')
                self.assertEqual(private.read_bytes(), b'other project key')

    def test_symlink_directory_is_rejected_before_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.ssh').mkdir(mode=0o700)
            directory = home / '.ssh/github.release-key'
            target = home / 'unrelated'; target.mkdir()
            directory.symlink_to(target)
            with patch.object(setup.Path, 'home', return_value=home), patch.object(setup.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'unsafe signing backup directory'):
                    setup.backup(Path('/tool'), 'account', {'backup_directory': str(directory)}, 'public')
                command.assert_not_called()

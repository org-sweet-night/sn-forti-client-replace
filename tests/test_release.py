import importlib.util
import json
import plistlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_payload_changes_and_extra_files_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'payload'
            payload.write_bytes(b'original')
            manifest = root / 'RELEASE.json'
            manifest.write_text(json.dumps({'files': release.inventory(root)}))
            with patch.object(release, 'check_binaries') as check:
                release.verify(root)
                check.assert_called_once_with(root)
                check.reset_mock()
                payload.write_bytes(b'tampered')
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    release.verify(root)
                check.assert_not_called()
                payload.write_bytes(b'original')
                (root / 'extra').touch()
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    release.verify(root)

    def test_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'payload').write_bytes(b'original')
            (root / 'link').symlink_to('payload')
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                release.inventory(root)

    def test_framework_links_are_manifested_and_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            framework = root / 'dist/SN VPN.app/Contents/Helpers/SN VPN Updater.app/Contents/Frameworks/Sparkle.framework'
            (framework / 'Versions/B').mkdir(parents=True)
            (framework / 'Versions/B/Sparkle').write_bytes(b'framework')
            link = framework / 'Versions/Current'
            link.symlink_to('B')
            (framework / 'Sparkle').symlink_to('Versions/Current/Sparkle')
            files = release.inventory(root)
            self.assertIn(link.relative_to(root).as_posix(), files)
            link.unlink()
            link.symlink_to(root)
            with self.assertRaisesRegex(RuntimeError, 'unsafe symlink'):
                release.inventory(root)

    def test_newer_binary_cannot_be_disguised_by_lower_plist_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contents = root / 'dist/SN VPN.app/Contents'
            contents.mkdir(parents=True)
            (contents / 'Info.plist').write_bytes(plistlib.dumps({'LSMinimumSystemVersion': '15.0'}))
            (root / 'dist/service/runtime').mkdir(parents=True)
            def output(*args):
                return 'arm64' if args[0].endswith('lipo') else 'cmd LC_BUILD_VERSION\nminos 27.0\n'
            with patch.object(release.platform, 'system', return_value='Darwin'), \
                 patch.object(release.platform, 'machine', return_value='arm64'), \
                 patch.object(release.platform, 'mac_ver', return_value=('27.2', (), '')), \
                 patch.object(release, 'run', side_effect=output):
                with self.assertRaisesRegex(RuntimeError, 'incompatible binary minimum OS'):
                    release.check_binaries(root)

import base64
import contextlib
import importlib.util
import json
import io
from pathlib import Path
import plistlib
import tempfile
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load('publish_update', 'publish-update.py')
configure = load('configure_updates', 'configure-updates.py')
setup = load('setup_signing', 'setup-update-signing.py')
packager = load('update_packager', 'package-update.py')


class PublishingTests(unittest.TestCase):
    def test_feed_binds_build_full_package_and_escaped_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'SN-VPN-1.0.12-macOS-arm64.pkg'
            package.write_bytes(b'package')
            data = publish.feed({'version': '1.0.12', 'build': '12', 'minimum_macos': '15.0'},
                                package, 'v1.0.12', 'signature', '<script>bad()</script>')
            item = ET.fromstring(data).find('./channel/item')
            self.assertEqual(item.find('{' + publish.NS + '}version').text, '12')
            self.assertEqual(item.find('{' + publish.NS + '}minimumSystemVersion').text, '15.0.0')
            self.assertEqual(item.find('enclosure').get('{' + publish.NS + '}installationType'), 'package')
            self.assertIn('&lt;script&gt;', item.find('description').text)
            self.assertNotIn('<script>', item.find('description').text)

    def test_package_cannot_be_signed_for_different_archive_or_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, package = root / 'release.zip', root / 'release.pkg'
            version = {'version': '1.0.12', 'build': '12', 'minimum_macos': '15.0', 'architecture': 'arm64'}
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('release/RELEASE.json', json.dumps(version))
                bundle.writestr('release/dist/SN VPN.app/Contents/Info.plist', plistlib.dumps({'SUPublicEDKey': 'public'}))
            package.write_bytes(b'package')
            sidecar = package.with_suffix('.pkg.json')
            with patch.object(packager, 'package', return_value=version), \
                 patch.object(sys, 'argv', ['package-update.py', '--archive', str(archive), '--tag', 'v1.0.12', '--output', str(package)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                packager.main()
            self.assertTrue(sidecar.exists())
            self.assertEqual(publish.metadata(archive, package, 'v1.0.12')[0], version)
            with self.assertRaisesRegex(ValueError, 'binding'):
                publish.metadata(archive, package, 'v1.0.13')
            package.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'binding'):
                publish.metadata(archive, package, 'v1.0.12')

    def test_major_version_reset_keeps_sparkle_comparison_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'update.pkg'; package.write_bytes(b'package')
            version = {'version': '2.0.1', 'bundle_version': '2.0.1', 'build': '1', 'minimum_macos': '15.0'}
            item = ET.fromstring(publish.feed(version, package, 'v2.0.1', 'signature', '')).find('./channel/item')
            self.assertEqual(item.find('{' + publish.NS + '}version').text, '2.0.1')

    def test_test_config_and_private_or_other_feed_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            value = {'feed_url': configure.FEED, 'keychain_account': 'local.snvpn.release-signing',
                     'public_key': base64.b64encode(bytes(32)).decode()}
            path.write_text(json.dumps(value))
            self.assertEqual(configure.configuration(path), value)
            value['feed_url'] += '?token=secret'
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'unexpected update source'):
                configure.configuration(path)

    def test_missing_pinned_key_is_never_silently_rotated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            path.write_text(json.dumps({'keychain_account': 'local.snvpn.release-signing', 'public_key': 'pinned'}))
            with patch.object(setup, 'CONFIG', path), patch.object(setup.subprocess, 'run') as command:
                command.return_value.returncode = 1
                with self.assertRaisesRegex(ValueError, 'restore it'):
                    setup.setup(True)
                self.assertEqual(command.call_count, 1)

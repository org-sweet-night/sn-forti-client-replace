import base64
import contextlib
import importlib.util
import json
import io
from pathlib import Path
import plistlib
import subprocess
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
    def test_publish_draft_safe_creation_resume_and_immutable_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'signed'; output.mkdir()
            archive = root / 'release.zip'; archive.write_bytes(b'archive')
            archive.with_suffix('.zip.sha256').write_text('archive checksum')
            for name in ['update.pkg', 'update.pkg.sha256', 'appcast.xml']:
                (output / name).write_text(name)
            notes = root / 'notes.md'; notes.write_text('notes')
            assets = [archive, archive.with_suffix('.zip.sha256'), *sorted(output.iterdir())]
            identity = {'apiUrl': 'https://api.github.com/repos/' + publish.REPO + '/releases/123',
                        'isDraft': True, 'tagName': 'v1.0.13'}
            remote = {'tag_name': 'v1.0.13', 'draft': True, 'assets': [
                {'name': p.name, 'state': 'uploaded', 'digest': 'sha256:' + publish.digest(p)} for p in assets]}

            for scenario in ['new', 'resume', 'published', 'mismatch', 'partial', 'create_failure', 'unexpected_asset']:
                with self.subTest(scenario=scenario):
                    discovered = dict(identity)
                    if scenario == 'published': discovered['isDraft'] = False
                    lookups = [subprocess.CompletedProcess([], 0, json.dumps(discovered), '')]
                    if scenario in ('new', 'create_failure'):
                        lookups.insert(0, subprocess.CompletedProcess([], 1, '', 'release not found\n'))
                    uploaded = json.loads(json.dumps(remote))
                    if scenario == 'mismatch': uploaded['assets'][0]['digest'] = 'sha256:wrong'
                    if scenario == 'partial': uploaded['assets'].pop()
                    if scenario == 'unexpected_asset': uploaded['assets'].append({'name': 'unexpected', 'state': 'uploaded', 'digest': 'sha256:other'})

                    def command(*args):
                        if args[:3] == ('gh', 'release', 'create') and scenario == 'create_failure':
                            raise subprocess.CalledProcessError(1, args, stderr='upload failed')
                        if args[:2] == ('gh', 'api'):
                            self.assertNotIn('/releases/tags/', args[2])
                            if args[2] == identity['apiUrl']: return json.dumps(uploaded)
                            if args[2].endswith('/releases/latest'):
                                return json.dumps(dict(remote, draft=False))
                            self.fail('unexpected API endpoint')
                        return ''

                    with patch.object(publish.subprocess, 'run', side_effect=lookups) as lookup, \
                         patch.object(publish, 'run', side_effect=command) as operation:
                        if scenario in ('new', 'resume'):
                            publish.publish(output, 'v1.0.13', archive, notes)
                        else:
                            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                                publish.publish(output, 'v1.0.13', archive, notes)
                        writes = [c.args[:3] for c in operation.call_args_list if c.args[:2] == ('gh', 'release')]
                        self.assertEqual(sum(c == ('gh', 'release', 'create') for c in writes), int(scenario in ('new', 'create_failure')))
                        self.assertEqual(sum(c == ('gh', 'release', 'edit') for c in writes), int(scenario in ('new', 'resume')))
                        self.assertFalse(any(c == ('gh', 'release', 'upload') for c in writes))
                        self.assertTrue(all('--json' in c.args[0] for c in lookup.call_args_list))

    def test_draft_lookup_rejects_untrusted_api_url_and_lookup_errors(self):
        bad = {'apiUrl': 'https://evil.test/releases/123', 'isDraft': True, 'tagName': 'v1.0.13'}
        for result in [subprocess.CompletedProcess([], 0, json.dumps(bad), ''),
                       subprocess.CompletedProcess([], 1, '', 'authentication failed')]:
            with patch.object(publish.subprocess, 'run', return_value=result), patch.object(publish, 'run') as operation:
                with self.assertRaises((RuntimeError, ValueError)):
                    publish.release_for_tag('v1.0.13')
                operation.assert_not_called()

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

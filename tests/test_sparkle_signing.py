"""Exercise the pinned upstream signer without creating or accessing a Keychain key."""
import base64
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SIGNER = ROOT / 'dist/sparkle/bin/sign_update'
spec = importlib.util.spec_from_file_location('signing_publish', ROOT / 'scripts/publish-update.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


@unittest.skipUnless(SIGNER.exists() and Path('/usr/bin/swift').exists(), 'prepare the pinned Sparkle SDK on macOS first')
class UpstreamSigningTests(unittest.TestCase):
    def test_real_signer_feed_and_package_roundtrip_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / 'ephemeral-test-key'
            source = root / 'test-key.swift'
            source.write_text('import Foundation\nimport CryptoKit\n'
                              'let key = Curve25519.Signing.PrivateKey()\n'
                              'let data = key.rawRepresentation.base64EncodedData()\n'
                              'FileManager.default.createFile(atPath: CommandLine.arguments[1], contents: data, attributes: [.posixPermissions: 0o600])\n'
                              'print(key.publicKey.rawRepresentation.base64EncodedString())\n')
            public = subprocess.check_output(['/usr/bin/swift', str(source), str(private)], text=True).strip()
            self.assertEqual(len(base64.b64decode(public)), 32)
            (root / 'scripts').mkdir()
            (root / 'scripts/configure-updates.py').write_bytes((ROOT / 'scripts/configure-updates.py').read_bytes())
            (root / 'app').mkdir()
            (root / 'app/update-config.json').write_text(json.dumps({
                'feed_url': 'https://github.com/org-sweet-night/sn-forti-client-replace/releases/latest/download/appcast.xml',
                'keychain_account': 'local.snvpn.release-signing', 'public_key': public}))
            archive, package = root / 'release.zip', root / 'SN-VPN-1.0.12-macOS-arm64.pkg'
            version = {'version': '1.0.12', 'build': '12', 'minimum_macos': '15.0', 'architecture': 'arm64'}
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('release/RELEASE.json', json.dumps(version))
                bundle.writestr('release/dist/SN VPN.app/Contents/Info.plist', plistlib.dumps({'SUPublicEDKey': public}))
            package.write_bytes(b'Signing fixture; never install')
            package.with_suffix('.pkg.json').write_text(json.dumps(dict(version, tag='v1.0.12',
                package_sha256=publish.digest(package), release_zip_sha256=publish.digest(archive))))
            notes = root / 'notes.md'; notes.write_text('Signed update notes\n')
            output = root / 'signed'
            original_run = publish.run
            def run(*args):
                if str(args[0]).endswith('/generate_keys'):
                    return public
                self.assertTrue(str(args[0]).endswith('/sign_update'))
                self.assertEqual(list(args[1:3]), ['--account', 'local.snvpn.release-signing'])
                return original_run(SIGNER, '--ed-key-file', private, *args[3:])
            with patch.object(publish, 'ROOT', root), patch.object(publish, 'run', side_effect=run):
                publish.prepare(archive, package, 'v1.0.12', notes, output)
                # Reviewing and subsequently publishing the same prepared directory must work.
                publish.prepare(archive, package, 'v1.0.12', notes, output)
                feed = output / 'appcast.xml'
                feed.write_bytes(feed.read_bytes().replace(b'Signed update notes', b'Tampered update notes'))
                with self.assertRaisesRegex(ValueError, 'differs'):
                    publish.prepare(archive, package, 'v1.0.12', notes, output)
            signature = original_run(SIGNER, '--ed-key-file', private, '-p', package)
            package.write_bytes(b'tampered payload')
            with self.assertRaises(subprocess.CalledProcessError):
                original_run(SIGNER, '--ed-key-file', private, '--verify', package, signature)

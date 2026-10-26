import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('branding', ROOT / 'scripts/branding.py')
branding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(branding)


class BrandingTests(unittest.TestCase):
    def test_config_is_required_and_has_no_field_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'brand.json'
            with self.assertRaises(FileNotFoundError):
                branding.configuration(path)
            for value in ({}, {'identifier': 'demo'}, {'brand': 'Example'},
                          {'identifier': 'demo', 'brand': 'Example', 'extra': True}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    branding.configuration(path)
            path.write_text('{"identifier":"demo","identifier":"other","brand":"Example"}')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                branding.configuration(path)

    def test_alternate_identity_and_swift_literal_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'brand.json'
            path.write_text(json.dumps({'identifier': 'demo', 'brand': 'Example "VPN" & Company'}))
            value = branding.configuration(path)
            self.assertEqual(value.bundle_id, 'local.demovpn.desktop')
            self.assertEqual(value.helper_label, 'local.demovpn.helper')
            self.assertEqual(value.install_path, '/Applications/DEMO VPN.app')
            self.assertEqual(value.signing_account, 'local.demovpn.release-signing')
            self.assertEqual(value.env_prefix, 'DEMOVPN')
            source = branding.swift_source(value)
            self.assertIn('static let displayName = "Example \\"VPN\\" & Company VPN"', source)
            self.assertIn('static let bundleID = "local.demovpn.desktop"', source)

    def test_invalid_identity_or_brand_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'brand.json'
            for identity, brand in [('Demo', 'Example'), ('../other', 'Example'),
                                    ('demo;echo', 'Example'), ('demo', ''),
                                    ('demo', 'Example\nOther'), ('demo', ' Example')]:
                path.write_text(json.dumps({'identifier': identity, 'brand': brand}))
                with self.subTest(identity=identity, brand=brand), self.assertRaises(ValueError):
                    branding.configuration(path)

    def test_built_config_must_match_source(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            target = app / 'Contents/Resources/brand.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(branding.BRAND.raw))
            branding.check_built(app)
            target.write_text(json.dumps({'identifier': 'different', 'brand': 'Different'}))
            with self.assertRaisesRegex(ValueError, 'differs'):
                branding.check_built(app)


if __name__ == '__main__':
    unittest.main()

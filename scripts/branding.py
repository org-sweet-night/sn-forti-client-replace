#!/usr/bin/python3
"""Validate product identity and generate immutable build inputs."""
import argparse
import json
from pathlib import Path
import re
import shlex
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def configuration(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate branding field')
            result[key] = value
        return result
    value = json.loads(Path(path).read_text(), object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {'identifier', 'brand'}:
        raise ValueError('branding requires identifier and brand')
    identity, brand = value['identifier'], value['brand']
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9]{0,23}', identity):
        raise ValueError('invalid branding identifier')
    if (not isinstance(brand, str) or not brand.strip() or brand != brand.strip()
            or len(brand) > 48 or any(ord(c) < 32 or ord(c) == 127 for c in brand)):
        raise ValueError('invalid brand name')
    app_name = identity.upper() + ' VPN'
    prefix = 'local.' + identity + 'vpn'
    return SimpleNamespace(
        identifier=identity, brand=brand, display_name=brand + ' VPN', app_name=app_name,
        updater_name=app_name + ' Updater', bundle_id=prefix + '.desktop',
        helper_label=prefix + '.helper', updater_id=prefix + '.updater',
        signing_account=prefix + '.release-signing', package_id=prefix + '.managed-update',
        authorization_id=prefix + '.keychain-authorization', env_prefix=identity.upper() + 'VPN',
        build_key=identity.upper() + 'VPNBuildNumber', test_key=identity.upper() + 'VPNUpdateTestConfiguration',
        archive_prefix=identity.upper() + '-VPN', temporary_prefix=identity + 'vpn',
        install_path='/Applications/' + app_name + '.app',
        raw=value)


BRAND = configuration(ROOT / 'app/brand.json')


def swift_source(brand):
    fields = {'identifier': brand.identifier, 'displayName': brand.display_name,
              'appName': brand.app_name, 'updaterName': brand.updater_name,
              'bundleID': brand.bundle_id, 'helperLabel': brand.helper_label,
              'updaterID': brand.updater_id, 'installPath': brand.install_path}
    return 'enum Brand {\n' + ''.join(
        '    static let ' + key + ' = ' + json.dumps(value, ensure_ascii=False) + '\n'
        for key, value in fields.items()) + '}\n'


def check_built(app):
    built = configuration(Path(app) / 'Contents/Resources/brand.json')
    if built.raw != BRAND.raw:
        raise ValueError('built application branding differs from build configuration')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--swift', type=Path)
    action.add_argument('--shell', action='store_true')
    action.add_argument('--check-built', type=Path)
    args = parser.parse_args()
    if args.swift:
        args.swift.write_text(swift_source(BRAND))
    elif args.check_built:
        check_built(args.check_built)
    else:
        values = {'APP_NAME': BRAND.app_name, 'DISPLAY_NAME': BRAND.display_name,
                  'UPDATER_NAME': BRAND.updater_name, 'BUNDLE_ID': BRAND.bundle_id,
                  'HELPER_LABEL': BRAND.helper_label, 'UPDATER_ID': BRAND.updater_id,
                  'BUILD_KEY': BRAND.build_key, 'ENV_PREFIX': BRAND.env_prefix,
                  'TEMPORARY_PREFIX': BRAND.temporary_prefix, 'BRAND_IDENTIFIER': BRAND.identifier}
        print('\n'.join(key + '=' + shlex.quote(value) for key, value in values.items()))

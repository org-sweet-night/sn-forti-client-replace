#!/usr/bin/python3
"""Embed public update trust; private keys stay in the maintainer's Keychain."""
import base64
import json
from pathlib import Path
import plistlib
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from branding import BRAND

ROOT = Path(__file__).resolve().parents[1]
FEED = json.loads((ROOT / 'app/update-config.json').read_text())['feed_url']


def repository(feed):
    url = urlsplit(feed)
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/(?:latest/download|download/[A-Za-z0-9_.-]+)/appcast\.xml', url.path)
    if (url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password
            or url.port or url.query or url.fragment or not match):
        raise ValueError('invalid update source')
    return match[1] + '/' + match[2]


def configuration(path):
    value = json.loads(Path(path).read_text())
    if value.get('feed_url') != FEED:
        raise ValueError('unexpected update source')
    repository(value['feed_url'])
    if 'keychain_account' in value and value['keychain_account'] != BRAND.signing_account:
        raise ValueError('unexpected signing account')
    value['keychain_account'] = BRAND.signing_account
    key = value.get('public_key')
    if key:
        raw = base64.b64decode(key, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode() != key:
            raise ValueError('invalid Ed25519 public key')
    elif key != '':
        raise ValueError('missing public_key')
    return value


def configure(plist_path, config_path, test=False):
    config = configuration(config_path)
    path = Path(plist_path)
    info = plistlib.loads(path.read_bytes())
    info.update(SUFeedURL=config['feed_url'], SUPublicEDKey=config['public_key'],
                SUEnableAutomaticChecks=True, SUAutomaticallyUpdate=False, SUAllowsAutomaticUpdates=False,
                SUEnableSystemProfiling=False, SUVerifyUpdateBeforeExtraction=True, SURequireSignedFeed=True,
                SUSignedFeedFailureExpirationInterval=0)
    if test:
        info[BRAND.test_key] = True
    path.write_bytes(plistlib.dumps(info))
    if not config['public_key']:
        print('Update signing public key is not configured; this local build cannot check or install updates.')


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != '--test'):
        raise SystemExit('usage: configure-updates.py Info.plist CONFIG.json [--test]')
    configure(sys.argv[1], sys.argv[2], len(sys.argv) == 4)

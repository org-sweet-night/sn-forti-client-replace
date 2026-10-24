#!/usr/bin/python3
"""Embed public update trust; private keys stay in the maintainer's Keychain."""
import base64
import json
from pathlib import Path
import plistlib
import sys

ROOT = Path(__file__).resolve().parents[1]
FEED = 'https://github.com/org-sweet-night/sn-forti-client-replace/releases/latest/download/appcast.xml'


def configuration(path):
    value = json.loads(Path(path).read_text())
    if value.get('feed_url') != FEED or value.get('keychain_account') != 'local.snvpn.release-signing':
        raise ValueError('unexpected update source or signing account')
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
        info['SNVPNUpdateTestConfiguration'] = True
    path.write_bytes(plistlib.dumps(info))
    if not config['public_key']:
        print('Update signing public key is not configured; this local build cannot check or install updates.')


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != '--test'):
        raise SystemExit('usage: configure-updates.py Info.plist CONFIG.json [--test]')
    configure(sys.argv[1], sys.argv[2], len(sys.argv) == 4)

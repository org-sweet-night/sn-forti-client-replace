#!/usr/bin/python3
"""Explicit maintainer setup; never export or print the private signing key."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'app/update-config.json'


def setup(create):
    config = json.loads(CONFIG.read_text())
    tool = ROOT / 'dist/sparkle/bin/generate_keys'
    account = 'local.snvpn.release-signing'
    if config.get('keychain_account') != account:
        raise ValueError('unexpected signing account')
    command = [str(tool), '--account', account]
    existing = subprocess.run([*command, '-p'], capture_output=True, text=True)
    if existing.returncode:
        if not create:
            raise ValueError('signing key is not available; explicit --create setup is required')
        if config.get('public_key'):
            raise ValueError('pinned signing key is missing; restore it instead of creating a replacement')
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        existing = subprocess.run([*command, '-p'], check=True, capture_output=True, text=True)
    key = existing.stdout.strip()
    raw = base64.b64decode(key, validate=True)
    if len(raw) != 32 or base64.b64encode(raw).decode() != key:
        raise ValueError('invalid signing public key')
    if config.get('public_key') not in ('', key):
        raise ValueError('refusing to rotate the pinned update trust key')
    if create:
        config['public_key'] = key
        CONFIG.write_text(json.dumps(config, indent=2) + '\n')
        print('Update public key pinned; private key remains in the login Keychain.')
    elif config.get('public_key') != key:
        raise ValueError('signing key has not been pinned; explicit --create setup is required')
    else:
        print('Pinned public key matches the Keychain signing key.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--create', action='store_true', help='explicitly authorize creating and pinning a project signing key')
    setup(parser.parse_args().create)

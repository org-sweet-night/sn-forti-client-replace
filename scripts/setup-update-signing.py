#!/usr/bin/python3
"""Explicit maintainer setup and authorized file backup; never print private key material."""
import argparse
import base64
import json
import hmac
import os
from pathlib import Path
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'app/update-config.json'


def backup(tool, account, config, key):
    # The user-approved directory is a key store, not a key file.
    directory = Path(config.get('backup_directory', '~/.ssh/github.release-key')).expanduser()
    if directory != Path.home() / '.ssh/github.release-key':
        raise ValueError('unexpected signing backup directory')
    ssh = directory.parent
    ssh.mkdir(mode=0o700, exist_ok=True)
    info = ssh.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or ssh.is_symlink() or info.st_mode & 0o022:
        raise ValueError('unsafe SSH directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or directory.is_symlink():
        raise ValueError('unsafe signing backup directory')
    directory.chmod(0o700)
    target = directory / 'sn-forti-client-replace-update-ed25519.key'
    with tempfile.TemporaryDirectory(prefix='.export-', dir=directory) as temporary:
        exported = Path(temporary) / 'private-key'
        subprocess.run([str(tool), '--account', account, '-x', str(exported)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exported.chmod(0o600)
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not hmac.compare_digest(target.read_bytes().strip(), exported.read_bytes().strip()):
                raise ValueError('existing backup differs; preserved without overwriting')
        else:
            os.link(exported, target)
        target.chmod(0o600)
    public = target.with_suffix('.pub')
    data = (key + '\n').encode()
    try:
        descriptor = os.open(public, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = public.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or public.read_bytes() != data:
            raise ValueError('existing public backup differs; preserved without overwriting')
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
    public.chmod(0o600)


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
        backup(tool, account, config, key)
        config['public_key'] = key
        CONFIG.write_text(json.dumps(config, indent=2) + '\n')
        print('Update public key pinned; private key is in the login Keychain and approved backup directory.')
    elif config.get('public_key') != key:
        raise ValueError('signing key has not been pinned; explicit --create setup is required')
    else:
        print('Pinned public key matches the Keychain signing key.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--create', action='store_true', help='explicitly authorize creating, pinning and backing up a project signing key')
    setup(parser.parse_args().create)

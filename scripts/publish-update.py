#!/usr/bin/python3
"""Prepare a signed Sparkle feed from the same artifacts attached to a GitHub Release."""
import argparse
import base64
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from branding import BRAND
_config_spec = importlib.util.spec_from_file_location('update_config', ROOT / 'scripts/configure-updates.py')
_config_module = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_module)
REPO = _config_module.repository(_config_module.FEED)
NS = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
ET.register_namespace('sparkle', NS)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True, stderr=subprocess.PIPE).strip()


def metadata(archive, package, tag):
    if not re.fullmatch(r'[A-Za-z0-9._-]+\.pkg', package.name):
        raise ValueError('invalid package asset name')
    data = json.loads(package.with_suffix(package.suffix + '.json').read_text())
    with zipfile.ZipFile(archive) as bundle:
        names = [n for n in bundle.namelist() if n.endswith('/RELEASE.json') and n.count('/') == 1]
        if len(names) != 1:
            raise ValueError('invalid release manifest')
        release = json.loads(bundle.read(names[0]))
        prefix = names[0][:-len('RELEASE.json')]
        info = plistlib.loads(bundle.read(prefix + 'dist/' + BRAND.app_name + '.app/Contents/Info.plist'))
    for field in ('version', 'build', 'minimum_macos', 'architecture'):
        if data.get(field) != release.get(field):
            raise ValueError('package metadata differs from release: ' + field)
    bundle_version = release.get('bundle_version', release['build'])
    if data.get('bundle_version', data['build']) != bundle_version:
        raise ValueError('package metadata differs from release bundle version')
    if release.get('bundle_version') and (bundle_version != release['version']
            or info.get('CFBundleVersion') != bundle_version or info.get(BRAND.build_key) != release['build']):
        raise ValueError('Sparkle comparison version differs from release app')
    if (not re.fullmatch(r'v[1-9][0-9]*\.[0-9]+\.[0-9]+', tag) or tag != 'v' + release['version']
            or data.get('tag') != tag or not re.fullmatch(r'[1-9][0-9]*', release['build'])
            or release['minimum_macos'] != '15.0' or release['architecture'] != 'arm64'
            or data.get('package_sha256') != digest(package) or data.get('release_zip_sha256') != digest(archive)):
        raise ValueError('package/archive/tag binding failed')
    if info.get(BRAND.test_key) or not info.get('SUPublicEDKey'):
        raise ValueError('cannot publish an unconfigured or test updater')
    return release, info


def feed(version, package, tag, signature, notes):
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = BRAND.display_name + ' Updates'
    ET.SubElement(channel, 'link').text = 'https://github.com/' + REPO + '/releases'
    item = ET.SubElement(channel, 'item')
    ET.SubElement(item, 'title').text = BRAND.display_name + ' ' + version['version']
    ET.SubElement(item, '{' + NS + '}version').text = version.get('bundle_version', version['build'])
    ET.SubElement(item, '{' + NS + '}shortVersionString').text = version['version']
    ET.SubElement(item, '{' + NS + '}minimumSystemVersion').text = version['minimum_macos'] + '.0'
    ET.SubElement(item, '{' + NS + '}hardwareRequirements').text = 'arm64'
    warning = '安装会暂时断开 VPN，更新 App、助手和全部运行组件；需要管理员授权。凭据不兼容会回滚，不会自动改写密码或放宽钥匙串权限。'
    ET.SubElement(item, 'description').text = '<p>' + html.escape(warning) + '</p><pre>' + html.escape(notes) + '</pre>'
    ET.SubElement(item, 'enclosure', {
        'url': 'https://github.com/' + REPO + '/releases/download/' + tag + '/' + package.name,
        'length': str(package.stat().st_size), 'type': 'application/octet-stream',
        '{' + NS + '}edSignature': signature, '{' + NS + '}installationType': 'package',
    })
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True)


def prepare(archive, package, tag, notes, output):
    version, info = metadata(archive, package, tag)
    spec = importlib.util.spec_from_file_location('configure_updates', ROOT / 'scripts/configure-updates.py')
    config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_module)
    config = config_module.configuration(ROOT / 'app/update-config.json')
    key = run(ROOT / 'dist/sparkle/bin/generate_keys', '--account', config['keychain_account'], '-p')
    if not key or key != config['public_key'] or key != info['SUPublicEDKey']:
        raise ValueError('signing key does not match the release app public key')
    if output.exists():
        verify_prepared(output, version, package, tag, notes, config['keychain_account'])
        return version
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.signed-update-', dir=output.parent) as temporary:
        stage = Path(temporary)
        staged_package = stage / package.name
        shutil.copy2(package, staged_package)
        tool = ROOT / 'dist/sparkle/bin/sign_update'
        signature = run(tool, '--account', config['keychain_account'], '-p', staged_package)
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError('invalid package signature')
        run(tool, '--account', config['keychain_account'], '--verify', staged_package, signature)
        appcast = stage / 'appcast.xml'
        appcast.write_bytes(feed(version, staged_package, tag, signature, notes.read_text()))
        run(tool, '--account', config['keychain_account'], appcast)
        run(tool, '--account', config['keychain_account'], '--verify', appcast)
        (stage / (package.name + '.sha256')).write_text(digest(staged_package) + '  ' + package.name + '\n')
        stage.rename(output)
    return version


def verify_prepared(output, version, package, tag, notes, account):
    allowed = {package.name, package.name + '.sha256', 'appcast.xml'}
    if {p.name for p in output.iterdir()} != allowed or any(p.is_symlink() or not p.is_file() for p in output.iterdir()):
        raise ValueError('unexpected files in prepared update')
    staged_package = output / package.name
    if digest(staged_package) != digest(package):
        raise ValueError('prepared package differs from requested package')
    appcast = output / 'appcast.xml'
    actual = ET.fromstring(appcast.read_bytes())
    enclosure = actual.find('./channel/item/enclosure')
    if enclosure is None:
        raise ValueError('prepared update has no package enclosure')
    signature = enclosure.get('{' + NS + '}edSignature', '')
    expected = ET.fromstring(feed(version, package, tag, signature, notes.read_text()))
    def shape(element):
        return element.tag, sorted(element.attrib.items()), (element.text or '').strip(), [shape(child) for child in element]
    if shape(actual) != shape(expected):
        raise ValueError('prepared feed differs from release metadata or notes')
    tool = ROOT / 'dist/sparkle/bin/sign_update'
    run(tool, '--account', account, '--verify', staged_package, signature)
    run(tool, '--account', account, '--verify', appcast)


def release_for_tag(tag):
    # GitHub's /releases/tags endpoint does not reliably find draft releases.
    result = subprocess.run(['gh', 'release', 'view', tag, '--repo', REPO,
                             '--json', 'apiUrl,isDraft,tagName'],
                            capture_output=True, text=True)
    if result.returncode:
        if result.returncode == 1 and result.stderr.strip().lower() == 'release not found':
            return None
        raise RuntimeError('could not inspect release; no release changes attempted')
    value = json.loads(result.stdout)
    if (value.get('tagName') != tag or not isinstance(value.get('isDraft'), bool)
            or not re.fullmatch(r'https://api\.github\.com/repos/' + re.escape(REPO)
                                + r'/releases/[1-9][0-9]*', value.get('apiUrl', ''))):
        raise ValueError('unexpected release identity')
    return value


def verify_assets(remote, tag, expected):
    assets = remote.get('assets', [])
    actual = {asset['name']: asset.get('digest') for asset in assets if asset.get('state') == 'uploaded'}
    if remote.get('tag_name') != tag or len(assets) != len(expected) or actual != expected:
        raise ValueError('uploaded assets do not match; release retained without replacing assets')


def publish(output, tag, archive, notes):
    # Existing drafts are resumable only when their complete immutable asset set matches.
    assets = [archive, archive.with_suffix(archive.suffix + '.sha256'), *sorted(output.iterdir())]
    expected = {path.name: 'sha256:' + digest(path) for path in assets}
    if len(assets) != 5 or len(expected) != 5:
        raise ValueError('expected exactly five distinct release assets')
    existing = release_for_tag(tag)
    if existing is not None and not existing['isDraft']:
        raise ValueError('release already published; publish a new version rather than replacing signed assets')
    if existing is None:
        run('git', '-C', ROOT, 'ls-remote', '--exit-code', 'origin', 'refs/tags/' + tag)
        # On failure, retain any partial draft and let a later explicit invocation inspect it.
        run('gh', 'release', 'create', tag, '--repo', REPO, '--verify-tag', '--draft',
            '--title', BRAND.display_name + ' ' + tag[1:], '--notes-file', notes, *assets)
        existing = release_for_tag(tag)
    if existing is None or not existing['isDraft']:
        raise ValueError('expected draft release; no assets changed')
    remote = json.loads(run('gh', 'api', existing['apiUrl']))
    if remote.get('draft') is not True:
        raise ValueError('release is no longer a draft; no assets changed')
    verify_assets(remote, tag, expected)
    run('gh', 'release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest')
    latest = json.loads(run('gh', 'api', 'repos/' + REPO + '/releases/latest'))
    if latest.get('draft') is not False:
        raise ValueError('published latest release verification failed')
    verify_assets(latest, tag, expected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--notes-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--publish', action='store_true', help='explicitly upload and publish a new GitHub Release')
    args = parser.parse_args()
    prepare(args.archive.resolve(), args.package.resolve(), args.tag, args.notes_file.resolve(), args.output.resolve())
    print('Signed package and appcast prepared; no GitHub writes unless --publish is specified.')
    if args.publish:
        publish(args.output.resolve(), args.tag, args.archive.resolve(), args.notes_file.resolve())
        print('GitHub Release and uploaded asset digests verified.')

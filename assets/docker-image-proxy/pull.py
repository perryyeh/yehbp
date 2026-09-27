#!/usr/bin/env python3
"""Download public Docker Hub images through YehBP's SOCKS5 using curl.

Produce a docker-load-compatible archive; never ask the Docker daemon to pull.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse


def image_reference(ref):
    # Deliberately limit the built-in downloader to public Docker Hub tags.
    if '@' in ref or '://' in ref:
        raise ValueError('only tagged Docker Hub images are supported')
    if ref.startswith('docker.io/'):
        ref = ref[len('docker.io/'):]
    elif ref.startswith('index.docker.io/'):
        ref = ref[len('index.docker.io/'):]
    repo, sep, tag = ref.rpartition(':')
    if not sep or '/' in tag:
        repo, tag = ref, 'latest'
    if '.' in repo.split('/')[0] or ':' in repo.split('/')[0]:
        raise ValueError('non-Docker Hub registry is not supported')
    if '/' not in repo:
        repo = 'library/' + repo
    if not re.fullmatch(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+', repo):
        raise ValueError('invalid Docker Hub repository')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
        raise ValueError('invalid image tag')
    return repo, tag


def verify(path, digest):
    algorithm, expected = digest.split(':', 1)
    if algorithm != 'sha256' or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('unsupported digest')
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise ValueError('registry content digest mismatch: ' + digest)


def pull(proxy, image, output, platform):
    repo, tag = image_reference(image)
    os_name, arch, *variant = platform.split('/')
    if os_name != 'linux' or not arch:
        raise ValueError('unsupported Docker platform: ' + platform)
    base = 'https://registry-1.docker.io/v2/' + repo
    with tempfile.TemporaryDirectory(prefix='.yehbp-image-', dir=str(output.parent)) as temp:
        work = Path(temp)
        auth_file = work / 'auth.conf'

        def fetch(url, dest, authenticated=True):
            # -q ignores ~/.curlrc; --noproxy "" overrides NO_PROXY and
            # --proxy socks5h resolves registry/CDN hosts on the proxy.
            cmd = ['curl', '-q', '-fsSL', '--retry', '2', '--connect-timeout', '10',
                   '--proxy', proxy, '--noproxy', '', '-o', str(dest)]
            if authenticated:
                cmd += ['-K', str(auth_file)]
            subprocess.run(cmd + [url], check=True)

        token_file = work / 'token.json'
        query = urllib.parse.urlencode({'service': 'registry.docker.io',
                                        'scope': 'repository:' + repo + ':pull'})
        fetch('https://auth.docker.io/token?' + query, token_file, False)
        token = json.loads(token_file.read_text()).get('token')
        if not token or not re.fullmatch(r'[A-Za-z0-9._~+/-]+', token):
            raise ValueError('Docker Hub did not return a usable public token')
        auth_file.write_text('header = ' + json.dumps('Authorization: Bearer ' + token) + '\n')
        os.chmod(auth_file, 0o600)
        accept = ('application/vnd.oci.image.index.v1+json, '
                  'application/vnd.docker.distribution.manifest.list.v2+json, '
                  'application/vnd.oci.image.manifest.v1+json, '
                  'application/vnd.docker.distribution.manifest.v2+json')
        with auth_file.open('a') as cfg:
            cfg.write('header = ' + json.dumps('Accept: ' + accept) + '\n')

        manifest_file = work / 'registry-manifest.json'
        fetch(base + '/manifests/' + urllib.parse.quote(tag, safe=''), manifest_file)
        manifest = json.loads(manifest_file.read_text())
        if 'manifests' in manifest:
            matching = [entry for entry in manifest['manifests']
                        if entry.get('platform', {}).get('os') == os_name
                        and entry.get('platform', {}).get('architecture') == arch
                        and (not variant or entry.get('platform', {}).get('variant') == variant[0])]
            if not matching:
                raise ValueError('no matching image platform: ' + platform)
            digest = matching[0]['digest']
            fetch(base + '/manifests/' + digest, manifest_file)
            verify(manifest_file, digest)
            manifest = json.loads(manifest_file.read_text())
        if 'config' not in manifest or 'layers' not in manifest:
            raise ValueError('unsupported image manifest')
        parts = [manifest['config']] + manifest['layers']
        paths = []
        for idx, part in enumerate(parts):
            digest = part['digest']
            path = work / ('part-' + str(idx))
            fetch(base + '/blobs/' + digest, path)
            verify(path, digest)
            paths.append(path)
        config = json.loads(paths[0].read_text())
        if config.get('os') != os_name or config.get('architecture') != arch:
            raise ValueError('image config platform mismatch')
        layer_names = []
        for idx, layer in enumerate(manifest['layers'], 1):
            media = layer.get('mediaType', '')
            if media not in ('application/vnd.oci.image.layer.v1.tar+gzip',
                             'application/vnd.docker.image.rootfs.diff.tar.gzip',
                             'application/vnd.oci.image.layer.v1.tar'):
                raise ValueError('unsupported layer media type: ' + media)
            layer_names.append('layer-' + str(idx) + ('.tar.gz' if media.endswith('gzip') else '.tar'))
        descriptor = [{'Config': 'config.json', 'RepoTags': [image], 'Layers': layer_names}]
        staged = work / 'image.tar'
        with tarfile.open(staged, 'w') as archive:
            data = json.dumps(descriptor).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size = len(data)
            import io
            archive.addfile(info, io.BytesIO(data))
            archive.add(paths[0], arcname='config.json')
            for name, path in zip(layer_names, paths[1:]):
                archive.add(path, arcname=name)
        os.replace(staged, output)


if __name__ == '__main__':
    try:
        pull(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4])
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print('❌ 镜像代理下载失败：' + str(exc), file=sys.stderr)
        sys.exit(1)

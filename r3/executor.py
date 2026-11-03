"""Fixed SITE executor; never executes deployment code from the updated checkout."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import shutil
import tempfile
import time
import urllib.request
from environment import clean_environment, PUBLIC_SITE, require_install

ROOT = Path('/root/personal-site')
INSTALL = Path('/usr/local/lib/gozxiao-site-release')


class ResourceLimit(RuntimeError):
    pass


def check_resources():
    available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    if available < 512 * 1024**2 or shutil.disk_usage(ROOT).free < 4 * 1024**3:
        raise ResourceLimit('minimum memory/disk headroom not met')


def command(args, env):
    # Temp files avoid pipe deadlocks if a terminated CLI leaves descendants.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=output, stderr=errors)
        start = time.monotonic()
        try:
            while p.poll() is None:
                if 'build' in args or 'up' in args:
                    check_resources()
                if time.monotonic() - start > 720:
                    raise TimeoutError('command deadline')
                time.sleep(.25)
        except BaseException:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
            raise
        if p.returncode:
            raise RuntimeError('command failed; private output omitted')
        output.seek(0)
        return output.read().decode('utf-8').strip()


def publish(sha, delivery, run=command, get=None, resource_check=check_resources):
    if not re.fullmatch('[0-9a-f]{40}', sha) or sha == '0' * 40 or not re.fullmatch('[A-Za-z0-9-]{1,100}', delivery):
        raise ValueError('exact SHA and delivery required')
    base_env = clean_environment()
    env = {**clean_environment(site=True), 'RELEASE_TAG': sha}
    git = ['git', '-c', 'core.hooksPath=/dev/null']
    compose = ['docker', 'compose', '-p', 'personal-site', '-f', str(ROOT / 'compose.yaml')]
    receipt = {'status': 'failed', 'target_sha': sha, 'delivery': delivery, 'stage': 'preflight', 'replacement_started': False}
    def call(*args):
        return run(list(args), base_env)
    def g(*args):
        return run(git + list(args), base_env)
    try:
        resource_check()
        if g('branch', '--show-current') != 'main' or g('status', '--porcelain'):
            raise RuntimeError('clean main required')
        receipt['stage'] = 'fetch'
        g('fetch', '--no-tags', 'origin', 'main')
        fetched = g('rev-parse', 'FETCH_HEAD')
        if fetched != sha:
            return {**receipt, 'status': 'superseded', 'observed_main': fetched, 'reason': 'target not current main; no substitute deployed'}
        g('merge-base', '--is-ancestor', 'HEAD', sha)
        g('merge', '--ff-only', sha)
        receipt['checkout_sha'] = g('rev-parse', 'HEAD')
        if receipt['checkout_sha'] != sha or g('status', '--porcelain'):
            raise RuntimeError('checkout changed')
        model = json.loads(run(compose + ['config', '--format', 'json'], env))
        if model.get('name') != 'personal-site' or set(model['services']) != {'backend', 'web'}:
            raise RuntimeError('site-only compose required')
        if model.get('volumes', {}).get('site-data', {}).get('name') != 'personal-site_site-data':
            raise RuntimeError('original site volume required')
        call('docker', 'volume', 'inspect', 'personal-site_site-data')
        for name in ('backend', 'web'):
            s = model['services'][name]
            if s.get('image') != 'personal-site-' + name + ':' + sha or s.get('post_start') or s.get('pre_stop'):
                raise RuntimeError('unexpected image or lifecycle hook')
        receipt['stage'] = 'baseline'
        resource_check()
        receipt['baseline'] = {}
        for name in ('backend', 'web'):
            cid = run(compose + ['ps', '-q', name], env)
            manifest = json.loads(call('docker', 'inspect', '--format', '{{json .ImageManifestDescriptor}}', cid))['digest']
            reference = call('docker', 'inspect', '--format', '{{.Config.Image}}', cid)
            index = call('docker', 'image', 'inspect', '--format', '{{.Id}}', reference)
            if call('docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', index) != manifest:
                raise RuntimeError('baseline reference no longer matches running container')
            tag = 'personal-site-' + name + ':baseline-' + delivery
            call('docker', 'image', 'tag', index, tag)
            receipt['baseline'][name] = {'index': index, 'manifest': manifest, 'tag': tag, 'source_sha': None}
        receipt['stage'] = 'build'
        resource_check()
        # Compose build must not read runtime secrets from the site's .env.
        build_env = {**base_env, 'RELEASE_TAG': sha}
        build_env.update({k: str(v) for k, v in model['services']['web'].get('build', {}).get('args', {}).items() if k in PUBLIC_SITE and v is not None})
        run(compose + ['--env-file', os.devnull, 'build', 'backend', 'web'], build_env)
        receipt['built'] = {}
        for name in ('backend', 'web'):
            receipt['built'][name] = call('docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{.Id}}', 'personal-site-' + name + ':' + sha)
        if g('rev-parse', 'HEAD') != sha or g('status', '--porcelain'):
            raise RuntimeError('checkout changed during build')
        receipt['stage'] = 'replace'
        resource_check()
        receipt['replacement_started'] = True
        run(compose + ['up', '-d', '--no-build', '--wait', '--wait-timeout', '120', 'backend', 'web'], env)
        receipt['stage'] = 'verify'
        receipt['running'] = {}
        for name in ('backend', 'web'):
            cid = run(compose + ['ps', '-q', name], env)
            actual = json.loads(call('docker', 'inspect', '--format', '{{json .ImageManifestDescriptor}}', cid))['digest']
            if actual != receipt['built'][name]:
                raise RuntimeError('running manifest mismatch')
            receipt['running'][name] = {'container': cid, 'manifest': actual}
        backend = receipt['running']['backend']['container']
        mounts = json.loads(call('docker', 'inspect', '--format', '{{json .Mounts}}', backend))
        if not any(m.get('Name') == 'personal-site_site-data' and m.get('Destination') == '/data' for m in mounts):
            raise RuntimeError('running volume mismatch')
        call('docker', 'exec', backend, 'python', '-c', "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=10).status==200")
        get = get or (lambda url: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10).read())
        body = get('http://127.0.0.1:8080/')
        expected = call('docker', 'exec', receipt['running']['web']['container'], 'cat', '/usr/share/nginx/html/index.html').encode()
        if body.strip() != expected.strip():
            raise RuntimeError('frontend differs from running image')
        assets = re.findall(rb'(?:src|href)=["\x27](/assets/[^"\x27]+\.(?:js|css))["\x27]', body)
        if not assets:
            raise RuntimeError('frontend assets absent')
        receipt['asset_evidence'] = []
        for asset in assets[:4]:
            if not re.fullmatch(rb'/assets/[A-Za-z0-9_.-]+', asset):
                raise RuntimeError('frontend asset unavailable')
            served = get('http://127.0.0.1:8080' + asset.decode())
            built = call('docker', 'exec', receipt['running']['web']['container'], 'cat', '/usr/share/nginx/html' + asset.decode()).encode()
            if not served or served.strip() != built.strip():
                raise RuntimeError('frontend asset mismatch')
            receipt['asset_evidence'].append({'path': asset.decode(), 'sha256': hashlib.sha256(served).hexdigest()})
        if get('https://gozxiao.win/').strip() != body.strip():
            raise RuntimeError('public frontend stale or inaccessible')
        for item in receipt['asset_evidence']:
            if hashlib.sha256(get('https://gozxiao.win' + item['path'])).hexdigest() != item['sha256']:
                raise RuntimeError('public asset mismatch')
        receipt.update(status='ready', backend_health=True, public_frontend=True, frontend_sha256=hashlib.sha256(body).hexdigest(), assets_checked=min(4, len(assets)))
    except Exception as e:
        status = ('uncertain' if receipt['stage'] in ('replace', 'verify') or receipt['replacement_started']
                  else 'blocked_resources' if isinstance(e, ResourceLimit) else 'failed')
        receipt.update(status=status, reason=type(e).__name__, replacement_may_have_occurred=receipt['replacement_started'])
    return receipt


def main():
    require_install(INSTALL, __file__)
    import fcntl
    with (ROOT / '.git/deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(publish(*sys.argv[1:])))


if __name__ == '__main__':
    main()

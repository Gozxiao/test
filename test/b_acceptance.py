#!/usr/bin/env python3
"""Disposable GitHub-hosted runner: real R3 Docker and systemd acceptance.
Never run on a business host, private runner, or production Docker daemon.
All repositories, containers, keys, certificates and volumes are synthetic.
"""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

SRC = Path(__file__).resolve().parents[1]
ROOT = Path('/root/personal-site')
INSTALL = Path('/usr/local/lib/gozxiao-site-release')
STATE = Path('/var/lib/personal-site-deploy')
UNIT = 'r3-synthetic-webhook.service'
ORIGIN = Path('/tmp/r3b-origin.git')
AUTHOR = Path('/tmp/r3b-author')
CERTDIR = Path('/tmp/r3b-tls')
SECRET = 'synthetic-hmac-ci-only-nonproduction'
SHARE_SECRET = 'synthetic-share-ci-only-nonproduction'
VOLUME = 'personal-site_site-data'
STEPS=[]


def run(*args,cwd=None,env=None,timeout=200):
    out=subprocess.run(list(args),cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if out.returncode:
        raise RuntimeError(f'command failed: {args[:3]} (rc={out.returncode}; stderr={out.stderr[-700:]})')
    return out.stdout.strip()


def record(key,**fields):
    item={'step':key,'result':'PASS',**fields};STEPS.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)


def failsafe():
    # GitHub hosted ephemeral runner only. Do not accept an arbitrary self-hosted runner.
    assert os.getuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true', 'GitHub root runner required'
    labels = json.loads(os.environ.get('RUNNER_LABELS_JSON','[]'))
    assert 'ubuntu-24.04' in labels, 'Expected explicit GitHub-hosted Ubuntu image'
    assert Path('/proc/1/comm').read_text().strip()=='systemd', 'PID1 must be real systemd'
    assert os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted', 'No self-hosted daemon allowed'
    for p in (ROOT,INSTALL,STATE,ORIGIN,AUTHOR,CERTDIR):
        assert not p.exists(), f'nonempty/foreign path forbidden: {p}'
    assert not Path('/tmp/r3b-stale-public').exists()
    assert not run('docker','volume','ls','-q','--filter',f'name=^{VOLUME}$')
    assert run('docker','info','--format','{{.OSType}}')=='linux'
    assert int(run('id','-u'))==0
    assert run('docker','info','--format','{{.Name}}')
    record('isolated-runner',pid1='systemd',docker_root=run('docker','info','--format','{{.DockerRootDir}}'))


def site_files(version, path):
    (path/'backend').mkdir(parents=True,exist_ok=True)
    (path/'web'/'assets').mkdir(parents=True,exist_ok=True)
    (path/'backend'/'Dockerfile').write_text('FROM python:3.12-alpine\nARG RELEASE_TAG\nLABEL ci.synthetic_sha=$RELEASE_TAG\nCOPY app.py /app.py\nCMD ["python","/app.py"]\n')
    (path/'backend'/'app.py').write_text('''from http.server import BaseHTTPRequestHandler,HTTPServer
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path!='/api/health':self.send_error(404);return
  self.send_response(200);self.end_headers();self.wfile.write(b'ok')
HTTPServer(('0.0.0.0',8000),H).serve_forever()
''')
    (path/'web'/'Dockerfile').write_text('FROM nginx:alpine\nARG RELEASE_TAG\nLABEL ci.synthetic_sha=$RELEASE_TAG\nCOPY index.html /usr/share/nginx/html/index.html\nCOPY assets/ /usr/share/nginx/html/assets/\n')
    (path/'web'/'index.html').write_text(f'<!doctype html><html><head><title>synthetic {version}</title></head><body><h1>R3 {version}</h1><script src="/assets/app.js"></script></body></html>\n')
    (path/'web'/'assets'/'app.js').write_text(f'window.R3_SYNTHETIC_VERSION={version!r};\n')
    (path/'compose.yaml').write_text('''name: personal-site
services:
  backend:
    image: personal-site-backend:${RELEASE_TAG:-baseline}
    build:
      context: ./backend
      args:
        RELEASE_TAG: ${RELEASE_TAG:-baseline}
    volumes:
      - site-data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).status==200"]
      interval: 2s
      retries: 15
  web:
    image: personal-site-web:${RELEASE_TAG:-baseline}
    build:
      context: ./web
      args:
        RELEASE_TAG: ${RELEASE_TAG:-baseline}
    ports:
      - "127.0.0.1:8080:80"
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/"]
      interval: 2s
      retries: 15
volumes:
  site-data:
    name: personal-site_site-data
    external: true
''')
    (path/'ops'/'release').mkdir(parents=True,exist_ok=True)
    shutil.copyfile(SRC/'r3'/'rollback-site.sh',path/'ops'/'release'/'rollback-site.sh')
    (path/'ops'/'release'/'rollback-site.sh').chmod(0o755)
    (path/'.gitignore').write_text('.env\n')
    (path/'.env').write_text('ADMIN_PASSWORD=synthetic-admin-not-production\n')


def commit(version,repo):
    site_files(version,repo)
    run('git','add','.',cwd=repo)
    run('git','commit','-qm',f'synthetic release {version}',cwd=repo)
    sha=run('git','rev-parse','HEAD',cwd=repo)
    run('git','push','origin','main',cwd=repo)
    return sha


def provision():
    run('git','init','--bare',str(ORIGIN))
    run('git','init','-b','main',str(ROOT))
    run('git','config','user.email','ci@example.invalid',cwd=ROOT)
    run('git','config','user.name','R3 Synthetic CI',cwd=ROOT)
    run('git','remote','add','origin',str(ORIGIN),cwd=ROOT)
    sha=commit('alpha',ROOT)
    run('git','--git-dir',str(ORIGIN),'symbolic-ref','HEAD','refs/heads/main')
    run('git','clone',str(ORIGIN),str(AUTHOR))
    run('git','config','user.email','ci@example.invalid',cwd=AUTHOR)
    run('git','config','user.name','R3 Synthetic CI',cwd=AUTHOR)
    run('docker','volume','create',VOLUME)
    env={**os.environ,'RELEASE_TAG':sha}
    run('docker','compose','-p','personal-site','-f',str(ROOT/'compose.yaml'),'build','backend','web',cwd=ROOT,env=env,timeout=600)
    run('docker','compose','-p','personal-site','-f',str(ROOT/'compose.yaml'),'up','-d','--wait','--wait-timeout','120','backend','web',cwd=ROOT,env=env,timeout=180)
    cid=run('docker','compose','-p','personal-site','-f',str(ROOT/'compose.yaml'),'ps','-q','backend',cwd=ROOT,env=env)
    sentinel='SYNTHETIC-'+hashlib.sha256(os.urandom(16)).hexdigest()
    run('docker','exec',cid,'python','-c',f"from pathlib import Path;Path('/data/r3b-sentinel').write_text({sentinel!r})")
    record('real-baseline-docker',sha=sha,volume=VOLUME,backend=cid[:12]);return sha,sentinel


def tls():
    CERTDIR.mkdir()
    run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(CERTDIR/'ca.key'),'-out',str(CERTDIR/'ca.crt'),'-subj','/CN=R3 Synthetic Acceptance CA')
    run('openssl','req','-newkey','rsa:2048','-nodes','-keyout',str(CERTDIR/'leaf.key'),'-out',str(CERTDIR/'leaf.csr'),'-subj','/CN=gozxiao.win')
    (CERTDIR/'ext').write_text('subjectAltName=DNS:gozxiao.win\nextendedKeyUsage=serverAuth\n')
    run('openssl','x509','-req','-in',str(CERTDIR/'leaf.csr'),'-CA',str(CERTDIR/'ca.crt'),'-CAkey',str(CERTDIR/'ca.key'),'-CAcreateserial','-out',str(CERTDIR/'leaf.crt'),'-days','1','-extfile',str(CERTDIR/'ext'))
    shutil.copy(CERTDIR/'ca.crt','/usr/local/share/ca-certificates/r3b-synthetic.crt')
    run('update-ca-certificates')
    with open('/etc/hosts','a') as f:f.write('\n127.0.0.1 gozxiao.win # R3 SYNTHETIC EPHEMERAL CI ONLY\n')
    assert socket.gethostbyname('gozxiao.win')=='127.0.0.1'
    with open('/tmp/r3b-proxy.out','w') as f:
        subprocess.Popen(['/usr/bin/python3',str(SRC/'test'/'tls_proxy.py')],stdout=f,stderr=f,start_new_session=True)
    for _ in range(40):
        try:
            req=urllib.request.build_opener(urllib.request.ProxyHandler({})).open('https://gozxiao.win/',timeout=2)
            assert b'synthetic alpha' in req.read()
            record('trusted-synthetic-https',host='gozxiao.win',local_dns='127.0.0.1',real_tls=True);return
        except Exception:time.sleep(.25)
    raise RuntimeError('local synthetic trusted TLS not reachable')


def systemd_start():
    INSTALL.mkdir(parents=True,mode=0o755)
    for n in ['controller.py','executor.py','environment.py']:
        shutil.copyfile(SRC/'r3'/n, INSTALL/n);(INSTALL/n).chmod(0o644)
    STATE.mkdir(mode=0o700)
    Path('/run/r3b-webhook.env').write_text(f'WEBHOOK_SECRET={SECRET}\nWEBHOOK_SECRET_GOZXIAO_GOZXIAO_SHARE={SHARE_SECRET}\nWEBHOOK_PORT=9000\n')
    Path('/run/r3b-webhook.env').chmod(0o600)
    Path('/etc/systemd/system/'+UNIT).write_text(f'''[Unit]
Description=R3 ephemeral synthetic webhook acceptance
After=docker.service
[Service]
Type=simple
EnvironmentFile=/run/r3b-webhook.env
ExecStart=/usr/bin/python3 {INSTALL}/controller.py
Restart=on-failure
RestartSec=1
[Install]
WantedBy=multi-user.target
''')
    run('systemctl','daemon-reload');run('systemctl','start',UNIT)
    for _ in range(40):
        if health_status()==200:record('real-systemd-controller',unit=UNIT);return
        time.sleep(.25)
    raise RuntimeError('systemd controller failed to become ready; inspect journal')


def health_status():
    try:
        c=http.client.HTTPConnection('127.0.0.1',9000,timeout=2);c.request('GET','/healthz');r=c.getresponse();r.read();c.close();return r.status
    except Exception:return 0


def post(sha,delivery):
    body=json.dumps({'repository':{'full_name':'Gozxiao/personal-site'},'ref':'refs/heads/main',
                     'after':sha,'deleted':False,'forced':False},separators=(',',':')).encode()
    headers={'X-GitHub-Event':'push','X-GitHub-Delivery':delivery,
             'X-Hub-Signature-256':'sha256='+hmac.new(SECRET.encode(),body,hashlib.sha256).hexdigest(),
             'Content-Type':'application/json'}
    c=http.client.HTTPConnection('127.0.0.1',9000,timeout=3)
    c.request('POST','/webhook',body,headers)
    r=c.getresponse();content=r.read();c.close();return r.status,json.loads(content)


def rows():
    db=STATE/'receipts.sqlite3'
    if not db.exists():return []
    with sqlite3.connect(str(db)) as c:
        return c.execute('SELECT sha,status,result FROM jobs ORDER BY id').fetchall()


def wait_job(sha,timeout=240):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        found=[x for x in rows() if x[0]==sha]
        if found and found[-1][1] not in ('queued','running'):
            result=json.loads(found[-1][2] or '{}')
            return found[-1][1],result
        time.sleep(.5)
    raise RuntimeError(f'timed out waiting for synthetic target {sha[:12]}')


def sentinel_check(value):
    cid=run('docker','compose','-p','personal-site','-f',str(ROOT/'compose.yaml'),'ps','-q','backend',cwd=ROOT)
    txt=run('docker','exec',cid,'cat','/data/r3b-sentinel')
    assert txt==value,'synthetic site-data sentinel changed'
    return cid


def exercise():
    sha0,sentinel=provision();tls();systemd_start()
    sha1=commit('bravo',AUTHOR)
    status,body=post(sha1,'r3b-bravo')
    assert status==202,(status,body)
    outcome,detail=wait_job(sha1)
    if outcome!='ready':
        print('R3B_FAILURE_RECEIPT='+json.dumps(detail,sort_keys=True),flush=True)
        raise RuntimeError(f'real SHA B failed before successful replacement: {outcome} stage={detail.get("stage")} reason={detail.get("reason")}')
    sentinel_check(sentinel)
    assert health_status()==200
    record('real-docker-sha-publish',old=sha0,new=sha1,server_health=200,volume_sentinel_preserved=True,
           manifest_attested=True,public_test_tls=True)
    # Artificially stale edge is an actual HTTPS response from a trusted local TLS origin,
    # not an executor mock; Docker/Compose and controller are the unmodified R3 runtime.
    Path('/tmp/r3b-stale-public').write_text('only for isolated synthetic B')
    sha2=commit('charlie',AUTHOR)
    status,body=post(sha2,'r3b-charlie');assert status==202,(status,body)
    status,body=post('d'*40,'r3b-next-queued');assert status==202,(status,body)
    outcome,detail=wait_job(sha2)
    if outcome!='uncertain':
        print('R3B_FAILURE_RECEIPT='+json.dumps(detail,sort_keys=True),flush=True)
        raise RuntimeError(f'expected post-replacement uncertain, got {outcome}: {detail.get("stage")}, {detail.get("reason")}')
    assert detail.get('replacement_started') is True and detail.get('target_sha')==sha2
    assert [r[1] for r in rows()]==['ready','uncertain','queued'],rows()
    assert health_status()==503
    status,_=post('e'*40,'r3b-after-failure');assert status==503,status
    sentinel_check(sentinel)
    record('real-replacement-failure-quarantine',uncertain_sha=sha2,queue_next_still='queued',healthz=503,post=503)
    run('systemctl','restart',UNIT)
    for _ in range(30):
        if health_status()==503:break
        time.sleep(.25)
    assert health_status()==503
    assert [r[1] for r in rows()]==['ready','uncertain','queued']
    record('real-systemd-restart-quarantine',healthz=503,queued_not_replayed=True)
    # Real human-style rollback of backend/web only; never delete or swap the volume.
    run('bash',str(ROOT/'ops'/'release'/'rollback-site.sh'),sha1,cwd=ROOT,timeout=180)
    sentinel_check(sentinel)
    html=urllib.request.urlopen('http://127.0.0.1:8080/',timeout=5).read()
    assert b'synthetic bravo' in html
    assert health_status()==503, 'rollback must NOT silently clear uncertain release ledger'
    record('real-image-rollback',rollback_target=sha1,site_data_preserved=True,controller_still_quarantined=True)


def main():
    try:
        failsafe();exercise()
        print('R3_LINUX_B_CORE_PASS: real Docker replacement, real systemd persistence, real rollback, synthetic volume.',flush=True)
    except Exception as e:
        print('R3_LINUX_B_NOT_PASS: '+repr(e),flush=True)
        # Do not hide server journal diagnostics; never output synthetic secrets.
        try:
            print('R3_SYSTEMD_STATUS='+run('systemctl','is-active',UNIT),flush=True)
        except Exception:pass
        raise
    finally:
        Path('/tmp/r3b-result.json').write_text(json.dumps({'core_pass':all(x['result']=='PASS' for x in STEPS) and any(x['step']=='real-image-rollback' for x in STEPS), 'steps':STEPS},indent=2))

if __name__=='__main__':main()

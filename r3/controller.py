"""Fixed host-installed webhook controller; standard library, no repository execution."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from environment import clean_environment, require_install

ROUTES = {'Gozxiao/personal-site': 'WEBHOOK_SECRET',
          'Gozxiao/Gozxiao-Share': 'WEBHOOK_SECRET_GOZXIAO_GOZXIAO_SHARE'}
INSTALL = '/usr/local/lib/gozxiao-site-release'


class Rejected(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def replacement_unconfirmed(result):
    """Only a complete success can release a replacement from quarantine."""
    if not isinstance(result, dict):
        return True
    changed = result.get('stage') in ('replace', 'verify')
    for key in ('replacement_started', 'replacement_may_have_occurred'):
        if key in result and (not isinstance(result[key], bool) or result[key]):
            changed = True
    if result.get('status') != 'ready':
        return changed or (result.get('status') == 'failed'
                           and result.get('stage') not in ('preflight', 'fetch', 'baseline', 'build'))
    built, running = result.get('built'), result.get('running')
    confirmed = (result.get('checkout_sha') == result.get('target_sha')
        and result.get('backend_health') is True and result.get('public_frontend') is True
        and isinstance(result.get('frontend_sha256'), str)
        and re.fullmatch('[0-9a-f]{64}', result['frontend_sha256'])
        and type(result.get('assets_checked')) is int and result['assets_checked'] > 0
        and isinstance(built, dict) and isinstance(running, dict)
        and all(isinstance(built.get(n), str) and bool(built[n])
                and isinstance(running.get(n), dict) and running[n].get('manifest') == built[n]
                for n in ('backend', 'web')))
    return not confirmed


def reconciliation_in(c):
    # ponytail: scan the small release ledger; index a derived flag if it grows materially.
    if c.execute("SELECT 1 FROM jobs WHERE status IN ('interrupted','uncertain','blocked_resources') LIMIT 1").fetchone():
        return True
    # Also recognize persisted R2 receipts without rewriting or deleting them.
    for row in c.execute("SELECT status,result FROM jobs WHERE status IN ('failed','superseded','legacy_unverified','ready')"):
        try:
            result = json.loads(row[1])
            if not isinstance(result, dict) or result.get('status') != row[0] or replacement_unconfirmed(result):
                return True
        except (ValueError, TypeError):
            return True
    return False


class Queue:
    def __init__(self, path, secrets):
        if set(secrets) != set(ROUTES) or any(not s for s in secrets.values()):
            raise ValueError('all whitelisted repository secrets must be nonempty')
        self.secrets = secrets
        self.path = path
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS jobs(
              id INTEGER PRIMARY KEY,repo TEXT,sha TEXT,status TEXT,result TEXT,
              queued REAL,started REAL,ended REAL);
              CREATE TABLE IF NOT EXISTS deliveries(
              delivery TEXT PRIMARY KEY,digest TEXT,job INTEGER);''')
            # A process restart cannot establish whether replacement completed.
            c.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def accept(self, headers, body):
        if not 0 < len(body) <= 1024 * 1024:
            raise Rejected(413, 'bounded body required')
        try:
            v = json.loads(body)
            repo = v['repository']['full_name']
        except (ValueError, KeyError, TypeError):
            raise Rejected(400, 'invalid object')
        if not isinstance(repo, str) or repo not in ROUTES:
            raise Rejected(403, 'repository denied')
        expected = 'sha256=' + hmac.new(self.secrets[repo].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get('X-Hub-Signature-256', ''), expected):
            raise Rejected(403, 'signature denied')
        if headers.get('X-GitHub-Event') != 'push' or v.get('ref') != 'refs/heads/main':
            return 204, {'status': 'ignored'}
        sha, delivery = v.get('after'), headers.get('X-GitHub-Delivery', '')
        if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)
                or sha == '0' * 40 or v.get('deleted') is not False
                or v.get('forced') is not False
                or not re.fullmatch('[A-Za-z0-9-]{1,100}', delivery)):
            raise Rejected(400, 'invalid main push identity')
        digest = hashlib.sha256(body).hexdigest()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if reconciliation_in(c):
                raise Rejected(503, 'reconciliation_required')
            old = c.execute('SELECT * FROM deliveries WHERE delivery=?', (delivery,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Rejected(409, 'delivery ID collision')
                return 200, {'job': old['job'], 'status': 'duplicate'}
            # Coalesce the SAME immutable target only. Never substitute a new SHA.
            job = c.execute('SELECT id FROM jobs WHERE repo=? AND sha=? ORDER BY id LIMIT 1', (repo, sha)).fetchone()
            if job:
                ident = job['id']
                status = 'coalesced'
            else:
                ident = c.execute("INSERT INTO jobs(repo,sha,status,queued) VALUES(?,?,'queued',?)", (repo, sha, time.time())).lastrowid
                status = 'queued'
            c.execute('INSERT INTO deliveries VALUES(?,?,?)', (delivery, digest, ident))
            return 202, {'job': ident, 'target_sha': sha, 'status': status}

    def step(self, execute):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if reconciliation_in(c):
                return False
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return False
            c.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (time.time(), row['id']))
            delivery = c.execute('SELECT delivery FROM deliveries WHERE job=? ORDER BY rowid LIMIT 1', (row['id'],)).fetchone()[0]
        try:
            result = execute(row['repo'], row['sha'], delivery)
            status = result['status']
            if (result.get('target_sha') != row['sha'] or result.get('delivery') != delivery
                    or status not in ('ready', 'failed', 'superseded', 'legacy_unverified', 'blocked_resources', 'uncertain')):
                raise ValueError('invalid executor receipt')
            if replacement_unconfirmed(result):
                # Retain all target/delivery/stage/manifests; do not replace evidence.
                result = {**result, 'executor_status': status, 'status': 'uncertain',
                          'isolation_reason': 'replacement_not_confirmed', 'automatic_retry': False}
                status = 'uncertain'
        except Exception as e:
            status, result = 'uncertain', {'status': 'uncertain', 'target_sha': row['sha'],
                'delivery': delivery, 'stage': 'unknown', 'reason': type(e).__name__, 'automatic_retry': False}
        with self.db() as c:
            c.execute('UPDATE jobs SET status=?,result=?,ended=? WHERE id=?', (status, json.dumps(result), time.time(), row['id']))
        return True

    def receipt(self, job):
        with self.db() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            return dict(row) if row else None

    def needs_reconciliation(self):
        with self.db() as c:
            return reconciliation_in(c)


class Supervisor:
    """Fail closed on worker/database failure, without rewriting ambiguous jobs."""
    def __init__(self, queue, execute):
        self.queue, self.execute = queue, execute
        self.gate = threading.RLock()
        self.failed = threading.Event()
        self.stopping = threading.Event()
        self.reason = None
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def fault(self, reason):
        with self.gate:
            self.reason = reason
            self.failed.set()

    def start(self):
        try:
            if self.queue.needs_reconciliation():
                self.fault('reconciliation_required')
                return
        except Exception:
            self.fault('database_unavailable')
            return
        self.thread.start()

    def healthy(self):
        if not self.thread.is_alive() or self.failed.is_set() or self.stopping.is_set():
            return False
        try:
            if self.queue.needs_reconciliation():
                self.fault('reconciliation_required')
                return False
        except Exception:
            self.fault('database_unavailable')
            return False
        return True

    def accept(self, headers, body):
        with self.gate:
            if not self.healthy():
                raise Rejected(503, 'execution_unavailable')
            try:
                return self.queue.accept(headers, body)
            except (sqlite3.Error, OSError):
                self.fault('database_unavailable')
                raise Rejected(503, 'execution_unavailable')

    def loop(self):
        try:
            while not self.stopping.is_set() and not self.failed.is_set():
                if not self.queue.step(self.execute):
                    self.stopping.wait(.25)
                if self.queue.needs_reconciliation():
                    self.fault('reconciliation_required')
                    return
        except BaseException:
            self.fault('worker_failed')
        finally:
            if not self.stopping.is_set():
                self.fault(self.reason or 'worker_exited')

    def stop(self):
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)


def child(command, timeout, env=None):
    """Kill this deployment's process group, never the Docker daemon/services."""
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=clean_environment() if env is None else env,
                         start_new_session=(os.name == 'posix'))
    try:
        stdout, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            os.killpg(p.pid, signal.SIGKILL)
        else:
            # Windows is offline test-only; production installation is Linux.
            subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
        p.communicate()
        raise TimeoutError('deployment timed out; reconcile before retry')
    if os.name == 'posix':
        # Remove descendants left behind even when the executor itself exited.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if p.returncode:
        raise RuntimeError('executor nonzero exit; private stderr not returned')
    return stdout


def execute(repo, sha, delivery):
    if repo == 'Gozxiao/personal-site':
        return json.loads(child(['python3', INSTALL + '/executor.py', sha, delivery], 900,
                                clean_environment(site=True)))
    # Preserve existing Share routing. Legacy exit zero is NOT a version proof.
    child(['bash', '/root/gozxiao-share/deploy.sh'], 600,
          {**clean_environment(), 'DEPLOY_EXPECTED_SHA': sha, 'DEPLOY_DELIVERY_ID': delivery})
    return {'status': 'legacy_unverified', 'target_sha': sha, 'delivery': delivery,
            'reason': 'Share script exit zero; target checkout/images not independently attested'}


def make_server(supervisor, port):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        def log_message(self, *_):
            pass
        def reply(self, status, result):
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path != '/healthz':
                return self.reply(404, {'error': 'not_found'})
            ok = supervisor.healthy()
            self.reply(200 if ok else 503, {'accepting': ok, 'worker_healthy': ok})
        def do_POST(self):
            try:
                if not supervisor.healthy():
                    raise Rejected(503, 'execution_unavailable')
                if self.path != '/webhook' or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise Rejected(400, 'invalid request framing')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise Rejected(413, 'bounded body required')
                body = self.rfile.read(size)
                if len(body) != size:
                    raise Rejected(400, 'incomplete body')
                status, result = supervisor.accept(self.headers, body)
            except Rejected as e:
                status, result = e.status, {'error': e.reason}
            except (ValueError, OSError):
                status, result = 400, {'error': 'invalid request'}
            self.reply(status, result)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    require_install(Path(INSTALL), __file__)
    os.umask(0o077)
    state = Path('/var/lib/personal-site-deploy')
    state.mkdir(mode=0o700, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != 0 or state.stat().st_mode & 0o077:
        raise PermissionError('private controller state required')
    # One controller instance: lock covers startup recovery and all workers.
    import fcntl
    lock = (state / 'controller.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    queue = Queue(state / 'receipts.sqlite3', {r: os.environ.get(k, '') for r, k in ROUTES.items()})
    supervisor = Supervisor(queue, execute)
    supervisor.start()
    server = make_server(supervisor, int(os.environ.get('WEBHOOK_PORT', '9000')))
    try:
        server.serve_forever()
    finally:
        supervisor.stop()
        server.server_close()


if __name__ == '__main__':
    main()

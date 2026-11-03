"""Synthetic signed requests, real disposable Git, Docker/HTTP protocol doubles."""
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).parent
def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
c = load('controller'); e = load('executor')
A, B = 'a' * 40, 'b' * 40
REPO = 'Gozxiao/personal-site'


def ready_receipt(sha, delivery):
    return {'status':'ready','target_sha':sha,'delivery':delivery,'checkout_sha':sha,
        'stage':'verify','replacement_started':True,'backend_health':True,'public_frontend':True,
        'frontend_sha256':'f'*64,'assets_checked':1,'built':{'backend':'b','web':'w'},
        'running':{'backend':{'manifest':'b'},'web':{'manifest':'w'}}}


class Admission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / 'q.db'
        self.keys = {r: 'synthetic-only-key' for r in c.ROUTES}
        self.q = c.Queue(self.path, self.keys)
    def tearDown(self):
        self.tmp.cleanup()
    def send(self, sha=A, delivery='test-1', repo=REPO, event='push', ref='refs/heads/main', **changes):
        v = {'repository': {'full_name': repo}, 'ref': ref, 'after': sha, 'deleted': False, 'forced': False, **changes}
        raw = json.dumps(v).encode()
        key = self.keys.get(repo, 'synthetic-only-key')
        headers = {'X-GitHub-Event': event, 'X-GitHub-Delivery': delivery,
                   'X-Hub-Signature-256': 'sha256=' + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()}
        return self.q.accept(headers, raw)
    def test_missing_secrets(self):
        with self.assertRaises(ValueError):c.Queue(self.path, {r: '' for r in c.ROUTES})
    def test_unknown_repo(self):
        with self.assertRaises(c.Rejected):self.send(repo='other/repo')
    def test_signature(self):
        with self.assertRaises(c.Rejected):self.q.accept({}, b'{"repository":{"full_name":"Gozxiao/personal-site"}}')
    def test_main_and_push(self):
        self.assertEqual(self.send(ref='refs/heads/dev')[0], 204)
        self.assertEqual(self.send(event='ping')[0], 204)
    def test_invalid_sha(self):
        for sha in ('0'*40, 'x'*40, A+';echo', None):
            with self.assertRaises(c.Rejected):self.send(sha=sha)
    def test_delete_force(self):
        for change in ({'deleted': True}, {'forced': True}, {'deleted': 'false'}):
            with self.assertRaises(c.Rejected):self.send(**change)
    def test_duplicate(self):
        first=self.send()[1]; self.assertEqual(self.send()[1]['job'],first['job'])
        with self.assertRaises(c.Rejected):self.send(sha=B)
    def test_same_sha_coalesces(self):
        self.assertEqual(self.send()[1]['job'], self.send(delivery='test-2')[1]['job'])
    def test_new_sha_kept_separate(self):
        self.assertNotEqual(self.send()[1]['job'],self.send(sha=B,delivery='test-2')[1]['job'])
    def test_newer_job_order_and_receipts(self):
        one=self.send()[1]['job'];two=self.send(sha=B,delivery='test-2')[1]['job'];seen=[]
        def execute(r,s,d):
            seen.append(s);return ready_receipt(s,d)
        self.q.step(execute);self.q.step(execute)
        self.assertEqual(seen,[A,B]);self.assertEqual(self.q.receipt(one)['status'],'ready');self.assertEqual(self.q.receipt(two)['status'],'ready')
        self.assertIsNotNone(self.q.receipt(two)['ended'])
    @unittest.skipUnless(sys.platform=='win32','Windows installation refusal boundary')
    def test_production_entry_refuses_windows(self):
        with self.assertRaises(PermissionError):c.main()
        with self.assertRaises(PermissionError):e.main()
    def test_share_route(self):
        self.assertEqual(self.send(repo='Gozxiao/Gozxiao-Share')[0],202)
    def test_timeout_is_quarantined(self):
        job=self.send()[1]['job']
        def timeout(*args):raise TimeoutError()
        self.q.step(timeout);self.assertEqual(self.q.receipt(job)['status'],'uncertain')
        self.assertFalse(self.q.step(timeout))
        with self.assertRaises(c.Rejected) as error:self.send(delivery='redelivery')
        self.assertEqual(error.exception.status,503)
    def test_restart_no_replay(self):
        job=self.send()[1]['job']
        with self.q.db() as db:db.execute("UPDATE jobs SET status='running'")
        q=c.Queue(self.path,self.keys)
        self.assertEqual(q.receipt(job)['status'],'interrupted');self.assertFalse(q.step(lambda *a:None))
    def test_wrong_executor_sha_rejected(self):
        job=self.send()[1]['job'];self.q.step(lambda *a:{'status':'ready','target_sha':B,'delivery':'test-1'})
        self.assertEqual(self.q.receipt(job)['status'],'uncertain')
    def test_real_child_timeout(self):
        with self.assertRaises(TimeoutError):c.child([sys.executable,'-c','import time; time.sleep(20)'],.1)
    def test_failure_receipt_no_retry(self):
        job=self.send()[1]['job']
        self.q.step(lambda r,s,d:{'status':'failed','target_sha':s,'delivery':d,'stage':'build'})
        self.assertEqual(json.loads(self.q.receipt(job)['result'])['stage'],'build')
        self.assertFalse(self.q.step(lambda *a:None))


class Executor(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.oldroot=e.ROOT;e.ROOT=self.root
        self.calls=[];self.fail_command=None;self.wrong=False;self.wrong_running=False;self.replaced=False;self.html='<html><script src="/assets/app-test.js"></script></html>'
        self.environments=[]
        self.remote=self.root/'origin.git';self.repo=self.root/'site';self.repo.mkdir()
        def git(*a):return subprocess.check_output(['git','-c','protocol.file.allow=always','-C',str(self.repo),*a],stderr=subprocess.STDOUT,text=True).strip()
        self.git=git;git('init','-q','-b','main');git('config','user.name','Synthetic');git('config','user.email','test@example.invalid')
        (self.repo/'deploy.sh').write_text('never execute checkout code')
        git('add','.');git('commit','-qm','first');self.first=git('rev-parse','HEAD')
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        git('remote','add','origin',self.remote.as_uri());git('push','-q','origin','main')
        (self.repo/'deploy.sh').write_text('malicious checkout script must not run')
        git('commit','-qam','second');self.target=git('rev-parse','HEAD');git('push','-q','origin','main')
        git('checkout','-q','--detach',self.first);git('branch','-f','main',self.first);git('checkout','-q','main')
    def tearDown(self):
        e.ROOT=self.oldroot;self.tmp.cleanup()
    def run_command(self,args,env):
        self.calls.append(args)
        self.environments.append((args,dict(env)))
        if args[0]=='git':return subprocess.check_output(['git','-c','protocol.file.allow=always',*args[1:]],cwd=self.repo,env=env,stderr=subprocess.STDOUT,text=True).strip()
        if self.fail_command and self.fail_command in args:raise RuntimeError('synthetic failure')
        if 'up' in args:self.replaced=True
        if 'config' in args:
            model={'name':'personal-site','services':{n:{'image':'personal-site-'+n+':'+self.target} for n in ('backend','web')},'volumes':{'site-data':{'name':'personal-site_site-data'}}}
            model['services']['web']['build']={'args':{'VITE_PINGVIN_SHARE_URL':'https://share.example.invalid'}}
            return json.dumps(model)
        if 'ps' in args:return 'container-'+args[-1]
        ids = {hashlib.sha256((kind+n).encode()).hexdigest():n for kind in ('config-', 'index-') for n in ('backend','web')}
        name = ids.get(args[-1].removeprefix('sha256:'), 'backend' if 'backend' in args[-1] else 'web')
        if '{{json .}}' in args and 'image' not in args:
            return json.dumps({'Image':'sha256:'+hashlib.sha256(('config-'+name).encode()).hexdigest(),
                'Config':{'Image':'personal-site-'+name+(':'+self.target if self.replaced else ':latest')}, 'ImageManifestDescriptor':None})
        if '{{json .}}' in args:
            wrong = self.wrong or (self.wrong_running and self.replaced) or (getattr(self,'wrong_baseline_tag',False) and ':baseline-' in args[-1])
            return json.dumps({'Id':'sha256:'+hashlib.sha256(('wrong-' if wrong else 'config-').encode()+name.encode()).hexdigest(), 'Os':'linux','Architecture':'amd64'})
        if '{{.Config.Image}}' in args:return 'personal-site-'+args[-1].replace('container-','')+':latest'
        if '{{.Id}}' in args:return 'sha256:'+hashlib.sha256(('index-'+name).encode()).hexdigest()
        if '{{json .Mounts}}' in args:return json.dumps([{'Name':'personal-site_site-data','Destination':'/data'}])
        if 'cat' in args:return self.html if args[-1].endswith('index.html') else 'asset'
        return ''
    def publish(self,get=None,resource_check=lambda:None):return e.publish(self.target,'synthetic-delivery',self.run_command,get or (lambda u:self.html.encode() if u.endswith('/') else b'asset'),resource_check=resource_check)
    def test_success_real_git_and_bound_evidence(self):
        r=self.publish();self.assertEqual(r['status'],'ready');self.assertEqual(self.git('rev-parse','HEAD'),self.target)
        self.assertTrue(r['backend_health']);self.assertEqual(r['assets_checked'],1)
        self.assertIsNone(r['baseline']['web']['source_sha'])
        self.assertFalse(any('bash' in x or 'pull' in x for x in self.calls))
        self.assertFalse(any(a in ('narralume','qq','share','server') for x in self.calls for a in x))
    def test_stale_target_not_substituted(self):
        r=e.publish(self.first,'old-delivery',self.run_command,lambda u:b'',resource_check=lambda:None)
        self.assertEqual(r['status'],'superseded');self.assertEqual(self.git('rev-parse','HEAD'),self.first)
        self.assertFalse(any(x[0]=='docker' for x in self.calls))
    def test_build_failure_no_replace(self):
        self.fail_command='build';r=self.publish();self.assertEqual(r['stage'],'build')
        self.assertFalse(any('up' in x for x in self.calls))
    def test_backend_failure_not_ready(self):
        self.fail_command='exec';r=self.publish();self.assertEqual(r['status'],'uncertain');self.assertTrue(r['replacement_may_have_occurred'])
    def test_frontend_mismatch(self):
        r=self.publish(lambda u:b'wrong');self.assertEqual(r['status'],'uncertain')
    def test_public_stale_not_ready(self):
        r=self.publish(lambda u:b'stale' if u.startswith('https:') else self.html.encode() if u.endswith('/') else b'asset')
        self.assertEqual(r['status'],'uncertain')
    def test_manifest_mismatch(self):
        self.wrong=True;r=self.publish();self.assertEqual(r['status'],'failed')
        self.assertFalse(self.replaced)
    def test_running_manifest_mismatch_is_uncertain(self):
        self.wrong_running=True;r=self.publish();self.assertEqual(r['status'],'uncertain')
        self.assertEqual(r['stage'],'verify');self.assertTrue(r['replacement_started'])
    def test_baseline_uses_verified_name_and_rechecks_created_tag(self):
        self.wrong_baseline_tag=True;r=self.publish()
        self.assertEqual(r['stage'],'baseline');self.assertEqual(r['status'],'failed');self.assertFalse(self.replaced)
        tags=[x for x in self.calls if x[:3]==['docker','image','tag']]
        self.assertEqual(tags[0][3],'personal-site-backend:latest')
        self.assertFalse(any(x[-1].startswith('sha256:') for x in self.calls if x[:3]==['docker','image','inspect']))
    def test_dirty_preflight(self):
        (self.repo/'untracked').write_text('retain');r=self.publish();self.assertEqual(r['stage'],'preflight')
        self.assertFalse(any(x[0]=='docker' for x in self.calls))


if __name__=='__main__':unittest.main(verbosity=2)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class RuntimePortabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='monitor runtime ')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.install = root / 'copied monitor'
        self.cwd = root / 'unrelated cwd'
        self.cwd.mkdir()
        self.state = root / 'job state'
        shutil.copytree(ROOT / 'codex_monitor', self.install / 'codex_monitor',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.entrypoint = self.install / 'codex_monitor' / 'entrypoint.py'
        self.env = os.environ.copy()
        self.env.pop('PYTHONPATH', None)
        self.env['PYTHONSAFEPATH'] = '1'
        self.env['PYTHONUTF8'] = '1'

    def run_command(self, argv):
        result = subprocess.run(argv, cwd=self.cwd, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def run_from_copy(self, body):
        setup = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from codex_monitor.runner import validate_spec
from codex_monitor.store import Store

store = Store(sys.argv[2])
spec = validate_spec({
    'command': [sys.executable, '-c', 'print("portable training output")'],
    'cwd': str(Path.cwd()),
    'thread_id': '11111111-1111-4111-8111-111111111111',
    'bridge': {'enabled': False},
})
job_id = store.create(spec)
"""
        return self.run_command([sys.executable, '-I', '-X', 'utf8', '-c', setup + body,
                                 str(self.install), str(self.state)])

    def test_detached_worker_runs_from_copied_package(self):
        outcome = json.loads(self.run_from_copy("""
from codex_monitor.runner import launch_worker

worker = launch_worker(store, job_id)
try:
    assert worker.wait(timeout=15) == 0, worker.args
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait(timeout=5)
print(json.dumps({'job': store.get(job_id), 'worker_argv': worker.args}))
"""))
        self.assertIn(str(self.entrypoint), outcome['worker_argv'])
        job = outcome['job']
        self.assertEqual(job['delivery'], 'disabled')
        result = json.loads(Path(job['result_path']).read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(Path(result['log_path']).read_text(encoding='utf-8').strip(),
                         'portable training output')

    def test_notification_acknowledges_from_unrelated_cwd(self):
        message = self.run_from_copy("""
from codex_monitor.bridge import notification
from codex_monitor.runner import finish

finish(store, job_id, 'succeeded', 'completed', 0)
job = store.get(job_id)
print(notification(spec, job['result_path'], job['event_id']))
""")
        argv = json.loads(next(line for line in message.splitlines() if line.startswith('["')))
        job = json.loads(self.run_command(argv))
        self.assertIn(str(self.entrypoint), argv)
        self.assertNotIn(str(ROOT), message)
        self.assertEqual(job['delivery_confirmed'], 1)


if __name__ == '__main__':
    unittest.main()

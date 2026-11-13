import concurrent.futures
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_monitor.cli import main
from codex_monitor.runner import Health, deliver_result, finish, launch_worker, run, validate_spec
from codex_monitor.store import Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def spec(self, source='print("done")', **changes):
        raw = dict(command=[sys.executable, '-u', '-c', source], cwd=str(Path.cwd()), thread_id='test-thread', bridge={'enabled': False})
        raw.update(changes)
        return validate_spec(raw)

    def execute(self, source='print("done")', **changes):
        job_id = self.store.create(self.spec(source, **changes))
        run(self.store, job_id)
        return self.store.get(job_id)

    def test_validation(self):
        for change in ({'command': 'echo unsafe'}, {'health': {'pattern': '(.*)'}}, {'max_runtime': float('nan')}, {'health': {'startup_timeout': -1}}, {'bridge': {'enabled': 'false'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.spec(**change)

    def test_health_requires_finite_monotonic_progress(self):
        config = self.spec(health={'pattern': r'step=(?P<step>\S+) loss=(?P<loss>\S+)'})['health']
        health = Health(config)
        health.feed('step=0 loss=1\nstep=0 loss=1\n')
        self.assertEqual(health.state, 'starting')
        health.feed('step=1 loss=.5\nstep=2 loss=.25\n')
        self.assertEqual(health.state, 'healthy')
        self.assertEqual(health.loss, .25)
        health.feed('step=3 loss=nan\n')
        self.assertEqual(health.failure(), 'failed')

    def test_claim_is_single_winner(self):
        job = self.store.create(self.spec())
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.store.claim_run(job, os.getpid()), range(16)))
        self.assertEqual(sum(results), 1)
        self.assertFalse(self.store.claim_run(job, os.getpid()))

    def test_success_large_stdout_and_failure(self):
        job = self.execute('print("x" * 2500000)')
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['health'], 'unverified')
        self.assertGreater(Path(job['log_path']).stat().st_size, 2500000)
        self.assertEqual(job['delivery'], 'disabled')
        self.assertEqual(json.loads(Path(job['result_path']).read_text())['status'], 'succeeded')
        self.assertEqual(self.execute('raise SystemExit(7)')['exit_code'], 7)

    def test_health_success_and_timeouts(self):
        pattern = r'step=(?P<step>\d+)'
        job = self.execute('print("step=0\\nstep=1\\nstep=2")', health={'pattern': pattern})
        self.assertEqual((job['status'], job['health']), ('succeeded', 'healthy'))
        job = self.execute('import time; time.sleep(10)', health={'pattern': pattern, 'startup_timeout': .2})
        self.assertEqual(job['status'], 'startup_failed')
        job = self.execute('import time; print("step=0\\nstep=1\\nstep=2"); time.sleep(10)', health={'pattern': pattern, 'stall_timeout': .2})
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(self.execute('import time; time.sleep(10)', max_runtime=.2)['status'], 'timed_out')

    def test_duplicate_worker_does_not_rerun(self):
        job = self.execute()
        before = Path(job['log_path']).read_bytes()
        run(self.store, job['id'])
        self.assertEqual(Path(job['log_path']).read_bytes(), before)

    def test_delivery_claim_and_ack_race(self):
        job_id = self.store.create(self.spec())
        finish(self.store, job_id, 'succeeded', 'ok', 0)
        job = self.store.get(job_id)
        def delivery(spec, path, event):
            self.store.acknowledge(job_id, event, spec['thread_id'])
            return {'state': 'submitted'}
        with patch('codex_monitor.bridge.deliver', side_effect=delivery) as mocked:
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda _: deliver_result(self.store, job_id), range(16)))
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.store.get(job_id)['delivery_confirmed'], 1)
        with self.assertRaises(ValueError):
            self.store.acknowledge(job_id, job['event_id'], 'other-thread')

    def test_unknown_delivery_is_never_replayed(self):
        job_id = self.store.create(self.spec())
        finish(self.store, job_id, 'succeeded', 'ok')
        with patch('codex_monitor.bridge.deliver', side_effect=RuntimeError('lost response')) as mocked:
            self.assertEqual(deliver_result(self.store, job_id)['delivery'], 'uncertain')
            deliver_result(self.store, job_id)
            self.assertEqual(mocked.call_count, 1)

    def test_detached_worker(self):
        job_id = self.store.create(self.spec())
        worker = launch_worker(self.store, job_id)
        self.assertEqual(worker.wait(timeout=15), 0)
        self.assertEqual(self.store.get(job_id)['status'], 'succeeded')

    def test_wait_ready_is_bounded(self):
        job = self.store.create(self.spec(health={'pattern': r'(?P<step>\d+)'}))
        start = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(['--state-dir', self.temp.name, 'wait-ready', job, '--timeout', '.05'])
        self.assertEqual(code, 2)
        self.assertLess(time.monotonic() - start, 1)

    def test_nonfinite_training_fails_after_healthy(self):
        job = self.execute('print("step=0 loss=1\\nstep=1 loss=.5\\nstep=2 loss=.2\\nstep=3 loss=nan")',
                           health={'pattern': r'step=(?P<step>\S+) loss=(?P<loss>\S+)'})
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['health'], 'invalid')

    def test_ssh_transport_recovers_without_relaunch(self):
        spec = self.spec(backend='ssh', ssh_host='host', cwd='/tmp', max_runtime=5)
        job_id = self.store.create(spec)
        with patch('codex_monitor.ssh.start', return_value={'job_id': job_id}) as start, patch('codex_monitor.ssh.poll', side_effect=[RuntimeError('disconnect'), {'log': 'done\n', 'offset': 5, 'done': True, 'exit_code': 0}, {'log': '', 'offset': 5, 'done': True, 'exit_code': 0}]), patch('codex_monitor.ssh.stop') as stop:
            run(self.store, job_id)
        self.assertEqual(start.call_count, 1)
        stop.assert_not_called()
        self.assertEqual(self.store.get(job_id)['status'], 'succeeded')

    def test_ssh_ambiguous_start_retains_identity(self):
        job_id = self.store.create(self.spec(backend='ssh', ssh_host='host', cwd='/tmp'))
        with patch('codex_monitor.ssh.start', side_effect=RuntimeError('reply lost')) as start, patch('codex_monitor.ssh.stop') as stop, patch('codex_monitor.runner.time.sleep'):
            run(self.store, job_id)
            run(self.store, job_id)
        job = self.store.get(job_id)
        self.assertEqual(start.call_count, 3)
        self.assertTrue(all(call.args[1] == job_id for call in start.call_args_list))
        stop.assert_not_called()
        self.assertEqual(json.loads(job['remote_handle']), {'job_id': job_id})
        self.assertIn('remote launch remains unknown', job['detail'])

    def test_ssh_start_lost_reply_retries_same_job(self):
        job_id = self.store.create(self.spec(backend='ssh', ssh_host='host', cwd='/tmp'))
        with patch('codex_monitor.ssh.start', side_effect=[RuntimeError('reply lost'), {'job_id': job_id}]) as start, patch('codex_monitor.ssh.poll', return_value={'log': '', 'offset': 0, 'done': True, 'exit_code': 0}), patch('codex_monitor.ssh.stop') as stop, patch('codex_monitor.runner.time.sleep'):
            run(self.store, job_id)
        self.assertEqual(start.call_count, 2)
        self.assertTrue(all(call.args[1] == job_id for call in start.call_args_list))
        stop.assert_not_called()
        self.assertEqual(self.store.get(job_id)['status'], 'succeeded')

    def test_ssh_nan_stops_still_running_process(self):
        spec = self.spec(backend='ssh', ssh_host='host', cwd='/tmp', health={'pattern': r'step=(?P<step>\S+) loss=(?P<loss>\S+)'})
        job_id = self.store.create(spec)
        handle = {'job_id': job_id}
        with patch('codex_monitor.ssh.start', return_value=handle), patch('codex_monitor.ssh.poll', return_value={'log': 'step=1 loss=nan\n', 'offset': 16, 'done': False, 'exit_code': None}), patch('codex_monitor.ssh.stop') as stop:
            run(self.store, job_id)
        stop.assert_called_once_with(spec, handle)
        self.assertEqual(self.store.get(job_id)['status'], 'failed')

    def test_ssh_exited_process_drains_logs_past_deadlines(self):
        pattern = r'step=(?P<step>\d+)'
        for changes in ({'max_runtime': 1e-9}, {'health': {'pattern': pattern, 'startup_timeout': 1e-9}}, {'health': {'pattern': pattern, 'min_advances': 1, 'stall_timeout': 1e-9}}):
            with self.subTest(changes=changes):
                job_id = self.store.create(self.spec(backend='ssh', ssh_host='host', cwd='/tmp', **changes))
                replies = [{'log': 'step=0\nstep=1\n', 'offset': 14, 'done': False, 'exit_code': 0}, {'log': 'step=2\n', 'offset': 21, 'done': True, 'exit_code': 0}]
                with patch('codex_monitor.ssh.start', return_value={'job_id': job_id}), patch('codex_monitor.ssh.poll', side_effect=replies) as poll, patch('codex_monitor.ssh.stop') as stop, patch('codex_monitor.runner.time.sleep'):
                    run(self.store, job_id)
                self.assertEqual(self.store.get(job_id)['status'], 'succeeded')
                self.assertEqual(poll.call_count, 2)
                stop.assert_not_called()
                self.assertIn('step=2', Path(self.store.get(job_id)['log_path']).read_text())

    def test_sending_claim_survives_crash_without_resend(self):
        job_id = self.store.create(self.spec())
        finish(self.store, job_id, 'succeeded', 'ok')
        self.assertTrue(self.store.claim_delivery(job_id))
        with patch('codex_monitor.bridge.deliver') as deliver:
            deliver_result(self.store, job_id)
        deliver.assert_not_called()

    def test_timeout_stops_descendant(self):
        marker = str(Path(self.temp.name) / 'descendant-survived.txt')
        descendant = 'import time; from pathlib import Path; time.sleep(1.2); Path(' + repr(marker) + ').write_text("alive")'
        source = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(descendant) + ']); time.sleep(10)'
        self.assertEqual(self.execute(source, max_runtime=.4)['status'], 'timed_out')
        time.sleep(1.3)
        self.assertFalse(Path(marker).exists())


if __name__ == '__main__':
    unittest.main()

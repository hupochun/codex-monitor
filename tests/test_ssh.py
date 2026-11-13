import base64
import json
import shlex
import subprocess
import unittest
from unittest.mock import patch

from codex_monitor import ssh


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.spec = {'ssh_host': 'user@example.org', 'command': ['python3', 'train.py', 'a; echo unsafe'], 'cwd': '/work dir'}
        self.job_id = 'a' * 32
        self.handle = {'job_id': self.job_id}

    def wire(self, data=b'', offset=0, done=False, code=None):
        return {'data': base64.b64encode(data).decode(), 'offset': offset + len(data), 'done': done, 'exit_code': code}

    @patch('codex_monitor.ssh.subprocess.run')
    def test_start_transport_and_quoted_payload(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self.handle).encode(), b'')
        spec = dict(self.spec, ssh_python='/path with spaces/python3')
        self.assertEqual(ssh.start(spec, self.job_id), self.handle)
        args, kwargs = run.call_args
        argv = args[0]
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('ConnectTimeout=10', argv)
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        remote = shlex.split(argv[-1])
        self.assertEqual(remote[:2], ['/path with spaces/python3', '-c'])
        request = json.loads(base64.b64decode(remote[-1]))
        self.assertEqual(request['spec']['command'], self.spec['command'])
        self.assertEqual(request['job_id'], self.job_id)
        compile(remote[2], '<remote>', 'exec')
        compile(request['wrapper'], '<wrapper>', 'exec')

    @patch('codex_monitor.ssh._transport')
    def test_repeat_start_preserves_job_identity(self, transport):
        transport.side_effect = [RuntimeError('disconnected'), self.handle]
        with self.assertRaises(RuntimeError):
            ssh.start(self.spec, self.job_id)
        self.assertEqual(ssh.start(self.spec, self.job_id), self.handle)
        self.assertEqual(transport.call_args_list[0], transport.call_args_list[1])

    @patch('codex_monitor.ssh._transport')
    def test_poll_binary_offset_and_split_utf8(self, transport):
        encoded = '中'.encode()
        transport.return_value = self.wire(b'abc' + encoded[:2], 10)
        first = ssh.poll(self.spec, self.handle, 10)
        self.assertEqual(first['log'], 'abc')
        self.assertEqual(first['offset'], 13)
        transport.return_value = self.wire(encoded + b'\xff', 13, True, 2)
        last = ssh.poll(self.spec, self.handle, first['offset'])
        self.assertEqual(last, {'log': '中\ufffd', 'offset': 17, 'done': True, 'exit_code': 2})
        self.assertEqual(transport.call_args.args[1]['limit'], ssh.CHUNK_SIZE)

    @patch('codex_monitor.ssh._transport')
    def test_final_truncated_utf8_is_consumed(self, transport):
        transport.return_value = self.wire(b'\xe4', 0, True, 0)
        self.assertEqual(ssh.poll(self.spec, self.handle, 0), {'log': '\ufffd', 'offset': 1, 'done': True, 'exit_code': 0})

    @patch('codex_monitor.ssh._transport')
    def test_invalid_remote_offsets_and_chunk(self, transport):
        for wire in (dict(self.wire(b'x'), offset=99), self.wire(b'x' * (ssh.CHUNK_SIZE + 1))):
            transport.return_value = wire
            with self.assertRaises(RuntimeError):
                ssh.poll(self.spec, self.handle, 0)

    @patch('codex_monitor.ssh.subprocess.run')
    def test_transport_failures_are_not_job_success(self, run):
        run.return_value = subprocess.CompletedProcess([], 255, b'', b'connection lost')
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            ssh.poll(self.spec, self.handle, 0)
        run.return_value = subprocess.CompletedProcess([], 0, b'not JSON', b'')
        with self.assertRaisesRegex(RuntimeError, 'invalid response'):
            ssh.start(self.spec, self.job_id)
        run.side_effect = subprocess.TimeoutExpired('ssh', 45)
        with self.assertRaises(subprocess.TimeoutExpired):
            ssh.start(self.spec, self.job_id)

    @patch('codex_monitor.ssh.subprocess.run')
    def test_argument_injection_rejected_before_transport(self, run):
        for host in ['-oProxyCommand=bad', 'host; touch bad', 'host\nother', 'user@host $(bad)']:
            with self.assertRaises(ValueError):
                ssh.start(dict(self.spec, ssh_host=host), self.job_id)
        for job_id in ['../escape', '-danger', 'a' * 31]:
            with self.assertRaises(ValueError):
                ssh.start(self.spec, job_id)
        for spec in [dict(self.spec, cwd='relative'), dict(self.spec, command='shell text'), dict(self.spec, command=[''])]:
            with self.assertRaises(ValueError):
                ssh.start(spec, self.job_id)
        run.assert_not_called()

    @patch('codex_monitor.ssh._transport')
    def test_stop_sends_only_validated_job_identity(self, transport):
        ssh.stop(self.spec, self.handle)
        transport.assert_called_once_with(self.spec, {'op': 'stop', 'job_id': self.job_id})

    def test_negative_offset_rejected(self):
        for offset in [-1, True, 1.5]:
            with self.assertRaises(ValueError):
                ssh.poll(self.spec, self.handle, offset)


class WrapperCleanupTests(unittest.TestCase):
    def execute_wrapper(self, *, interrupt, group=1234, result_write_fails=False):
        import contextlib
        import signal
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        original_read = Path.read_text
        original_replace = __import__('os').replace
        handlers = {}
        events = []
        descendant = {'alive': True}
        child = Mock()
        waits = 0

        def wait(**kwargs):
            nonlocal waits
            waits += 1
            if interrupt and waits == 1:
                # Direct child exits on TERM, but its grandchild ignores TERM.
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return -15 if interrupt else 0

        child.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text(json.dumps({'command': ['train'], 'cwd': '/work'}))

            def read(path, *args, **kwargs):
                name = str(path).replace('\\', '/')
                if name.endswith('/proc/1234/stat'):
                    return '1234 (python) S ' + ' '.join(['0'] * 18) + ' 42'
                if name.endswith('/proc/sys/kernel/random/boot_id'):
                    return 'boot'
                return original_read(path, *args, **kwargs)

            def replace(source, destination):
                if Path(destination).name == 'result.json':
                    if result_write_fails:
                        raise OSError('disk full')
                    events.append('result')
                return original_replace(source, destination)

            def killpg(pid, sig):
                self.assertEqual((pid, sig), (1234, signal.SIGKILL))
                descendant['alive'] = False
                events.append('kill_group')

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'argv', ['wrapper', directory]))
                stack.enter_context(patch.object(Path, 'read_text', read))
                stack.enter_context(patch('os.getpid', return_value=1234))
                stack.enter_context(patch('os.getpgrp', return_value=group, create=True))
                stack.enter_context(patch('os.killpg', side_effect=killpg, create=True))
                stack.enter_context(patch('os.replace', side_effect=replace))
                stack.enter_context(patch.object(signal, 'SIGKILL', 9, create=True))
                stack.enter_context(patch('signal.signal', side_effect=lambda sig, handler: handlers.__setitem__(sig, handler)))
                stack.enter_context(patch('subprocess.Popen', return_value=child))
                if result_write_fails:
                    with self.assertRaisesRegex(OSError, 'disk full'):
                        exec(compile(ssh._WRAPPER, '<wrapper>', 'exec'), {})
                    result = None
                else:
                    exec(compile(ssh._WRAPPER, '<wrapper>', 'exec'), {})
                    result = json.loads((root / 'result.json').read_text())
            return events, descendant['alive'], result

    def test_stop_kills_term_resistant_grandchild_after_parent_exits(self):
        events, descendant_alive, result = self.execute_wrapper(interrupt=True)
        self.assertFalse(descendant_alive)
        self.assertEqual(events, ['result', 'kill_group'])
        self.assertEqual(result['exit_code'], -15)

    def test_normal_exit_cleans_remaining_group_without_losing_result(self):
        events, descendant_alive, result = self.execute_wrapper(interrupt=False)
        self.assertFalse(descendant_alive)
        self.assertEqual(events, ['result', 'kill_group'])
        self.assertEqual(result['exit_code'], 0)

    def test_cleanup_does_not_address_another_process_group(self):
        events, _, _ = self.execute_wrapper(interrupt=True, group=5678)
        self.assertEqual(events, ['result'])

    def test_result_write_failure_still_cleans_group(self):
        events, descendant_alive, _ = self.execute_wrapper(interrupt=True, result_write_fails=True)
        self.assertFalse(descendant_alive)
        self.assertEqual(events, ['kill_group'])


class RemoteProtocolTests(unittest.TestCase):
    """Execute the shipped bootstrap with filesystem isolation and mocked OS/SSH."""
    def remote(self, home, request, popen=None, read_text=None, killpg=None):
        import contextlib
        import io
        import sys
        import types
        from pathlib import Path
        payload = base64.b64encode(json.dumps(request).encode()).decode()
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'home', return_value=Path(home)))
            stack.enter_context(patch.object(sys, 'argv', ['bootstrap', payload]))
            stack.enter_context(patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=lambda *a: None, LOCK_EX=1)}))
            stack.enter_context(patch('subprocess.Popen', side_effect=popen))
            stack.enter_context(patch('time.sleep'))
            stack.enter_context(patch('os.getpgid', return_value=1234, create=True))
            stack.enter_context(patch('os.killpg', side_effect=killpg, create=True))
            if read_text:
                stack.enter_context(patch.object(Path, 'read_text', read_text))
            with contextlib.redirect_stdout(output):
                exec(compile(ssh._REMOTE, '<remote>', 'exec'), {})
        return json.loads(output.getvalue())

    def test_remote_launch_is_idempotent_and_fully_detached(self):
        import tempfile
        from pathlib import Path
        request = {'op': 'start', 'job_id': 'b' * 32, 'spec': {'command': ['train'], 'cwd': '/work'}, 'wrapper': ssh._WRAPPER}
        launches = []
        with tempfile.TemporaryDirectory() as home:
            def launch(argv, **kwargs):
                launches.append((argv, kwargs))
                root = Path(argv[-1])
                (root / 'result.json').write_text('{"exit_code": 0}')
            self.remote(home, request, popen=launch)
            self.remote(home, request, popen=launch)
            self.assertEqual(len(launches), 1)
            kwargs = launches[0][1]
            self.assertTrue(kwargs['start_new_session'])
            self.assertTrue(kwargs['close_fds'])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertIs(kwargs['stdout'], kwargs['stderr'])
            changed = dict(request, spec={'command': ['other'], 'cwd': '/work'})
            with self.assertRaisesRegex(RuntimeError, 'different specification'):
                self.remote(home, changed, popen=launch)

    def test_remote_uncertain_launch_does_not_spawn_again(self):
        import tempfile
        from pathlib import Path
        request = {'op': 'start', 'job_id': 'c' * 32, 'spec': {'command': ['train'], 'cwd': '/work'}, 'wrapper': ssh._WRAPPER}
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                self.remote(home, request, popen=lambda *a, **k: (_ for _ in ()).throw(OSError('spawn failed')))
            launches = []
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                self.remote(home, request, popen=lambda *a, **k: launches.append(a))
            self.assertEqual(launches, [])

    def test_remote_poll_drains_log_before_done(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as home:
            job_id = 'd' * 32
            root = Path(home) / '.local/state/codex-monitor/jobs' / job_id
            root.mkdir(parents=True)
            (root / 'result.json').write_text('{"exit_code": 3}')
            (root / 'log.txt').write_bytes(b'abcdef')
            request = {'op': 'poll', 'job_id': job_id, 'offset': 0, 'limit': 4}
            first = self.remote(home, request)
            self.assertFalse(first['done'])
            self.assertEqual(base64.b64decode(first['data']), b'abcd')
            last = self.remote(home, dict(request, offset=4))
            self.assertTrue(last['done'])
            self.assertEqual(last['exit_code'], 3)
            self.assertEqual(base64.b64decode(last['data']), b'ef')

    def test_stop_refuses_reused_pid(self):
        import tempfile
        from pathlib import Path
        original_read = Path.read_text
        with tempfile.TemporaryDirectory() as home:
            job_id = 'e' * 32
            root = Path(home) / '.local/state/codex-monitor/jobs' / job_id
            root.mkdir(parents=True)
            (root / 'process.json').write_text(json.dumps({'pid': 1234, 'start': 'OLD', 'boot': 'boot'}))
            def read(path, *args, **kwargs):
                if str(path).replace('\\', '/').endswith('/proc/1234/stat'):
                    return '1234 (python) S ' + ' '.join(['0'] * 18) + ' NEW'
                return original_read(path, *args, **kwargs)
            signals = []
            with self.assertRaisesRegex(RuntimeError, 'identity unavailable or changed'):
                self.remote(home, {'op': 'stop', 'job_id': job_id}, read_text=read, killpg=lambda *a: signals.append(a))
            self.assertEqual(signals, [])

if __name__ == '__main__':
    unittest.main()

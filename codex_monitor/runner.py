"""One-shot worker. A claimed training run is never automatically replayed."""
import codecs
import ctypes
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .store import Store, TERMINAL


def validate_spec(raw):
    if not isinstance(raw, dict):
        raise ValueError('spec must be an object')
    spec = dict(raw)
    argv = spec.get('command')
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv) or not argv[0]:
        raise ValueError('command must be a string argv list with a nonempty executable')
    spec.setdefault('backend', 'local')
    if spec['backend'] not in ('local', 'ssh'):
        raise ValueError('backend must be local or ssh')
    cwd = spec.get('cwd')
    if not isinstance(cwd, str) or '\0' in cwd:
        raise ValueError('cwd must be an absolute directory')
    if spec['backend'] == 'local':
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError('local cwd must be an existing absolute directory')
    elif not cwd.startswith('/'):
        raise ValueError('SSH cwd must be an absolute Linux directory')
    if spec['backend'] == 'ssh' and (not isinstance(spec.get('ssh_host'), str) or not spec['ssh_host'].strip()):
        raise ValueError('ssh_host required')
    if not isinstance(spec.get('health', {}), dict) or not isinstance(spec.get('bridge', {}), dict):
        raise ValueError('health and bridge must be objects')
    health = spec['health'] = dict(spec.get('health', {}))
    health.setdefault('min_advances', 2)
    health.setdefault('startup_timeout', 300)
    health.setdefault('stall_timeout', 0)
    if type(health['min_advances']) is not int or health['min_advances'] < 1:
        raise ValueError('min_advances must be a positive integer')
    pattern = health.get('pattern')
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError('health pattern must be a nonempty regex')
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'invalid health pattern: {exc}') from exc
        if 'step' not in compiled.groupindex:
            raise ValueError('health pattern requires named step group')
    for key, obj in [('startup_timeout', health), ('stall_timeout', health), ('max_runtime', spec)]:
        obj.setdefault(key, 0)
        value = obj[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{key} must be a finite nonnegative number')
    if pattern and health['startup_timeout'] <= 0:
        raise ValueError('health startup_timeout must be positive')
    spec['bridge'] = dict(spec.get('bridge', {}))
    spec['bridge'].setdefault('enabled', True)
    if type(spec['bridge']['enabled']) is not bool:
        raise ValueError('bridge.enabled must be boolean')
    spec['thread_id'] = spec.get('thread_id') or os.environ.get('CODEX_THREAD_ID', '')
    if not isinstance(spec['thread_id'], str):
        raise ValueError('thread_id must be text')
    if spec['bridge']['enabled']:
        try:
            uuid.UUID(spec['thread_id'])
        except (ValueError, AttributeError) as exc:
            raise ValueError('bridge requires a UUID thread_id or CODEX_THREAD_ID') from exc
    if spec['bridge'].get('mode', 'auto') not in ('auto', 'native', 'cli'):
        raise ValueError('bridge.mode must be auto, native, or cli')
    for key in ('codex', 'remote', 'remote_auth_token_env', 'app_pipe'):
        if key in spec['bridge'] and (not isinstance(spec['bridge'][key], str) or not spec['bridge'][key].strip() or '\0' in spec['bridge'][key]):
            raise ValueError(f'bridge.{key} must be nonempty text')
    if not isinstance(spec.get('follow_up', ''), str):
        raise ValueError('follow_up must be text')
    return spec


class Health:
    def __init__(self, config):
        self.config = config
        self.pattern = re.compile(config['pattern']) if config.get('pattern') else None
        self.step = None
        self.loss = None
        self.advances = 0
        self.started = self.last_advance = time.monotonic()
        self.state = 'starting' if self.pattern else 'unverified'
        self.pending = ''
        self.invalid_numeric = False

    def feed(self, text, final=False):
        if not self.pattern:
            return
        lines = (self.pending + text).split('\n')
        self.pending = '' if final else lines.pop()
        # Bounded memory for malformed output with no line endings.
        self.pending = self.pending[-65536:]
        for line in lines:
            for match in self.pattern.finditer(line):
                try:
                    step = float(match.group('step'))
                    loss_text = match.groupdict().get('loss')
                    loss = float(loss_text) if loss_text is not None else None
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(step) or (loss is not None and not math.isfinite(loss)):
                    self.invalid_numeric = True
                    self.state = 'invalid'
                    continue
                if self.step is None or step > self.step:
                    if self.step is not None:
                        self.advances += 1
                    self.step, self.loss = step, loss
                    self.last_advance = time.monotonic()
                    if self.advances >= self.config['min_advances'] and not self.invalid_numeric:
                        self.state = 'healthy'

    def failure(self):
        now = time.monotonic()
        if self.invalid_numeric:
            return 'failed'
        if self.pattern and self.state != 'healthy' and now - self.started >= self.config['startup_timeout']:
            return 'startup_failed'
        if self.state == 'healthy' and self.config['stall_timeout'] and now - self.last_advance >= self.config['stall_timeout']:
            return 'stalled'
        return None


class WindowsJob:
    """Kill only this worker's child tree, including on worker process death."""
    def __init__(self, process):
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            self.close()
            process.kill()
            process.wait()
            raise OSError(error, 'could not contain training process in Windows job')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def stop_local(process, container):
    if os.name == 'nt':
        if container:
            container.close()
        elif process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def launch_worker(store, job_id):
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    entrypoint = Path(__file__).with_name('entrypoint.py').resolve()
    argv = [sys.executable, str(entrypoint), '--state-dir', str(store.root), '_run', job_id]
    return subprocess.Popen(argv, **kwargs)


def finish(store, job_id, status, detail, exit_code=None, health=None):
    job = store.get(job_id)
    result = {'job_id': job_id, 'event_id': job['event_id'], 'thread_id': job['spec']['thread_id'], 'status': status, 'detail': detail, 'exit_code': exit_code, 'health': health or job['health'], 'completed_at': time.time(), 'log_path': job['log_path'], 'follow_up': job['spec'].get('follow_up', '')}
    path = Path(job['result_path'])
    temp = path.with_suffix('.json.tmp')
    with temp.open('w', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    store.update(job_id, status=status, detail=detail, exit_code=exit_code, health=result['health'])


def deliver_result(store, job_id):
    if not store.claim_delivery(job_id):
        return store.get(job_id)
    job = store.get(job_id)
    try:
        from .bridge import deliver
        outcome = deliver(job['spec'], job['result_path'], job['event_id'])
        if not isinstance(outcome, dict) or outcome.get('state') not in {'disabled', 'submitted', 'confirmed', 'retryable', 'uncertain', 'blocked'}:
            raise ValueError('invalid bridge outcome')
        store.update(job_id, delivery=outcome['state'], delivery_detail=str(outcome.get('detail', '')))
        if outcome['state'] == 'confirmed':
            store.acknowledge(job_id, job['event_id'], job['spec']['thread_id'])
    except Exception as exc:
        # Once claimed, an exception cannot establish that nothing was sent.
        store.update(job_id, delivery='uncertain', delivery_detail=f'{type(exc).__name__}: {exc}')
    return store.get(job_id)


def run(store, job_id):
    if not store.claim_run(job_id, os.getpid()):
        return 0
    job = store.get(job_id)
    spec = job['spec']
    health = Health(spec['health'])
    process = container = remote = None
    status, detail, code = 'interrupted', 'worker interrupted', None
    started = time.monotonic()
    offset = 0
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    transport_failures = 0
    remote_done = False
    remote_exited = False
    remote_start_confirmed = False
    try:
        with open(job['log_path'], 'ab', buffering=0) as output:
            if spec['backend'] == 'local':
                kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
                process = subprocess.Popen(spec['command'], cwd=spec['cwd'], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **kwargs)
                if os.name == 'nt':
                    container = WindowsJob(process)
            else:
                from . import ssh
                remote = {'job_id': job_id}
                store.update(job_id, remote_handle=json.dumps(remote))
                for attempt in range(3):
                    if attempt and spec['max_runtime'] and time.monotonic() - started >= spec['max_runtime']:
                        raise RuntimeError(detail)
                    try:
                        # The adapter durably claims this exact ID before launch.
                        # A lost reply can be retried without creating another run.
                        remote = ssh.start(spec, job_id)
                        remote_start_confirmed = True
                        break
                    except Exception as exc:
                        detail = f'remote launch remains unknown: {exc}; job identity retained; remote training may still be running'
                        store.update(job_id, detail=detail, heartbeat=time.time())
                        if attempt == 2 or (spec['max_runtime'] and time.monotonic() - started >= spec['max_runtime']):
                            raise RuntimeError(detail) from exc
                        delay = 1 + attempt
                        if spec['max_runtime']:
                            delay = min(delay, max(0, spec['max_runtime'] - (time.monotonic() - started)))
                        time.sleep(delay)
                store.update(job_id, remote_handle=json.dumps(remote))
            with open(job['log_path'], 'rb') as reader:
                while True:
                    done = False
                    if process is not None:
                        code = process.poll()
                        done = code is not None
                    elif remote_done:
                        done = True
                    else:
                        try:
                            info = ssh.poll(spec, remote, offset)
                            offset = info['offset']
                            output.write(info['log'].encode('utf-8'))
                            done = info['done']
                            if info['exit_code'] is not None:
                                code = info['exit_code']
                                remote_exited = True
                            remote_done = done
                            transport_failures = 0
                        except Exception as exc:
                            transport_failures += 1
                            store.update(job_id, detail=f'SSH observation unavailable: {exc}')
                            # Health deadlines remain active; transport never proves success.
                    chunk = reader.read(1024 * 1024)
                    health.feed(decoder.decode(chunk))
                    store.update(job_id, health=health.state, heartbeat=time.time())
                    if done and not chunk:
                        health.feed(decoder.decode(b'', final=True), final=True)
                        if code is None:
                            raise RuntimeError('backend completed without exit code')
                        status = 'succeeded' if code == 0 else 'failed'
                        if health.invalid_numeric:
                            status = 'failed'
                        if status == 'succeeded' and health.pattern and health.state != 'healthy':
                            status = 'startup_failed'
                        detail = f'process exited with code {code}'
                        break
                    # Remote exit precedes draining the last log chunks. Runtime
                    # and progress deadlines apply only while training may run.
                    process_exited = code is not None if process is not None else remote_exited
                    failure = health.failure() if not process_exited else None
                    if not process_exited and spec['max_runtime'] and time.monotonic() - started >= spec['max_runtime']:
                        failure = 'timed_out'
                    if failure:
                        status, detail = failure, f'{failure}: required progress or runtime deadline exceeded'
                        break
                    time.sleep(0.02 if process_exited and not transport_failures else min(0.2 * (2 ** min(transport_failures, 4)), 3))
    except BaseException as exc:
        status, detail = 'interrupted', f'{type(exc).__name__}: {exc}'
    finally:
        try:
            if process is not None:
                stop_local(process, container)
                code = process.returncode
            elif remote is not None and remote_start_confirmed and not remote_exited:
                ssh.stop(spec, remote)
        except Exception as exc:
            detail += f'; stop could not be confirmed: {exc}'
        finish(store, job_id, status, detail, code, health.state)
    # Only explicitly retryable outcomes are retried, and retries are bounded.
    for attempt in range(3):
        outcome = deliver_result(store, job_id)
        if outcome['delivery'] != 'retryable':
            break
        if attempt < 2:
            time.sleep(1 + attempt)
    return 0

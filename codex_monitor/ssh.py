"""Durable Linux jobs over OpenSSH; no credentials or SSH configuration are changed."""
import base64
import codecs
import json
import re
import shlex
import subprocess

CHUNK_SIZE = 64 * 1024

# Runs in its own session. All inherited standard streams are redirected by bootstrap.
_WRAPPER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def atomic(name, value):
    tmp = root / (name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    os.replace(tmp, root / name)
def identity(pid):
    stat = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start': stat[19], 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
def interrupted(signum, frame):
    # Repeated stop requests must not interrupt cleanup.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
child = None
code = 127
try:
    atomic('process.json', identity(os.getpid()))
    spec = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    child = subprocess.Popen(spec['command'], cwd=spec['cwd'], stdin=subprocess.DEVNULL, shell=False)
    code = child.wait()
except KeyboardInterrupt:
    if child is not None:
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    code = -15
except Exception as exc:
    print(str(exc), file=sys.stderr, flush=True)
finally:
    try:
        atomic('result.json', {'exit_code': code, 'finished_at': time.time()})
    finally:
        # Keep the session leader alive until its entire group is killed. A
        # TERM-resistant grandchild can outlive the direct child. Addressing
        # our own current group cannot target a recycled PID; also clean up
        # descendants on normal completion, as the local backend does.
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
'''

_REMOTE = r'''
import base64, fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path
request = json.loads(base64.b64decode(sys.argv[1]))
root = Path.home() / '.local' / 'state' / 'codex-monitor' / 'jobs' / request['job_id']
root.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(root, 0o700)
def read(name):
    return json.loads((root / name).read_text(encoding='utf-8'))
def atomic(name, value):
    tmp = root / (name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, root / name)
def alive(meta):
    try:
        pid = meta['pid']
        fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return (fields[0] != 'Z' and fields[19] == meta['start']
                and Path('/proc/sys/kernel/random/boot_id').read_text().strip() == meta['boot']
                and os.getpgid(pid) == pid)
    except (OSError, KeyError, ValueError):
        return False
with (root / 'lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    op = request['op']
    if op == 'start':
        if (root / 'launched').exists():
            if read('config.json') != request['spec']:
                raise RuntimeError('job ID already belongs to a different specification')
        else:
            atomic('config.json', request['spec'])
            (root / 'wrapper.py').write_text(request['wrapper'], encoding='utf-8')
            os.chmod(root / 'wrapper.py', 0o600)
            # Mark before spawning: uncertain launches must never run training twice.
            (root / 'launched').touch(mode=0o600, exist_ok=False)
            with (root / 'log.txt').open('ab', buffering=0) as log:
                subprocess.Popen([sys.executable, str(root / 'wrapper.py'), str(root)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True, close_fds=True)
        for _ in range(100):
            if (root / 'process.json').exists() or (root / 'result.json').exists():
                break
            time.sleep(0.05)
        else:
            raise RuntimeError('launch is uncertain; refusing to restart this job ID')
        response = {'job_id': request['job_id']}
    elif op == 'poll':
        offset = request['offset']
        data = b''
        if (root / 'log.txt').exists():
            with (root / 'log.txt').open('rb') as log:
                log.seek(offset)
                data = log.read(request['limit'])
        result = read('result.json') if (root / 'result.json').exists() else None
        if result is None:
            if not (root / 'process.json').exists() or not alive(read('process.json')):
                raise RuntimeError('remote job disappeared without a result; it was not restarted')
        size = (root / 'log.txt').stat().st_size if (root / 'log.txt').exists() else 0
        response = {'data': base64.b64encode(data).decode('ascii'),
                    'offset': offset + len(data), 'done': result is not None and offset + len(data) >= size,
                    'exit_code': result['exit_code'] if result is not None else None}
    elif op == 'stop':
        meta = read('process.json') if (root / 'process.json').exists() else None
        if meta is not None and alive(meta):
            try:
                os.killpg(meta['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(60):
                if not alive(meta):
                    break
                time.sleep(0.05)
            if alive(meta):
                try:
                    os.killpg(meta['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not (root / 'result.json').exists():
                atomic('result.json', {'exit_code': -15, 'finished_at': time.time()})
        elif not (root / 'result.json').exists():
            raise RuntimeError('cannot safely stop job: process identity unavailable or changed')
        response = {}
    else:
        raise ValueError('unknown operation')
print(json.dumps(response))
'''


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('job_id must be UUID hex')
    return value


def _transport(spec, request):
    host = spec.get('ssh_host', '')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*', host):
        raise ValueError('ssh_host must be a host or user@host, without options')
    python = spec.get('ssh_python', 'python3')
    if not isinstance(python, str) or not python or '\x00' in python:
        raise ValueError('ssh_python must be an executable path')
    payload = base64.b64encode(json.dumps(request).encode('utf-8')).decode('ascii')
    command = ' '.join(shlex.quote(arg) for arg in (python, '-c', _REMOTE, payload))
    completed = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
         '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', host, command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=45, check=False, shell=False,
    )
    if completed.returncode:
        raise RuntimeError('SSH transport failed: ' + completed.stderr.decode('utf-8', errors='replace')[-2000:])
    try:
        response = json.loads(completed.stdout)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('SSH returned an invalid response') from exc
    if not isinstance(response, dict):
        raise RuntimeError('SSH returned an invalid response')
    return response


def start(spec: dict, job_id: str) -> dict:
    """Launch once per ID. A failed SSH call may be retried with the same ID."""
    job_id = _job_id(job_id)
    command = spec.get('command')
    cwd = spec.get('cwd')
    if not isinstance(command, list) or not command or any(not isinstance(a, str) or '\x00' in a for a in command):
        raise ValueError('command must be a nonempty argv list')
    if not command[0] or not isinstance(cwd, str) or not cwd.startswith('/') or '\x00' in cwd:
        raise ValueError('remote cwd must be absolute and command executable nonempty')
    return _transport(spec, {'op': 'start', 'job_id': job_id,
                            'spec': {'command': command, 'cwd': cwd}, 'wrapper': _WRAPPER})


def poll(spec: dict, handle: dict, offset: int) -> dict:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError('offset must be a nonnegative byte offset')
    response = _transport(spec, {'op': 'poll', 'job_id': _job_id(handle['job_id']),
                                 'offset': offset, 'limit': CHUNK_SIZE})
    data = base64.b64decode(response['data'], validate=True)
    if len(data) > CHUNK_SIZE or response['offset'] != offset + len(data):
        raise RuntimeError('SSH returned invalid log offsets')
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    text = decoder.decode(data, final=bool(response['done']))
    pending, _ = decoder.getstate()
    return {'log': text, 'offset': response['offset'] - len(pending),
            'done': response['done'], 'exit_code': response['exit_code']}


def stop(spec: dict, handle: dict) -> None:
    """Signal only the process group with the recorded Linux process identity."""
    _transport(spec, {'op': 'stop', 'job_id': _job_id(handle['job_id'])})

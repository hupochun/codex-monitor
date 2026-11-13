"""Offline wheel acceptance in a fresh venv and unrelated working directory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wheel', type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix='codex wheel acceptance ') as temp:
        root = Path(temp).resolve()
        environment = root / 'isolated environment'
        venv.EnvBuilder(with_pip=True).create(environment)
        binaries = environment / ('Scripts' if os.name == 'nt' else 'bin')
        python = binaries / ('python.exe' if os.name == 'nt' else 'python')
        cli = binaries / ('codex-monitor.exe' if os.name == 'nt' else 'codex-monitor')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        def run(argv):
            result = subprocess.run(list(map(str, argv)), cwd=root, env=env, check=True,
                                    capture_output=True, text=True, encoding='utf-8', timeout=60)
            return result.stdout
        run([python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel])
        run([cli, '--help'])
        skill = root / 'installed skill'
        installed = json.loads(run([cli, 'install-skill', '--destination', skill]))
        assert installed['status'] == 'installed'
        assert not (skill / 'monitor-location.json').exists()
        run([python, '-I', skill / 'scripts' / 'monitor.py', '--help'])
        spec = root / 'job.json'
        state = root / 'job state'
        spec.write_text(json.dumps({'command': [str(python), '-u', '-c',
                          'print("step=0\\nstep=1\\nstep=2")'],
                          'cwd': str(root), 'bridge': {'enabled': False},
                          'health': {'pattern': r'step=(?P<step>\d+)'}}), encoding='utf-8')
        job = json.loads(run([cli, '--state-dir', state, 'submit', '--spec', spec]))
        run([cli, '--state-dir', state, 'wait-ready', job['id'], '--timeout', '10'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = json.loads(run([cli, '--state-dir', state, 'status', job['id']]))
            if job['delivery'] == 'disabled':
                break
            time.sleep(.1)
        assert (job['status'], job['health'], job['delivery']) == ('succeeded', 'healthy', 'disabled'), job
        print('PASS: offline wheel install, console entry, embedded skill install, isolated detached training.')


if __name__ == '__main__':
    main()

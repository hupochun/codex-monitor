"""JSON command interface; status never contacts the model or SSH server."""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from .runner import deliver_result, finish, launch_worker, run, validate_spec
from .store import Store, TERMINAL


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def status_view(job):
    result = dict(job)
    # A durable sending claim left by a crashed worker cannot safely be replayed.
    result['delivery_effective'] = 'confirmed' if job['delivery_confirmed'] else ('uncertain' if job['delivery'] == 'sending' else job['delivery'])
    result['heartbeat_age'] = time.time() - job['heartbeat'] if job['heartbeat'] else None
    result['monitor_warning'] = 'worker heartbeat stale; inspect worker and task, never automatically restart' if job['status'] == 'running' and result['heartbeat_age'] is not None and result['heartbeat_age'] > 60 else None
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Durable task monitor with conservative Codex event delivery')
    parser.add_argument('--state-dir', default=str(Path.home() / '.codex-monitor'))
    sub = parser.add_subparsers(dest='action', required=True)
    install = sub.add_parser('install-skill', help='Install a self-contained Codex skill')
    install.add_argument('--destination')
    install.add_argument('--upgrade', action='store_true')
    archive = sub.add_parser('build-skill', help='Build a portable skill ZIP')
    archive.add_argument('--output')
    submit = sub.add_parser('submit')
    submit.add_argument('--spec', required=True)
    status = sub.add_parser('status')
    status.add_argument('job', nargs='?')
    ready = sub.add_parser('wait-ready')
    ready.add_argument('job')
    ready.add_argument('--timeout', type=float, default=30)
    retry = sub.add_parser('retry-delivery')
    retry.add_argument('job')
    ack = sub.add_parser('ack-delivery')
    ack.add_argument('job')
    ack.add_argument('--event-id', required=True)
    ack.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID'))
    worker = sub.add_parser('_run')
    worker.add_argument('job')
    args = parser.parse_args(argv)
    try:
        if args.action == 'install-skill':
            from .distribution import install_skill
            emit(install_skill(args.destination, upgrade=args.upgrade))
            return 0
        if args.action == 'build-skill':
            from .distribution import build_skill_archive
            emit({'archive': str(build_skill_archive(args.output))})
            return 0
        store = Store(args.state_dir)
        if args.action == 'submit':
            spec = validate_spec(json.loads(Path(args.spec).read_text(encoding='utf-8-sig')))
            job_id = store.create(spec)
            try:
                launch_worker(store, job_id)
            except Exception as exc:
                finish(store, job_id, 'startup_failed', f'worker launch failed: {exc}')
                emit(status_view(store.get(job_id)))
                return 1
            emit(status_view(store.get(job_id)))
        elif args.action == 'status':
            emit(status_view(store.get(args.job)) if args.job else [status_view(j) for j in store.list()])
        elif args.action == 'wait-ready':
            if not math.isfinite(args.timeout) or args.timeout < 0:
                raise ValueError('timeout must be finite and nonnegative')
            deadline = time.monotonic() + args.timeout
            while True:
                job = store.get(args.job)
                if job['status'] in TERMINAL:
                    emit(status_view(job))
                    return 0 if job['status'] == 'succeeded' and job['health'] == 'healthy' else 1
                if job['health'] == 'healthy':
                    emit(status_view(job))
                    return 0
                if job['health'] == 'unverified' or time.monotonic() >= deadline:
                    emit(status_view(job))
                    return 2
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        elif args.action == 'retry-delivery':
            job = store.get(args.job)
            if job['status'] not in TERMINAL:
                raise ValueError('job is not terminal')
            if job['delivery'] not in ('pending', 'retryable') or job['delivery_confirmed']:
                raise ValueError('only pending or explicitly retryable delivery can be retried')
            emit(status_view(deliver_result(store, args.job)))
        elif args.action == 'ack-delivery':
            store.acknowledge(args.job, args.event_id, args.thread_id)
            emit(status_view(store.get(args.job)))
        elif args.action == '_run':
            return run(store, args.job)
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

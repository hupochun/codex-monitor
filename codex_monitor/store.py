"""SQLite is the authority for launch and delivery claims."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = frozenset({'succeeded', 'failed', 'startup_failed', 'stalled', 'timed_out', 'interrupted'})


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'state.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                health TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                worker_pid INTEGER, heartbeat REAL, exit_code INTEGER, detail TEXT,
                event_id TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending',
                delivery_detail TEXT, delivery_confirmed INTEGER NOT NULL DEFAULT 0,
                confirmed_at REAL, remote_handle TEXT)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('invalid job ID')
        return self.root / 'jobs' / job_id

    def create(self, spec):
        job_id = uuid.uuid4().hex
        self.directory(job_id).mkdir(parents=True)
        self.directory(job_id).joinpath('log.txt').touch()
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO jobs(id,spec,status,health,created,updated,event_id) VALUES(?,?,?,?,?,?,?)',
                       (job_id, json.dumps(spec), 'queued', 'starting' if spec['health'].get('pattern') else 'unverified', now, now, uuid.uuid4().hex))
        return job_id

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('unknown job ID')
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        result['job_directory'] = str(self.directory(job_id))
        result['log_path'] = str(self.directory(job_id) / 'log.txt')
        result['result_path'] = str(self.directory(job_id) / 'result.json')
        return result

    def list(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM jobs ORDER BY created DESC')]
        return [self.get(i) for i in ids]

    def update(self, job_id, **values):
        allowed = {'status', 'health', 'worker_pid', 'heartbeat', 'exit_code', 'detail', 'delivery', 'delivery_detail', 'remote_handle'}
        if not values.keys() <= allowed:
            raise ValueError('unsupported state fields')
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), job_id))

    def claim_run(self, job_id, pid):
        with self.connect() as db:
            return db.execute("UPDATE jobs SET status='running',worker_pid=?,heartbeat=?,updated=? WHERE id=? AND status='queued'", (pid, time.time(), time.time(), job_id)).rowcount == 1

    def claim_delivery(self, job_id):
        with self.connect() as db:
            return db.execute("UPDATE jobs SET delivery='sending',updated=? WHERE id=? AND status IN ('succeeded','failed','startup_failed','stalled','timed_out','interrupted') AND delivery IN ('pending','retryable') AND delivery_confirmed=0", (time.time(), job_id)).rowcount == 1

    def acknowledge(self, job_id, event_id, thread_id):
        job = self.get(job_id)
        if event_id != job['event_id'] or not thread_id or thread_id != job['spec']['thread_id']:
            raise ValueError('event ID and originating thread ID must match')
        if job['status'] not in TERMINAL:
            raise ValueError('job has no completion event yet')
        with self.connect() as db:
            db.execute('UPDATE jobs SET delivery_confirmed=1,confirmed_at=?,updated=? WHERE id=?', (time.time(), time.time(), job_id))

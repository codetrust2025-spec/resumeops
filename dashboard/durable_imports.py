"""Owner-scoped durable import queue. Uploaded documents are removed after extraction."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from threading import Event, Lock, Thread

RETENTION = 24 * 60 * 60

class ImportConflict(ValueError):
    pass


class ImportCapacity(RuntimeError):
    pass


class DurableImports:
    def __init__(self, root, draft):
        self.root = root
        self.path = root / '.resumeops-imports.sqlite3'
        self.worker_path = root / '.resumeops-import-worker.sqlite3'
        self.draft = draft
        self.wake = Event()
        self.stop_signal = Event()
        self.lock = Lock()
        self.thread = None
        with closing(self.connection()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS imports (
                owner TEXT, id TEXT, extension TEXT, digest TEXT, document BLOB,
                status TEXT, stage TEXT, created REAL, started REAL, activity REAL,
                metrics TEXT, result TEXT, PRIMARY KEY(owner,id))''')

    def freeze_review(self, owner, identifier, payload):
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT result FROM imports WHERE owner=? AND id=? AND extension='.ai' AND status='complete'",
                             (owner, identifier)).fetchone()
            if not job:
                raise ImportConflict('The draft is no longer available')
            result = json.loads(job['result'])
            previous = result.get('_queued_review')
            if previous is not None and previous != payload:
                raise ImportConflict('This draft already has a different approved resume')
            result['_queued_review'] = payload
            db.execute('UPDATE imports SET result=? WHERE owner=? AND id=?',
                       (json.dumps(result), owner, identifier))

    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        return db

    @staticmethod
    def purge(db, now):
        db.execute("DELETE FROM imports WHERE activity < ? AND status IN ('queued','complete','error')",
                   (now - RETENTION,))

    def enqueue(self, owner, identifier, extension, data):
        digest = hashlib.sha256(data).hexdigest()
        now = time.time()
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.purge(db, now)
            item = db.execute('SELECT digest,extension FROM imports WHERE owner=? AND id=?',
                              (owner, identifier)).fetchone()
            if item:
                if item['digest'] != digest or item['extension'] != extension:
                    raise ImportConflict('Import identifier already belongs to different data.')
            else:
                count = db.execute('SELECT COUNT(*) FROM imports').fetchone()[0]
                pending = db.execute("SELECT COUNT(*) FROM imports WHERE owner=? AND status IN ('queued','processing')",
                                     (owner,)).fetchone()[0]
                if count >= 128 or pending >= 4:
                    raise ImportCapacity('Import storage is busy. Your upload can retry shortly.')
                db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (owner, identifier, extension, digest, data, 'queued', 'Reading document',
                            now, None, now, None, None))
        self.start()
        self.wake.set()
        return self.get(owner, identifier)

    def get(self, owner, identifier):
        now = time.time()
        with closing(self.connection()) as db:
            item = db.execute('SELECT status,stage,started,activity,metrics,result FROM imports WHERE owner=? AND id=?',
                              (owner, identifier)).fetchone()
        if not item or (item['status'] in ('complete','error') and item['activity'] < now - RETENTION):
            return None
        data = json.loads(item['result']) if item['result'] else {}
        data.pop('_queued_review', None)
        data.update(status=item['status'], stage=item['stage'], started=item['started'],
                    activity=item['activity'], now=now)
        if item['metrics']:
            data['metrics'] = json.loads(item['metrics'])
        return data

    def start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = Thread(target=self.run, name='resumeops-imports', daemon=True)
                self.thread.start()

    def stop(self):
        self.stop_signal.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        # A separate SQLite lock remains held through inference. If the process dies, SQLite
        # releases the lock, so a new process can recover unfinished work without overlap.
        while not self.stop_signal.is_set():
            try:
                with closing(sqlite3.connect(self.worker_path, timeout=0.2)) as guard:
                    guard.execute('BEGIN EXCLUSIVE')
                    self.run_locked()
            except sqlite3.Error:
                if not self.root.is_dir():
                    return
                self.stop_signal.wait(1)

    def run_locked(self):
        with closing(self.connection()) as db, db:
            db.execute("UPDATE imports SET status='queued',stage='Reading document',started=NULL WHERE status='processing'")
        while not self.stop_signal.is_set():
            with closing(self.connection()) as db, db:
                self.purge(db, time.time())
                job = db.execute("SELECT owner,id,extension,document FROM imports WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if job:
                    db.execute("UPDATE imports SET status='processing',started=?,activity=? WHERE owner=? AND id=?",
                               (time.time(), time.time(), job['owner'], job['id']))
            if job:
                self.process(job)
            else:
                self.wake.wait(3)
                self.wake.clear()

    def process(self, job):
        key = (job['owner'], job['id'])

        def update(stage, metrics=None):
            with closing(self.connection()) as db, db:
                db.execute('UPDATE imports SET stage=?,activity=?,metrics=? WHERE owner=? AND id=?',
                           (stage, time.time(), json.dumps(metrics), *key))

        try:
            result = self.draft(job['extension'], job['document'], update)
            status, stage = 'complete', 'Draft ready for review'
        except ValueError as error:
            status, stage = 'error', 'Import failed'
            result = {'error': str(error)}
        except Exception:
            status, stage = 'error', 'Import failed'
            result = {'error': 'Could not extract this resume. Check its format and the local Ollama service, then start a new import.'}
        with closing(self.connection()) as db, db:
            db.execute('UPDATE imports SET document=NULL,status=?,stage=?,activity=?,metrics=NULL,result=? WHERE owner=? AND id=?',
                       (status, stage, time.time(), json.dumps(result), *key))

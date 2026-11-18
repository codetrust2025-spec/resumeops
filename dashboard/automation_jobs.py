"""Durable, owner-scoped one-submit preparation before the existing watcher takes over."""
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from threading import Event, Lock, Thread


DOCUMENT_LIMIT = 12 * 1024 * 1024
REQUEST_LIMIT = DOCUMENT_LIMIT * 4 // 3 + 65536
RETRY_LIMIT = 8


class AutomationCapacity(ValueError):
    pass


def submission(payload):
    from resume_bot.automation import CONSENT_VERSION, attested_profile, select_target

    if not isinstance(payload, dict) or set(payload) - {'consent', 'role', 'candidate', 'document', 'extension'}:
        raise ValueError('Expected candidate facts or a resume, a target role, and consent.')
    if payload.get('consent') != CONSENT_VERSION:
        raise ValueError('Confirm the facts and consent to publishing the resume on Overleaf.')
    role = select_target(payload.get('role'))
    candidate, document = payload.get('candidate'), payload.get('document')
    if (candidate is None) == (document is None):
        raise ValueError('Submit a candidate form or one resume file.')
    if candidate is not None:
        if not isinstance(candidate, dict) or payload.get('extension'):
            raise ValueError('Expected candidate form details.')
        candidate = {**candidate, 'role': role}
        profile = attested_profile(candidate)
        return {'consent': CONSENT_VERSION, 'role': role, 'profile': profile,
                'years': candidate.get('years_of_experience')}
    if payload.get('extension') not in ('pdf', 'docx', 'txt') or not isinstance(document, str):
        raise ValueError('Choose a PDF, DOCX or TXT file.')
    try:
        raw = base64.b64decode(document, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Could not read the uploaded file.') from error
    if not 0 < len(raw) <= DOCUMENT_LIMIT:
        raise ValueError('Choose a resume file up to 12 MB.')
    return {'consent': CONSENT_VERSION, 'role': role, 'extension': payload['extension'],
            'document': base64.b64encode(raw).decode('ascii')}


class AutomationJobs:
    def __init__(self, workspace):
        self.workspace = workspace
        self.path = workspace.root / '.resumeops-automation.sqlite3'
        self.worker_path = workspace.root / '.resumeops-automation-worker.sqlite3'
        self.wake = Event()
        self.stopping = Event()
        self.lock = Lock()
        self.thread = None
        with closing(self.connection()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS automation (
                owner TEXT NOT NULL, id TEXT NOT NULL, payload TEXT, checkpoints TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'Submission saved',
                percent INTEGER NOT NULL DEFAULT 5, created REAL NOT NULL, updated REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
                job TEXT, error TEXT, PRIMARY KEY(owner,id))''')
            # Notices: entries omitted because the source does not support them. Added in place;
            # earlier releases select columns by name, so rollback is unaffected.
            if 'notice' not in {row[1] for row in db.execute('PRAGMA table_info(automation)')}:
                db.execute('ALTER TABLE automation ADD COLUMN notice TEXT')

    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        return db

    def enqueue(self, owner, payload):
        request = submission(payload)
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        identifier = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT 1 FROM automation WHERE owner=? AND id=?', (owner, identifier)).fetchone()
            if not old:
                pending = db.execute("SELECT count(*) FROM automation WHERE status in ('queued','processing','retrying') AND owner=?",
                                     (owner,)).fetchone()[0]
                global_pending = db.execute("SELECT count(*) FROM automation WHERE payload IS NOT NULL").fetchone()[0]
                if pending >= 4 or global_pending >= 24:
                    raise AutomationCapacity('The automation queue is busy. Try again shortly.')
                db.execute('INSERT INTO automation(owner,id,payload,created,updated) VALUES (?,?,?,?,?)',
                           (owner, identifier, raw, now, now))
        self.start()
        self.wake.set()
        return identifier

    def _status(self, row, state, owner):
        current = dict(row)
        name = current.pop('job')
        if not name:
            return current
        task = state.get(name, {})
        state_name = task.get('status')
        current.update(stage=task.get('stage') or 'Waiting for the resume watcher',
                       percent=55 + 44 * max(0, min(100, task.get('percent') or 0)) // 100)
        if state_name == 'ok' and task.get('percent') == 100:
            key = self.workspace.private_revision_key(task.get('output'), owner)
            revision = self.workspace.revision(key) if key else None
            if revision and self.workspace.artifact(key, 'overleaf.pdf') and revision.get('overleaf'):
                remote = revision['overleaf']
                url = remote.get('url', '')
                if re.fullmatch(r'https://www\.overleaf\.com/project/[A-Za-z0-9_-]+', url) and remote.get('pdf_sha256'):
                    current.update(status='complete', stage='Verified resume ready', percent=100,
                                   revision=key, ats=revision.get('ats'), overleaf=url)
                    return current
            current.update(status='blocked', error='Verified outputs are missing. Contact the ResumeOps operator.')
            return current
        current['status'] = 'processing'
        if state_name == 'failed':
            if task.get('next_retry'):
                current.update(status='retrying', next_retry=task['next_retry'],
                               error=task.get('error'))
            else:
                current.update(status='blocked', error=task.get('error') or 'The watcher needs attention.')
        return current

    def list(self, owner, identity=None):
        fields = 'id, status, stage, percent, attempts, next_retry, error, created, updated, job, notice'
        with closing(self.connection()) as db:
            rows = db.execute(f"SELECT {fields}, CASE status WHEN 'needs_input' THEN checkpoints END AS waiting "
                              'FROM automation WHERE owner=? AND (? IS NULL OR id=?) ORDER BY created DESC LIMIT 50',
                              (owner, identity, identity)).fetchall()
        state = self.workspace.automation_state()
        jobs = []
        for row in rows:
            job = self._status(row, state, owner)
            job['notice'] = json.loads(job.get('notice') or '[]')
            waiting = json.loads(job.pop('waiting') or '{}')  # only the questions leave the server
            if job['status'] == 'needs_input':
                job['questions'] = [{key: question[key] for key in ('id', 'field', 'label', 'explanation', 'value')}
                                    for question in waiting.get('questions', [])]
            jobs.append(job)
        return jobs

    def answer(self, owner, identifier, answers):
        """Apply the candidate's answers to a job that needs input, then continue it where it stopped."""
        from resume_bot.automation import AnswerError, apply_answers

        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status, checkpoints FROM automation WHERE owner=? AND id=?',
                             (owner, identifier)).fetchone()
            if not row or row['status'] != 'needs_input':
                raise AnswerError('This resume is not waiting for your answers.')
            checkpoints = json.loads(row['checkpoints'])
            questions = checkpoints.pop('questions')
            facts = apply_answers(checkpoints.pop('draft'), questions, answers)
            checkpoints['facts'] = facts
            confirmed = [f'Confirmed by you: {question["label"]}.' for question in questions]
            db.execute("UPDATE automation SET status='queued',stage='Answers saved; continuing',error=NULL,"
                       'next_retry=0,checkpoints=?,notice=?,updated=? WHERE owner=? AND id=?',
                       (json.dumps(checkpoints), json.dumps(confirmed), time.time(), owner, identifier))
        self.start()
        self.wake.set()

    def start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = Thread(target=self._run, name='resumeops-automation', daemon=True)
                self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        while not self.stopping.is_set():
            try:
                with closing(sqlite3.connect(self.worker_path, timeout=0.25)) as guard:
                    guard.execute('BEGIN EXCLUSIVE')
                    self._consume()
            except sqlite3.Error:
                if not self.workspace.root.is_dir():
                    return
                self.stopping.wait(1)

    def _consume(self):
        with closing(self.connection()) as db, db:
            db.execute("UPDATE automation SET status='queued' WHERE status='processing'")
        while not self.stopping.is_set():
            if not self.workspace.automation_ready():
                self.stopping.wait(3)
                continue
            with closing(self.connection()) as db, db:
                job = db.execute("SELECT * FROM automation WHERE status IN ('queued','retrying') AND next_retry<=? ORDER BY created LIMIT 1",
                                 (time.time(),)).fetchone()
                if job:
                    db.execute("UPDATE automation SET status='processing',updated=? WHERE owner=? AND id=?",
                               (time.time(), job['owner'], job['id']))
            if job:
                self._prepare(job)
            else:
                self.wake.wait(3)
                self.wake.clear()

    def _prepare(self, job):
        from resume_bot.automation import (NeedsInput, inferred_years, role_intelligence, source_profile, tailor_facts,
                                           verified_request)
        from resume_bot.ollama import Ollama, OllamaError

        key = (job['owner'], job['id'])
        checkpoints = json.loads(job['checkpoints'])
        model = Ollama(model=self.workspace.model)
        request = json.loads(job['payload'])
        updated = lambda: time.time()

        def progress(percent, stage):
            with closing(self.connection()) as db, db:
                db.execute('UPDATE automation SET percent=max(percent,?),stage=?,updated=? WHERE owner=? AND id=?',
                           (percent, stage, updated(), *key))

        def checkpoint(name, value):
            checkpoints[name] = value
            with closing(self.connection()) as db, db:
                db.execute('UPDATE automation SET checkpoints=?,updated=? WHERE owner=? AND id=?',
                           (json.dumps(checkpoints), updated(), *key))
            return value

        def extract():
            if 'profile' in request:
                return request['profile']
            progress(10, 'Reading resume')
            return self.workspace.read_automation_source(request['extension'], base64.b64decode(request['document']))

        def extraction_progress(metrics):
            fields = min(metrics.get('fields_emitted', 0), 15)
            progress(15 + fields * 10 // 15, f'Extracting source facts with Ollama ({fields} fields)')

        def drafting_progress(metrics):
            fields = min(metrics.get('fields_emitted', 0), 8)
            progress(35 + fields, 'Ranking verified evidence with Ollama')

        try:
            source = checkpoints.get('source')
            if source is None:
                source = checkpoint('source', extract())
            facts = checkpoints.get('facts')
            if facts is None:
                facts = checkpoint('facts', source if isinstance(source, dict) else
                                   source_profile(source, model, extraction_progress))
            progress(27, 'Candidate facts validated')
            role = request['role']
            years = request.get('years', inferred_years(facts))
            intelligence = checkpoints.get('role')
            if intelligence is None:
                intelligence = checkpoint('role', role_intelligence(role, self.workspace.root))
            progress(35, 'Target role and keyword evidence prepared')
            prepared = checkpoints.get('prepared')
            if prepared is None:
                profile = tailor_facts(facts, intelligence['label'], years, intelligence, model, drafting_progress)
                packet, claims = verified_request(profile, role, years, intelligence)
                prepared = checkpoint('prepared', [packet, claims])
            packet, claims = prepared
            progress(50, 'Evidence verified and ATS inputs prepared')
            approval = {'operation': job['id'], 'approved_by': job['owner'], 'claims': claims,
                        'project_names': packet['profile'].get('projects', []),
                        'market_sources': intelligence['sources'],
                        'automation': {'consent': request['consent'], 'submission_sha256': job['id'],
                                       'source_kind': request.get('extension', 'attested form'),
                                       'source_sha256': hashlib.sha256(
                                           (source if isinstance(source, str) else
                                            json.dumps(source, sort_keys=True)).encode()).hexdigest(),
                                       'facts': facts}}
            packet['target']['automation_owner'] = job['owner']
            packet['target']['automation_id'] = job['id']
            match = self.workspace.enqueue(json.dumps(packet).encode(), approval=approval)
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='handed_off',stage='Waiting for the resume watcher',percent=55,"
                           "payload=NULL,checkpoints='{}',error=NULL,updated=?,job=? WHERE owner=? AND id=?",
                           (updated(), match['name'], *key))
        except NeedsInput as gap:
            # Keep the submission and everything already done; ask only for what the resume lacks.
            checkpoints.update(draft=gap.draft, questions=gap.questions)
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='needs_input',stage='Needs your input',error=?,checkpoints=?,"
                           'updated=? WHERE owner=? AND id=?', (str(gap), json.dumps(checkpoints), updated(), *key))
        except (OllamaError, OSError, sqlite3.Error):
            # Temporary: keep the submission and every checkpoint, so the retry resumes where it stopped.
            count = job['attempts'] + 1
            available = count < RETRY_LIMIT
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status=?,stage=?,error=?,attempts=?,next_retry=?,updated=? WHERE owner=? AND id=?",
                           ('retrying' if available else 'blocked',
                            'Retrying automatically' if available else 'Needs operator attention',
                            'A temporary Ollama or storage problem interrupted preparation.' if available else
                            'Ollama or local storage needs attention.',
                            count, updated() + min(3600, 30 * 2 ** count) if available else 0, updated(), *key))
        except ValueError as error:
            # The resume itself lacks mandatory facts: say exactly which, and stop. Never stay "in progress".
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='blocked',stage='Needs your input',error=?,payload=NULL,"
                           "checkpoints='{}',updated=? WHERE owner=? AND id=?", (str(error), updated(), *key))
        except Exception:
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='blocked',stage='Needs operator attention',error=?,payload=NULL,"
                           "checkpoints='{}',updated=? WHERE owner=? AND id=?",
                           ('Unexpected preparation failure. Contact the ResumeOps operator.', updated(), *key))

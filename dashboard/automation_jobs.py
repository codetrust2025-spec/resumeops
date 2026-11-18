"""Durable, owner-scoped one-submit preparation before the existing watcher takes over."""
import base64
import binascii
import hashlib
import json
import logging
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
            columns = {row[1] for row in db.execute('PRAGMA table_info(automation)')}
            # role/candidate: History labels. facts: the verified facts of a finished resume, owner-scoped,
            # so it can be regenerated for another role without uploading again.
            for column in ('notice', 'role', 'candidate', 'facts'):
                if column not in columns:
                    db.execute(f'ALTER TABLE automation ADD COLUMN {column} TEXT')

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
                db.execute('INSERT INTO automation(owner,id,payload,created,updated,role) VALUES (?,?,?,?,?,?)',
                           (owner, identifier, raw, now, now, request['role']))
        self.start()
        self.wake.set()
        return identifier

    # The builder's measured stages, placed in the published progress bands: ATS analysis 65-75,
    # LaTeX 75-85, PDF 85-90, Overleaf 90-95, verification 95-99; 100 only after History verifies
    # the downloads (see below).
    WATCHER_BANDS = {'validate': (65, 66), 'job': (66, 67), 'source': (67, 68), 'ai': (68, 68),
                     'checks': (68, 75), 'latex': (75, 85), 'pdf': (85, 90), 'overleaf': (90, 95), 'verify': (95, 99)}

    def _watcher_percent(self, task):
        from resume_bot.progress import BANDS
        key = task.get('stage_key')
        if key not in self.WATCHER_BANDS or key not in BANDS:
            return 65
        low, high = self.WATCHER_BANDS[key]
        _, band_low, band_high, _ = BANDS[key]
        fraction = max(0.0, min(1.0, ((task.get('percent') or band_low) - band_low) / max(1, band_high - band_low)))
        return low + round(fraction * (high - low))

    def _status(self, row, state, owner):
        current = dict(row)
        name = current.pop('job')
        if not name:
            return current
        task = state.get(name, {})
        state_name = task.get('status')
        current.update(stage=task.get('stage') or 'Waiting for the resume builder',
                       percent=self._watcher_percent(task) if task else 65)
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
        fields = 'id, status, stage, percent, attempts, next_retry, error, created, updated, job, notice, role, candidate'
        with closing(self.connection()) as db:
            rows = db.execute(f"SELECT {fields}, CASE status WHEN 'needs_input' THEN checkpoints END AS waiting, "
                              'facts IS NOT NULL AS regenerable '
                              'FROM automation WHERE owner=? AND (? IS NULL OR id=?) ORDER BY created DESC LIMIT 50',
                              (owner, identity, identity)).fetchall()
        state = self.workspace.automation_state()
        jobs = []
        for row in rows:
            job = self._status(row, state, owner)
            job['notice'] = json.loads(job.get('notice') or '[]')
            job['regenerable'] = bool(job.get('regenerable')) and job['status'] == 'complete'
            waiting = json.loads(job.pop('waiting') or '{}')  # only the questions and proposals leave the server
            if job['status'] == 'needs_input':
                job['questions'] = [{key: question[key] for key in ('id', 'field', 'label', 'explanation', 'value')}
                                    for question in waiting.get('questions', [])]
                job['proposals'] = waiting.get('proposals', [])
            jobs.append(job)
        return jobs

    def decide_projects(self, owner, identifier, decisions):
        """Apply the candidate's decision on each suggested project. Only projects they confirm, as
        they edited them, become resume content; the rest are dropped. Then the job continues."""
        from resume_bot.automation import AnswerError
        from resume_bot.generate import confirmed_project

        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status, checkpoints, notice FROM automation WHERE owner=? AND id=?',
                             (owner, identifier)).fetchone()
            checkpoints = json.loads(row['checkpoints']) if row else {}
            if not row or row['status'] != 'needs_input' or 'proposals' not in checkpoints:
                raise AnswerError('This resume is not waiting for project decisions.')
            proposals = checkpoints['proposals']
            if not isinstance(decisions, list) or len(decisions) != len(proposals):
                raise AnswerError('Choose include or skip for every suggested project.')
            confirmed, notes = [], json.loads(row['notice'] or '[]')
            for index, (proposal, decision) in enumerate(zip(proposals, decisions)):
                if not isinstance(decision, dict) or decision.get('include') not in (True, False):
                    raise AnswerError('Choose include or skip for this project.', f'project.{index}')
                if not decision['include']:
                    notes.append(f'Not included: suggested project "{proposal["name"]}".')
                    continue
                stack = decision.get('stack', proposal['stack'])
                bullets = decision.get('bullets', proposal['bullets'])
                stack = [s for s in re.split(r'[,\n]', stack)] if isinstance(stack, str) else stack
                bullets = [b for b in bullets.split('\n')] if isinstance(bullets, str) else bullets
                if not isinstance(stack, list) or not isinstance(bullets, list):
                    raise AnswerError('Project details are invalid.', f'project.{index}')
                try:
                    project = confirmed_project(decision.get('name', proposal['name']), stack, bullets)
                except ValueError as exc:
                    raise AnswerError(str(exc), f'project.{index}') from exc
                confirmed.append(project)
                notes.append(f'Confirmed by you: project "{project.split(" - ")[0]}".')
            checkpoints.pop('proposals')
            checkpoints.update(projects_reviewed=True, confirmed_projects=confirmed)
            db.execute("UPDATE automation SET status='queued',stage='Project choices saved; continuing',percent=max(percent,62),"
                       'error=NULL,next_retry=0,checkpoints=?,notice=?,updated=? WHERE owner=? AND id=?',
                       (json.dumps(checkpoints), json.dumps(notes), time.time(), owner, identifier))
        self.start()
        self.wake.set()

    def regenerate(self, owner, identifier, role):
        """A new resume for another role from a finished resume's verified facts (no new upload)."""
        from resume_bot.automation import AnswerError, CONSENT_VERSION, select_target

        role = select_target(role)
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT facts, candidate FROM automation WHERE owner=? AND id=?', (owner, identifier)).fetchone()
            if not row or not row['facts']:
                raise AnswerError('Only a finished resume can be regenerated.')
            facts = json.loads(row['facts'])
            new_id = hashlib.sha256(json.dumps({'from': identifier, 'role': role.casefold()}).encode()).hexdigest()
            if not db.execute('SELECT 1 FROM automation WHERE owner=? AND id=?', (owner, new_id)).fetchone():
                payload = json.dumps({'consent': CONSENT_VERSION, 'role': role, 'regenerated_from': identifier})
                checkpoints = json.dumps({'source': facts, 'facts': facts, 'projects_reviewed': bool(facts.get('projects'))})
                now = time.time()
                db.execute('INSERT INTO automation(owner,id,payload,checkpoints,created,updated,role,candidate,percent,stage) '
                           "VALUES (?,?,?,?,?,?,?,?,30,'Verified facts reused from an earlier resume')",
                           (owner, new_id, payload, checkpoints, now, now, role, row['candidate']))
        self.start()
        self.wake.set()
        return new_id

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
        from resume_bot.automation import (NeedsInput, inferred_years, role_intelligence, source_profile, tailor_role,
                                           verified_request)
        from resume_bot.generate import propose_projects, split_project
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
            progress(12, 'File extraction: reading the resume')
            return self.workspace.read_automation_source(request['extension'], base64.b64decode(request['document']))

        def extraction_progress(metrics):
            fields = min(metrics.get('fields_emitted', 0), 15)
            progress(12 + fields * 8 // 15, f'File extraction: candidate facts with Ollama ({fields} fields)')

        def ranking_progress(metrics):
            fields = min(metrics.get('fields_emitted', 0), 8)
            progress(40 + fields * 7 // 8, 'AI resume generation: ordering evidence for the role')

        def summary_progress(metrics):
            progress(48 + min(6, metrics.get('chunks', 0) // 20), 'AI resume generation: writing the summary')

        def proposal_progress(metrics):
            progress(56 + min(3, metrics.get('chunks', 0) // 60), 'Project selection: proposing role-specific projects')

        try:
            source = checkpoints.get('source')
            if source is None:
                source = checkpoint('source', extract())
            facts = checkpoints.get('facts')
            if facts is None:
                progress(20, 'Candidate fact validation')
                facts = checkpoint('facts', source if isinstance(source, dict) else
                                   source_profile(source, model, extraction_progress))
            with closing(self.connection()) as db, db:
                db.execute('UPDATE automation SET candidate=? WHERE owner=? AND id=?', (facts.get('full_name'), *key))
            progress(30, 'Candidate facts validated')
            role = request['role']
            years = request.get('years', inferred_years(facts))
            intelligence = checkpoints.get('role')
            if intelligence is None:
                progress(33, f'Selected role analysis: {role}')
                intelligence = checkpoint('role', role_intelligence(role, self.workspace.root))
            progress(40, 'Role keywords and skill areas prepared')
            tailored = checkpoints.get('tailored')
            if tailored is None:
                profile, how = tailor_role(facts, intelligence['label'], years, intelligence, model,
                                           ranking_progress, summary_progress)
                tailored = checkpoint('tailored', [profile, how])
            profile, how = tailored
            progress(55, 'Role-based summary, skills and evidence order prepared')
            if not facts.get('projects') and not checkpoints.get('projects_reviewed'):
                proposals = checkpoints.get('proposals_made')
                if proposals is None:
                    progress(56, 'Project selection: proposing role-specific projects')
                    proposals = checkpoint('proposals_made', propose_projects(
                        facts, {'role': intelligence['role'], 'keywords': intelligence['terms']},
                        intelligence['label'], years, model, proposal_progress))
                if proposals:
                    # Suggestions are never resume content until the candidate confirms them.
                    checkpoints['proposals'] = proposals
                    with closing(self.connection()) as db, db:
                        db.execute("UPDATE automation SET status='needs_input',stage='Review suggested projects',percent=60,"
                                   'error=?,checkpoints=?,updated=? WHERE owner=? AND id=?',
                                   (f'{len(proposals)} project suggestions are ready. Include only the ones you have '
                                    'built, edit them as needed, or skip them.', json.dumps(checkpoints), updated(), *key))
                    return
            confirmed = checkpoints.get('confirmed_projects') or []
            final_facts = {**facts, 'projects': [*(facts.get('projects') or []), *confirmed]}
            profile = {**profile, 'projects': [*(profile.get('projects') or []), *confirmed]}
            if not profile['projects']:
                profile.pop('projects')
                final_facts.pop('projects')
            packet, claims = verified_request(profile, role, years, intelligence)
            progress(65, 'Projects confirmed; facts and claims verified')
            approval = {'operation': job['id'], 'approved_by': job['owner'], 'claims': claims,
                        'project_names': [split_project(p)[0] for p in packet['profile'].get('projects', [])],
                        'market_sources': intelligence['sources'],
                        'automation': {'v': 3, 'consent': request['consent'], 'submission_sha256': job['id'],
                                       'source_kind': request.get('extension', 'regenerated' if request.get('regenerated_from')
                                                                  else 'attested form'),
                                       'source_sha256': hashlib.sha256(
                                           (source if isinstance(source, str) else
                                            json.dumps(source, sort_keys=True)).encode()).hexdigest(),
                                       'facts': final_facts, 'summary': how, 'confirmed_projects': confirmed}}
            packet['target']['automation_owner'] = job['owner']
            packet['target']['automation_id'] = job['id']
            match = self.workspace.enqueue(json.dumps(packet).encode(), approval=approval)
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='handed_off',stage='Waiting for the resume builder',percent=65,"
                           "payload=NULL,checkpoints='{}',error=NULL,updated=?,job=?,facts=? WHERE owner=? AND id=?",
                           (updated(), match['name'], json.dumps(final_facts), *key))
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
            logging.getLogger('resumeops.automation').exception('Unexpected preparation failure for %s', job['id'][:12])
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='blocked',stage='Needs operator attention',error=?,payload=NULL,"
                           "checkpoints='{}',updated=? WHERE owner=? AND id=?",
                           ('Unexpected preparation failure. Contact the ResumeOps operator.', updated(), *key))

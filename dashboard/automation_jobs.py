"""Durable, owner-scoped one-submit preparation before the existing watcher takes over."""
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from threading import Event, Lock, Thread


DOCUMENT_LIMIT = 12 * 1024 * 1024
REQUEST_LIMIT = DOCUMENT_LIMIT * 4 // 3 + 65536
RETRY_LIMIT = 8
# Public candidate intake (dashboard/intake.py) has its own capacity: its submissions never make an account's own
# submission wait or fail, and a burst of candidates is saved rather than refused.
INTAKE_SOURCE = 'intake'
INTAKE_PENDING_LIMIT = int(os.environ.get('RESUMEOPS_INTAKE_PENDING_LIMIT', '500'))
# Applications waiting at once from one network (a hashed address). A campus drive behind one address fits; a flood
# from one address stops here instead of filling the GPU queue.
INTAKE_CLIENT_LIMIT = int(os.environ.get('RESUMEOPS_INTAKE_CLIENT_LIMIT', '200'))
QUEUE_PAUSED = '.resumeops-queue-paused'  # operator switch: while present, no new preparation starts
DELETE_ONE_PHRASE = 'DELETE'
DELETE_ALL_PHRASE = 'DELETE ALL'
PROTECTED_RECORDS = '.resumeops-protected.json'  # operator-managed: {"records": ["<64 hex id>", ...]}
BUILDER_LOCK = '.resume-bot-watch.sqlite3'  # the watcher holds it on the private inbox for each scan
RETRY_DIR = '.resume-bot-retry'
TRASH = '.deleting-'


class AutomationCapacity(ValueError):
    pass


class DeleteRefused(ValueError):
    """This record cannot be deleted by this request; nothing changed."""
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class DeleteBusy(RuntimeError):
    """The resume builder is working on a resume; nothing changed."""


class DeleteFailed(RuntimeError):
    """A file or record could not be deleted; everything was put back and nothing changed."""


def protected_records(root):
    """Record ids that are never deleted. An unreadable list stops every deletion (fail closed)."""
    path = Path(root) / PROTECTED_RECORDS
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError) as error:
        raise DeleteFailed('Deletion is unavailable: the protected-records list is unreadable. '
                           'Nothing was deleted.') from error
    records = data.get('records') if isinstance(data, dict) else None
    if not isinstance(records, list) or not all(isinstance(r, str) and re.fullmatch(r'[0-9a-f]{64}', r) for r in records):
        raise DeleteFailed('Deletion is unavailable: the protected-records list is invalid. Nothing was deleted.')
    return frozenset(records)


@contextmanager
def builder_paused(private_inbox, wait):
    """Hold the watcher's private-inbox lock: no build runs and the builder state cannot change meanwhile.
    The watcher waits at most 5 s for this lock, so it is held only for the short rename-and-commit step."""
    if not Path(private_inbox).is_dir():
        yield
        return
    connection = sqlite3.connect(Path(private_inbox) / BUILDER_LOCK, timeout=wait)
    try:
        try:
            connection.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as error:
            raise DeleteBusy('ResumeOps is building a resume right now. Nothing was deleted; '
                             'try again in a minute.') from error
        yield
    finally:
        connection.close()


def _read_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise DeleteFailed('The resume builder state could not be read. Nothing was deleted; try again.') from error
    if not isinstance(state, dict):
        raise DeleteFailed('The resume builder state is invalid. Nothing was deleted.')
    return state


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _plain_entries(folder):
    """Directory entries that are not links or junctions; nothing if the folder is missing."""
    try:
        with os.scandir(folder) as entries:
            return [entry for entry in entries if not _linked(entry.stat(follow_symlinks=False))]
    except (FileNotFoundError, NotADirectoryError):
        return []


def _no_links(path):
    """Refuse to delete through a link or junction: it could point at shared files or backups."""
    pending = [Path(path)]
    while pending:
        current = pending.pop()
        info = os.lstat(current)
        if _linked(info):
            raise DeleteFailed('A linked folder was found where resume files belong. Nothing was deleted; '
                               'contact the ResumeOps operator.')
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)


def _same(path):
    if not path:
        return None
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return None


def _audit_source(folder):
    """The build request a published folder came from (its audit names it)."""
    try:
        source = json.loads((Path(folder) / 'audit.json').read_text(encoding='utf-8')).get('source')
    except (OSError, ValueError, AttributeError):
        return None
    return Path(source).name if isinstance(source, str) and source else None


def _rename(source, destination, attempts=10):
    for attempt in range(attempts):
        try:
            os.rename(source, destination)
            return
        except PermissionError:  # Windows: a scanner or reader briefly has it open
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _restore(moved):
    """Put every moved path back. One that cannot go back is never swept: its holding folder is renamed
    .undeleted-<token> for the operator to restore, and the error is logged."""
    stuck = set()
    for original, destination in reversed(moved):
        try:
            _rename(destination, original)
        except OSError:
            logging.getLogger('resumeops.automation').exception('Could not put back %s', original)
            stuck.add(destination.parent)
    for holding in stuck:
        try:
            os.rename(holding, holding.with_name(holding.name.replace(TRASH, '.undeleted-', 1)))
        except OSError:
            logging.getLogger('resumeops.automation').exception('Could not protect %s', holding)


def _writable_retry(function, path, _error):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove_tree(path):
    try:
        shutil.rmtree(path, onexc=_writable_retry)
    except OSError:
        pass  # still open somewhere: retried on the next deletion; nothing in it is reachable


def _typed_beside_documents(candidate, role):
    """Details typed in the form beside uploaded documents: every field is optional (the documents may state it)
    and is checked now, so a mistake is reported on submit. Typed values are the candidate's own statements and are
    attested as on the form-only path; a typed name, phone or email replaces what the documents say. A note is read
    with the documents. Nothing typed gives the same request as documents alone."""
    from resume_bot.automation import attested_profile

    if not isinstance(candidate, dict):
        raise ValueError('Expected candidate form details.')
    identity = {}
    for key in ('full_name', 'phone', 'email'):
        value = candidate.get(key) or ''
        if not isinstance(value, str):
            raise ValueError('Expected candidate form details.')
        if value.strip():
            identity[key] = value.strip()
    if len(identity.get('full_name', '')) > 250:
        raise ValueError('Enter the full name.')
    if 'phone' in identity and not 7 <= len(re.sub(r'\D', '', identity['phone'])) <= 15:
        raise ValueError('Enter a valid phone number.')
    if 'email' in identity and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', identity['email']):
        raise ValueError('Enter a valid email address if supplied.')
    background = candidate.get('background') or ''
    if not isinstance(background, str) or len(background) > 7500:
        raise ValueError('Work or education facts must be under 7500 characters.')
    # Stand-ins only let the form check run when the documents supply the name or phone; they are never kept.
    typed = attested_profile({**candidate, 'role': role, 'full_name': identity.get('full_name', 'Stand In'),
                              'phone': identity.get('phone', '+91 90000 00000'), 'email': identity.get('email', '')},
                             entries_required=False)
    extra = {key: value for key, value in typed.items() if key not in ('full_name', 'phone', 'email') and value}
    request = {}
    if identity:
        request['identity'] = {key: typed[key] for key in identity}
    if extra:
        request['attested'] = extra
    if background.strip():
        request['background'] = background.strip()
    if candidate.get('years_of_experience') is not None:
        request['years'] = candidate['years_of_experience']
    return request


def submission(payload):
    from resume_bot.automation import CONSENT_VERSION, attested_profile, select_target

    if not isinstance(payload, dict) or set(payload) - {'consent', 'role', 'candidate', 'document', 'extension', 'documents'}:
        raise ValueError('Expected candidate facts or evidence documents, a target role, and consent.')
    if payload.get('consent') != CONSENT_VERSION:
        raise ValueError('Confirm the facts and consent to publishing the resume on Overleaf.')
    role = select_target(payload.get('role'))
    candidate, document = payload.get('candidate'), payload.get('document')
    if 'documents' in payload:
        if document is not None or payload.get('extension'):
            raise ValueError('Submit candidate documents or a candidate form.')
        documents = payload['documents']
        if not isinstance(documents, list) or not 1 <= len(documents) <= 10:
            raise ValueError('Choose between 1 and 10 candidate documents.')
        normalized, total = [], 0
        for item in documents:
            if not isinstance(item, dict) or set(item) != {'extension', 'document'}:
                raise ValueError('Invalid candidate document.')
            accepted = submission({'consent': payload['consent'], 'role': role, **item})
            total += len(base64.b64decode(accepted['document']))
            if total > DOCUMENT_LIMIT:
                raise ValueError('Candidate documents must total 12 MB or less.')
            normalized.append({'extension': accepted['extension'], 'document': accepted['document']})
        request = {'consent': payload['consent'], 'role': role, 'documents': normalized}
        if candidate is not None:
            request.update(_typed_beside_documents(candidate, role))
        return request
    if (candidate is None) == (document is None):
        raise ValueError('Submit a candidate form or one candidate evidence document.')
    if candidate is not None:
        if not isinstance(candidate, dict) or payload.get('extension'):
            raise ValueError('Expected candidate form details.')
        candidate = {**candidate, 'role': role}
        # Every candidate form: name and phone are mandatory; email is optional but must be valid if given.
        name, phone = candidate.get('full_name'), candidate.get('phone')
        email = candidate.get('email') or ''
        if not isinstance(name, str) or not name.strip() or len(name) > 250:
            raise ValueError('Enter the full name.')
        if not isinstance(phone, str) or not 7 <= len(re.sub(r'\D', '', phone)) <= 15:
            raise ValueError('Enter a valid phone number.')
        if not isinstance(email, str) or (email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
            raise ValueError('Enter a valid email address if supplied.')
        background = candidate.get('background') or ''
        if not isinstance(background, str) or len(background) > 7500:
            raise ValueError('Work or education facts must be under 7500 characters.')
        # Every typed field, attested as on the form-only path (the note itself is read by the extraction).
        typed = attested_profile(candidate, entries_required=False)
        years = candidate.get('years_of_experience')
        if (candidate.get('work_experience') or candidate.get('education')) and not background.strip():
            return {'consent': CONSENT_VERSION, 'role': role, 'profile': typed, 'years': years}
        # A note is read by the extraction, and the typed fields travel beside it so neither is dropped
        # (merge_attested joins them). Earlier, a note discarded every optional field when no entry was
        # typed, and a typed entry discarded the note.
        request = {'consent': CONSENT_VERSION, 'role': role,
                   'identity': {'full_name': name.strip(), 'phone': phone.strip(), 'email': email.strip()},
                   'background': background.strip()}
        extra = {key: value for key, value in typed.items() if key not in ('full_name', 'phone', 'email') and value}
        if extra:
            request['attested'] = extra
        if years is not None:
            request['years'] = years
        return request
    if payload.get('extension') not in ('pdf', 'docx', 'txt', 'jpg', 'jpeg', 'png') or not isinstance(document, str):
        raise ValueError('Choose PDF, DOCX, TXT, JPG or PNG documents.')
    try:
        raw = base64.b64decode(document, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Could not read the uploaded file.') from error
    if not 0 < len(raw) <= DOCUMENT_LIMIT:
        raise ValueError('Choose a candidate evidence document up to 12 MB.')
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
        self.delete_lock = Lock()
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
            # so it can be regenerated for another role without uploading again. (A hidden_at column left by
            # the earlier soft-delete release is no longer read: deletion is permanent, see delete().)
            # origin: the candidate's own work lines (from documents, the form or typed answers), as opposed to AI
            # drafts, so a role resume never presents drafted target-role work as employment.
            # source: 'intake' for public candidate submissions, NULL for an account's own (see enqueue).
            # client: a hashed network tag of a public submission, for the per-network limit and fair order.
            for column in ('notice', 'role', 'candidate', 'facts', 'origin', 'source', 'client'):
                if column not in columns:
                    db.execute(f'ALTER TABLE automation ADD COLUMN {column} TEXT')
            # Who deleted what, and when: one row per Delete or Delete all request, kept when the resumes go (it
            # holds no resume content: the account key, the resume identifier, counts and the requesting client).
            db.execute('''CREATE TABLE IF NOT EXISTS audit (
                at REAL NOT NULL, owner TEXT NOT NULL, action TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, kept INTEGER NOT NULL DEFAULT 0, client TEXT NOT NULL DEFAULT '{}')''')

    def audit(self, owner, identifier, outcome, client=None, db=None, deleted=0, kept=0):
        """Record one deletion request. With ``db`` the row joins that transaction (a deletion and its record
        commit together); without, it is written on its own (refused or failed requests)."""
        row = (time.time(), owner, 'delete_all' if identifier is None else 'delete', identifier, outcome,
               deleted, kept, json.dumps(client or {}, sort_keys=True))
        sql = 'INSERT INTO audit (at, owner, action, target, outcome, deleted, kept, client) VALUES (?,?,?,?,?,?,?,?)'
        if db is not None:
            db.execute(sql, row)
            return
        with closing(self.connection()) as own, own:
            own.execute(sql, row)

    def audit_log(self, owner=None, limit=200):
        """The latest deletion records, newest first (one account, or all for the operator)."""
        with closing(self.connection()) as db:
            rows = db.execute('SELECT * FROM audit WHERE ? IS NULL OR owner=? ORDER BY at DESC LIMIT ?',
                              (owner, owner, limit)).fetchall()
        return [{**dict(row), 'client': json.loads(row['client'] or '{}')} for row in rows]

    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        return db

    def enqueue(self, owner, payload):
        request = submission(payload)
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            identifier = self.insert(db, owner, request)
        self.start()
        self.wake.set()
        return identifier

    @staticmethod
    def request_id(request):
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest(), raw

    def insert(self, db, owner, request, source=None, candidate=None, client=None, per_owner=True, identifier=None):
        """Save a validated request inside the caller's transaction (``BEGIN IMMEDIATE``); the same request from
        the same owner is the same job (never processed twice). Account submissions keep their limits (four in
        progress per account, 24 waiting overall); intake submissions have their own, larger limit, and at most
        INTAKE_CLIENT_LIMIT of them wait at once from one network (``client``). ``per_owner=False``: the operator
        starting candidates' applications, which the intake limits govern instead of an account's four.
        ``identifier``: the job's own id instead of the request's hash (a new version of an application)."""
        hashed, raw = self.request_id(request)
        identifier = identifier or hashed
        if db.execute('SELECT 1 FROM automation WHERE owner=? AND id=?', (owner, identifier)).fetchone():
            return identifier
        pending = db.execute("SELECT count(*) FROM automation WHERE status in ('queued','processing','retrying') AND owner=?",
                             (owner,)).fetchone()[0] if per_owner else 0
        if source == INTAKE_SOURCE:
            waiting = db.execute('SELECT count(*) FROM automation WHERE payload IS NOT NULL AND source=?',
                                 (INTAKE_SOURCE,)).fetchone()[0]
            if pending >= 4 or waiting >= INTAKE_PENDING_LIMIT:
                raise AutomationCapacity('Applications are queued to capacity right now. Your form is kept on this '
                                         'device; it is sent again automatically.')
            if client and db.execute("SELECT count(*) FROM automation WHERE source=? AND client=? AND status IN "
                                     "('queued','processing','retrying')", (INTAKE_SOURCE, client)).fetchone()[0] \
                    >= INTAKE_CLIENT_LIMIT:
                raise AutomationCapacity('Many applications from your network are waiting to be prepared. Your form '
                                         'is kept on this device; it is sent again automatically.')
        else:
            global_pending = db.execute('SELECT count(*) FROM automation WHERE payload IS NOT NULL AND source IS NULL'
                                        ).fetchone()[0]
            if pending >= 4 or global_pending >= 24:
                raise AutomationCapacity('The automation queue is busy. Try again shortly.')
        now = time.time()
        db.execute('INSERT INTO automation(owner,id,payload,created,updated,role,source,candidate,client) '
                   'VALUES (?,?,?,?,?,?,?,?,?)',
                   (owner, identifier, raw, now, now, request['role'], source, candidate, client))
        return identifier

    # Preparation order: an account's own submissions first, then public intake, each oldest first (a job that
    # waited for the candidate's review keeps its place). Within each, the queue takes turns between networks
    # (accounts): the first waiting job of every network, then the second of every network, and so on, so 200
    # applications from one address never hold back a candidate applying from another.
    ORDER = "ORDER BY (source IS NOT NULL), created"
    FAIR = ("SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY (source IS NOT NULL), COALESCE(client, owner) "
            "ORDER BY created) AS fair_rank FROM automation WHERE {where}) "
            "ORDER BY (source IS NOT NULL), fair_rank, created")

    def paused(self):
        return (self.workspace.root / QUEUE_PAUSED).is_file()

    def set_paused(self, paused):
        flag = self.workspace.root / QUEUE_PAUSED
        if paused:
            flag.write_text(json.dumps({'paused_at': time.time()}), encoding='utf-8')
        else:
            flag.unlink(missing_ok=True)
            self.wake.set()

    def queue_positions(self, db=None):
        """{(owner, id): jobs ahead} for every job waiting to be prepared, in preparation order."""
        with (nullcontext(db) if db is not None else closing(self.connection())) as connection:
            rows = connection.execute(self.FAIR.format(
                where="status IN ('queued','retrying','processing')")).fetchall()
        return {(row['owner'], row['id']): index for index, row in enumerate(rows)}

    def queue_summary(self):
        with closing(self.connection()) as db:
            counts = dict(db.execute('SELECT status, count(*) FROM automation GROUP BY status').fetchall())
            intake = dict(db.execute('SELECT status, count(*) FROM automation WHERE source=? GROUP BY status',
                                     (INTAKE_SOURCE,)).fetchall())
        return {'paused': self.paused(), 'counts': counts, 'intake': intake}

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
        ahead = self.queue_positions() if any(row['status'] in ('queued', 'retrying') for row in rows) else {}
        jobs = []
        for row in rows:
            job = self._status(row, state, owner)
            if row['status'] in ('queued', 'retrying') and (owner, row['id']) in ahead:
                job['queue_ahead'] = ahead[(owner, row['id'])]  # jobs to be prepared before this one
                job['queue_paused'] = self.paused()
            job['notice'] = json.loads(job.get('notice') or '[]')
            job['regenerable'] = bool(job.get('regenerable')) and job['status'] == 'complete'
            waiting = json.loads(job.pop('waiting') or '{}')  # only the questions and proposals leave the server
            if job['status'] == 'needs_input':
                job['questions'] = [{key: question.get(key) for key in ('id', 'field', 'label', 'explanation', 'value',
                                                                        'drafted', 'suggestions')}
                                    for question in waiting.get('questions', [])]
                job['proposals'] = waiting.get('proposals', [])
                job['certifications'] = waiting.get('certification_options', [])
            jobs.append(job)
        return jobs

    # Permanent deletion. One resume (identifier) or every resume of one owner is deleted with everything
    # built from it: the record, the build request, the builder's state entry, every output folder (PDF,
    # LaTeX, Overleaf ZIP and PDF, profile, result, audit), the approval records and any retry marker.
    #
    # Safety rules:
    # - Only the owner's own records; ids of other accounts behave as nonexistent.
    # - Records on the operator's protected list (acceptance reference records) are never deleted.
    # - A record whose preparation is running right now is kept (its files do not exist yet).
    # - Anything another remaining record still uses is kept; nothing outside the owner's private folders
    #   is touched; a link or junction anywhere in a folder stops the whole deletion.
    # - All or nothing: the builder is paused (its own lock), every path is first renamed into a hidden
    #   .deleting-<token> folder on the same volume, then the state entry and the records are deleted in one
    #   transaction. Any failure renames everything back. The hidden folders are then removed; a removal
    #   that fails (an open file) is retried on the next deletion. Nothing in them is reachable any more.
    def delete(self, owner, identifier=None, wait=10, client=None):
        """Permanently delete one resume (``identifier``) or every resume of ``owner``. Every request is recorded
        in the audit table: a deletion together with its record, a refusal or failure on its own."""
        with self.delete_lock:  # one deletion at a time: a sweep never meets another deletion's files
            protected = protected_records(self.workspace.root)
            self.purge_deleted()
            try:
                try:
                    for paused in (self._needs_builder(owner, identifier), True):
                        with builder_paused(self.workspace.private_inbox, wait) if paused else nullcontext():
                            with closing(self.connection()) as db:
                                db.execute('BEGIN IMMEDIATE')
                                try:
                                    result = self._delete(db, owner, identifier, protected, paused, client)
                                except BaseException:
                                    db.rollback()
                                    raise
                        if result is not None:
                            break
                except sqlite3.OperationalError as error:
                    raise DeleteBusy('ResumeOps is busy saving another change. Nothing was deleted; '
                                     'try again in a moment.') from error
                except OSError as error:
                    raise DeleteFailed('Resume files could not be read. Nothing was deleted; try again.') from error
            except (DeleteRefused, DeleteBusy, DeleteFailed) as error:
                outcome = {DeleteRefused: 'refused', DeleteBusy: 'busy'}.get(type(error), 'failed')
                try:
                    self.audit(owner, identifier, f'{outcome}: {error}', client)
                except sqlite3.Error:
                    logging.getLogger('resumeops.automation').exception('Could not record a refused deletion')
                raise
            self.purge_deleted()
            return result

    def _needs_builder(self, owner, identifier):
        """Deleting files the builder may use needs it paused; a record that never reached it does not."""
        if identifier is None:
            return True
        with closing(self.connection()) as db:
            row = db.execute('SELECT job FROM automation WHERE owner=? AND id=?', (owner, identifier)).fetchone()
        return bool(row and row['job'])

    def _delete(self, db, owner, identifier, protected, paused, client=None):
        rows = db.execute('SELECT id, status, job FROM automation WHERE owner=? AND (? IS NULL OR id=?)',
                          (owner, identifier, identifier)).fetchall()
        if identifier is not None:
            if not rows:
                raise DeleteRefused('This resume does not exist or was already deleted.', 404)
            if identifier in protected:
                raise DeleteRefused('This record is protected and cannot be deleted.', 403)
            if rows[0]['status'] == 'processing':
                raise DeleteRefused('This resume is being prepared right now. Delete it when it finishes.', 409)
        kept = [{'id': row['id'], 'reason': 'protected' if row['id'] in protected else 'in_progress'}
                for row in rows if row['id'] in protected or row['status'] == 'processing']
        targets = {row['id']: row['job'] for row in rows if row['id'] not in protected and row['status'] != 'processing'}
        if not paused and (identifier is None or any(targets.values())):
            return None  # it reached the builder meanwhile: start again with the builder paused
        staying = {row['job'] for row in db.execute('SELECT owner, id, job FROM automation WHERE job IS NOT NULL')
                   if not (row['owner'] == owner and row['id'] in targets)}
        state_path = self.workspace.private_inbox / '.resume-bot-state.json'
        state = _read_state(state_path)
        # Delete all also clears this owner's leftovers (older builds, interrupted build folders), but not
        # while one of their resumes is being prepared: that preparation is about to publish its own files.
        sweep = identifier is None and not any(item['reason'] == 'in_progress' for item in kept)
        jobs = {job for job in targets.values() if job and job not in staying}
        paths, drop = self._owned_paths(owner, jobs, state, staying, sweep)
        token = secrets.token_hex(8)
        moved = []
        try:
            for path, _ in paths:
                _no_links(path)  # checked for every path before anything moves
            for path, root in paths:
                holding = root / (TRASH + token)
                holding.mkdir(exist_ok=True)
                destination = holding / str(len(moved))
                _rename(path, destination)
                moved.append((path, destination))
        except (OSError, DeleteFailed) as error:
            _restore(moved)
            if isinstance(error, DeleteFailed):
                raise
            raise DeleteFailed('A file could not be deleted (it may be open). Nothing was deleted; try again.') from error
        from resume_bot.storage import write_json
        try:
            if drop:
                write_json(state_path, {key: value for key, value in state.items() if key not in drop})
            db.executemany('DELETE FROM automation WHERE owner=? AND id=?', ((owner, key) for key in targets))
            self.audit(owner, identifier, 'deleted', client, db=db, deleted=len(targets), kept=len(kept))
            db.commit()
        except Exception as error:
            db.rollback()
            if drop:
                try:
                    write_json(state_path, state)
                except OSError:
                    logging.getLogger('resumeops.automation').exception('Could not restore the builder state')
            _restore(moved)
            raise DeleteFailed('The deletion could not be recorded. Nothing was deleted; try again.') from error
        return {'deleted': len(targets), 'kept': kept}

    def _owned_paths(self, owner, jobs, state, staying, sweep):
        """Every file and folder built for the given jobs of one owner, as (path, same-volume root)."""
        inbox, approvals = self.workspace.private_inbox, self.workspace.approvals
        output_root = self.workspace.private_output
        folder_root = output_root / ('private-' + owner)
        name = re.compile(r'private-' + re.escape(owner) + r'-[0-9a-f]{64}\.json')
        if sweep:
            found = set(jobs) | {key for key in state if name.fullmatch(key)}
            found |= {entry.name for entry in _plain_entries(inbox) if name.fullmatch(entry.name)}
            found |= {entry.name + '.json' for entry in _plain_entries(approvals) if name.fullmatch(entry.name + '.json')}
            jobs = {job for job in found if job not in staying}
        jobs = {job for job in jobs if name.fullmatch(job)}  # never a job of another owner
        keep = {_same(state[key].get('output')) for key in staying if isinstance(state.get(key), dict)} - {None}
        current = {_same(state[key].get('output')) for key in jobs if isinstance(state.get(key), dict)} - {None}
        paths = []
        for job in sorted(jobs):
            for path, root in ((inbox / job, inbox), (inbox / RETRY_DIR / job, inbox),
                               (approvals / job[:-len('.json')], approvals)):
                if os.path.lexists(path):
                    paths.append((path, root))
        for entry in _plain_entries(folder_root):
            folder = Path(entry.path)
            if not entry.is_dir(follow_symlinks=False) or _same(folder) in keep:
                continue
            source = _audit_source(folder)
            if (_same(folder) in current or source in jobs
                    or (sweep and (entry.name.startswith('resume-build-') or (source and source not in staying)))):
                paths.append((folder, output_root))
        return paths, [job for job in jobs if job in state]

    def purge_deleted(self):
        """Remove what deletions moved aside. Their records are already gone; failures retry next time."""
        for root in (self.workspace.private_inbox, self.workspace.approvals, self.workspace.private_output):
            for entry in _plain_entries(root):
                if entry.name.startswith(TRASH) and entry.is_dir(follow_symlinks=False):
                    _remove_tree(entry.path)

    @staticmethod
    def _legacy_origin(facts, notice):
        """For a resume finished before origins were recorded: the work lines of every job whose lines were not
        confirmed from an AI draft (its notices say which were)."""
        notes = json.loads(notice or '[]')
        drafted = lambda job: any(note.startswith('Reviewed and confirmed by you (AI draft): Work as ' + job['position'])
                                  for note in notes)
        return {'bullets': [line for job in facts['experience'] if not drafted(job) for line in job['bullets']]}

    @staticmethod
    def _draft_question(facts, index, lines, label, carry=()):
        """The work question for one job: the candidate's own lines first, then the AI drafts for the role, all
        editable; or an empty field when nothing could be drafted. The candidate's lines about another field's
        work (``carry``) are not shown beside the drafts: they stay in the stored facts for other roles and are
        never printed on this role's resume."""
        entry = facts['experience'][index]
        carry = [line for line in carry if line in (entry.get('bullets') or [])] if lines else []
        own = [line for line in entry.get('bullets') or [] if line not in carry]
        place = ' at ' + entry['company'] if entry.get('company') else ''
        dates = ' - '.join(value for value in (entry.get('start'), entry.get('end')) if value)
        # A job the documents give only an employer and dates for is named by them (no title is invented).
        title = ('Work as ' + entry['position'] + place if entry.get('position') else
                 'Work' + place + (f' ({dates})' if dates else ''))
        explanation = (f'AI drafted {label} lines from your '
                       + ('job title and dates' if entry.get('position') else 'employer and dates')
                       + (' below your own lines' if own else '')
                       + '. Keep only what you actually did; edit or delete any line. These are drafts until you '
                       'confirm them.' + (' Your lines about other work are kept for other roles.' if carry else '')
                       if lines else 'In your own simple words, what did you do? One line each. Leave '
                       'it empty to list this job without details.')
        question = {'id': f'experience.{index}.bullets', 'section': 'experience', 'entry': index, 'field': 'bullets',
                    'drafted': bool(lines), 'value': '\n'.join([*own, *(lines or [])]),
                    'label': title + (' (AI draft)' if lines else ''),
                    'explanation': explanation}
        if carry:
            question['carry'] = carry
        return question

    @staticmethod
    def _project_choices(checkpoints, notes, decisions):
        """Apply the candidate's decision on each suggested project. Only projects they confirm, as they
        edited them, become resume content; the rest are dropped."""
        from resume_bot.automation import AnswerError
        from resume_bot.generate import confirmed_project

        proposals = checkpoints['proposals']
        if not isinstance(decisions, list) or len(decisions) != len(proposals):
            raise AnswerError('Choose include or skip for every suggested project.')
        confirmed = []
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

    @staticmethod
    def _certification_choices(checkpoints, notes, certifications):
        """The certifications the candidate says they have completed: recommendations they ticked, or names they
        typed. Recommendations left unticked are never printed, and nothing is added otherwise."""
        from resume_bot.automation import AnswerError
        from resume_bot.generate import plain

        if checkpoints.pop('certification_options', None) is None:
            return
        checkpoints['certifications_reviewed'] = True
        if not certifications:
            return
        if not isinstance(certifications, list) or len(certifications) > 8 or \
                any(not isinstance(name, str) for name in certifications):
            raise AnswerError('List up to 8 certifications you have completed.', 'certifications')
        facts = checkpoints['facts']
        have = {name.casefold() for name in facts.get('certifications') or []}
        for name in certifications:
            name = ' '.join(plain(name).split())
            if len(name) > 120:
                raise AnswerError('Keep each certification under 120 characters.', 'certifications')
            if name and name.casefold() not in have:
                facts.setdefault('certifications', []).append(name)
                have.add(name.casefold())
                notes.append(f'Confirmed by you: certification "{name}".')

    def decide_projects(self, owner, identifier, decisions, certifications=None):
        """Project decisions alone (no other question pending). Then the job continues."""
        from resume_bot.automation import AnswerError

        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status, checkpoints, notice FROM automation WHERE owner=? AND id=?',
                             (owner, identifier)).fetchone()
            checkpoints = json.loads(row['checkpoints']) if row else {}
            if not row or row['status'] != 'needs_input' or 'proposals' not in checkpoints:
                raise AnswerError('This resume is not waiting for project decisions.')
            if checkpoints.get('questions'):
                raise AnswerError('Confirm the drafted details together with the projects.')
            notes = json.loads(row['notice'] or '[]')
            self._project_choices(checkpoints, notes, decisions)
            self._certification_choices(checkpoints, notes, certifications)
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
            row = db.execute('SELECT facts, candidate, origin, notice, source, client FROM automation '
                             'WHERE owner=? AND id=?', (owner, identifier)).fetchone()
            if not row or not row['facts']:
                raise AnswerError('Only a finished resume can be regenerated.')
            facts = json.loads(row['facts'])
            origin = json.loads(row['origin']) if row['origin'] else self._legacy_origin(facts, row['notice'])
            new_id = hashlib.sha256(json.dumps({'from': identifier, 'role': role.casefold()}).encode()).hexdigest()
            if not db.execute('SELECT 1 FROM automation WHERE owner=? AND id=?', (owner, new_id)).fetchone():
                payload = json.dumps({'consent': CONSENT_VERSION, 'role': role, 'regenerated_from': identifier})
                # The new role decides again: its work lines, skills and projects are drafted for it when the facts lack
                # them (a GCP resume regenerated for Java Backend gets Java Backend work lines to confirm, never GCP
                # lines on a Java resume).
                checkpoints = json.dumps({'source': facts, 'facts': facts})
                now = time.time()
                # A candidate's regenerated resume stays in the intake queue (source), like the one it came from.
                db.execute('INSERT INTO automation(owner,id,payload,checkpoints,created,updated,role,candidate,percent,stage,'
                           "origin,source,client) VALUES (?,?,?,?,?,?,?,?,30,'Verified facts reused from an earlier resume',"
                           '?,?,?)',
                           (owner, new_id, payload, checkpoints, now, now, role, row['candidate'], json.dumps(origin),
                            row['source'], row['client']))
        self.start()
        self.wake.set()
        return new_id

    def _edit_sources(self, owner):
        """The owner's finished resumes (newest first): those with verified facts."""
        with closing(self.connection()) as db:
            rows = db.execute('SELECT id, candidate, role, facts, updated FROM automation WHERE owner=? AND facts IS NOT '
                              'NULL ORDER BY updated DESC', (owner,)).fetchall()
        return [{'id': row['id'], 'candidate': row['candidate'] or '', 'role': row['role'] or '',
                 'updated': row['updated'], 'facts': json.loads(row['facts'])} for row in rows]

    def edit_preview(self, owner, message='', identifier=None, jd=''):
        """AI Edit: the proposed changes for one resume (named in the request, or chosen), for approval. Nothing is
        changed here. A correction request and a job description can be given together."""
        from resume_bot.ai_edit import EditError, corrections, role_change, select_resume, tailor
        from resume_bot.ollama import Ollama
        message, jd = ' '.join(str(message or '').split())[:2000], str(jd or '')[:20000]
        if not message and not jd.strip():
            raise EditError('Describe the correction, or paste a job description.')
        sources = self._edit_sources(owner)
        if identifier:
            # Per-resume AI Edit: exactly this resume, whatever names the request mentions.
            resume = next((item for item in sources if item['id'] == identifier), None)
            if not resume:
                raise EditError('Only a finished resume can be edited.')
        else:
            # Global AI Edit: the candidate by name, the resume by role (newest version); ask only when ambiguous.
            if not sources:
                raise EditError('There is no finished resume to edit yet.')
            kind, found = select_resume(sources, message)
            if kind == 'choose':
                return {'choose': [{'id': item['id'], 'candidate': item['candidate'], 'role': item['role'],
                                    'updated': item['updated'], 'versions': item['versions']} for item in found]}
            resume = found
        facts, role = resume['facts'], resume['role']
        versions = sum(1 for item in sources if item['candidate'].casefold() == resume['candidate'].casefold()
                       and item['role'].casefold() == role.casefold())
        result = {'id': resume['id'], 'candidate': resume['candidate'], 'role': role, 'updated': resume['updated'],
                  'versions': versions, 'edits': [], 'keywords': [], 'gaps': []}
        if jd.strip():
            tailored = tailor(facts, jd, role)
            result.update(keywords=tailored['keywords'], gaps=tailored['gaps'], edits=tailored['edits'],
                          strengths=tailored['strengths'])
        retarget = role_change(message, role) if message else None
        if retarget:
            result['edits'].append(retarget)  # regeneration for another role, through the same approval and apply
        elif message and not (jd.strip() and not re.search(r'\b(?:change|update|correct|fix|set|replace|add|remove|'
                                                            r'delete)\b', message, re.I)):
            result['edits'] += corrections(facts, message, Ollama(model=self.workspace.model))
        if not result['edits'] and not result['keywords']:
            raise EditError('No change to propose: the resume already matches.')
        return result

    def edit_apply(self, owner, identifier, edits, keywords=None, strengths=None):
        """A new version of a finished resume with the approved changes (the original stays). Every change is checked
        against the current facts; the new version is built by the normal pipeline (PDF, LaTeX, ATS, Overleaf)."""
        from resume_bot.ai_edit import EditError, apply_edits
        from resume_bot.automation import CONSENT_VERSION, select_target
        if not isinstance(edits, list) or len(edits) > 40:
            raise EditError('Choose the changes to apply.')
        keywords = [' '.join(str(k).split())[:60] for k in (keywords or []) if str(k).strip()][:40]
        if not edits and not keywords:
            raise EditError('Choose at least one change.')
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT facts, candidate, role, origin, notice, source, client FROM automation '
                             'WHERE owner=? AND id=?', (owner, identifier)).fetchone()
            if not row or not row['facts']:
                raise EditError('Only a finished resume can be edited.')
            facts, role, notes = apply_edits(json.loads(row['facts']), edits)
            role = select_target(role or row['role'])
            origin = json.loads(row['origin']) if row['origin'] else self._legacy_origin(facts, row['notice'])
            # Work lines the user wrote or corrected are their own attested words.
            origin['bullets'] = list(dict.fromkeys([*origin.get('bullets', []), *(
                e['new'] for e in edits if isinstance(e, dict) and '.bullets.' in str(e.get('path')) and e.get('new'))]))
            if keywords:
                notes.append('Tailored to a job description: ' + ', '.join(keywords[:12]) + '.')
            now = time.time()
            new_id = hashlib.sha256(json.dumps({'from': identifier, 'edits': edits, 'keywords': keywords, 'at': now},
                                               sort_keys=True, default=str).encode()).hexdigest()
            strengths = [' '.join(str(s).split())[:40] for s in (strengths or []) if str(s).strip()][:4]
            payload = json.dumps({'consent': CONSENT_VERSION, 'role': role, 'regenerated_from': identifier,
                                  'edited_from': identifier, 'jd_keywords': keywords, 'jd_strengths': strengths})
            db.execute('INSERT INTO automation(owner,id,payload,checkpoints,created,updated,role,candidate,percent,stage,'
                       "origin,source,client,notice) VALUES (?,?,?,?,?,?,?,?,30,'AI Edit approved: building the new version',"
                       '?,?,?,?)',
                       (owner, new_id, payload, json.dumps({'source': facts, 'facts': facts}), now, now, role,
                        facts.get('full_name') or row['candidate'], json.dumps(origin), row['source'], row['client'],
                        json.dumps(notes)))
        self.start()
        self.wake.set()
        return new_id

    def answer(self, owner, identifier, answers, decisions=None, certifications=None):
        """Apply the candidate's answers to a job that needs input, then continue it where it stopped. When the
        same review also holds project suggestions, their decisions come with the answers (one confirmation)."""
        from resume_bot.automation import AnswerError, apply_answers

        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status, checkpoints, notice, origin FROM automation WHERE owner=? AND id=?',
                             (owner, identifier)).fetchone()
            if not row or row['status'] != 'needs_input':
                raise AnswerError('This resume is not waiting for your answers.')
            checkpoints = json.loads(row['checkpoints'])
            project_notes = []
            if 'proposals' in checkpoints and checkpoints.get('questions'):
                self._project_choices(checkpoints, project_notes, decisions)
            if checkpoints.get('document_intake'):
                note = answers.get('source_text') if isinstance(answers, dict) else None
                if not isinstance(note, str) or not 20 <= len(note.strip()) <= 7500:
                    raise AnswerError('Add the missing candidate facts (20-7500 characters).', 'source_text')
                checkpoints['source'] += '\nCandidate-provided facts:\n' + note.strip()
                for field in ('document_intake', 'draft', 'questions'):
                    checkpoints.pop(field, None)
                confirmed = ['Candidate supplied additional facts alongside document evidence.']
            elif 'identity_intake' in checkpoints:
                note = answers.get('source_text') if isinstance(answers, dict) else None
                if not isinstance(note, str) or not 20 <= len(note.strip()) <= 7500:
                    raise AnswerError('Add a short factual work or education history (20-7500 characters).',
                                      'source_text')
                identity = checkpoints.pop('identity_intake')
                prefix = 'Full name: ' + identity['full_name'] + '\nPhone: ' + identity['phone'] + '\n'
                if identity.get('email'):
                    prefix += 'Email: ' + identity['email'] + '\n'
                checkpoints['source'] = prefix + '\nCandidate-provided facts:\n' + note.strip()
                checkpoints.pop('questions', None)
                confirmed = ['Candidate supplied additional source facts for AI extraction.']
            else:
                questions = checkpoints.pop('questions')
                facts = apply_answers(checkpoints.pop('draft'), questions, answers)
                for question in questions:
                    if question['field'] == 'bullets' and question.get('carry'):
                        # The candidate's lines about another field's work stay in the facts, for other roles.
                        job = facts['experience'][question['entry']]
                        job['bullets'] += [line for line in question['carry'] if line not in job['bullets']]
                    if question['field'] == 'skills' and question.get('carry'):
                        # Skills for other fields stay in the facts (off this role's resume, kept for other roles).
                        have = {s.casefold() for s in facts['skills']}
                        facts['skills'] += [s for s in question['carry'] if s.casefold() not in have]
                checkpoints['facts'] = facts
                self._certification_choices(checkpoints, project_notes, certifications)
                if any(question['field'] == 'bullets' for question in questions):
                    checkpoints['drafts_reviewed'] = True  # a job left without lines is not asked about again
                if any(question['field'] == 'skills' for question in questions):
                    checkpoints['skills_reviewed'] = checkpoints.pop('skills_review_role', True)
                # What the candidate confirmed for a job (their own words, or AI drafts they reviewed and kept) is their
                # attested work. An unconfirmed draft never reaches the resume: it only ever lives in the review card.
                typed = [line for question in questions if question['field'] == 'bullets'
                         for line in facts['experience'][question['entry']]['bullets']]
                if typed:
                    origin = json.loads(row['origin']) if row['origin'] else {'bullets': []}
                    origin['bullets'] = list(dict.fromkeys([*origin['bullets'], *typed]))
                    db.execute('UPDATE automation SET origin=? WHERE owner=? AND id=?',
                               (json.dumps(origin), owner, identifier))
                confirmed = [('Reviewed and confirmed by you (AI draft): ' if question.get('drafted') else
                              'Confirmed by you: ') + re.sub(r' \((?:AI draft|with AI suggestions)\)$', '',
                                                            question['label']) + '.'
                             for question in questions]
            db.execute("UPDATE automation SET status='queued',stage='Answers saved; continuing',error=NULL,"
                       'next_retry=0,checkpoints=?,notice=?,updated=? WHERE owner=? AND id=?',
                       (json.dumps(checkpoints), json.dumps(confirmed + project_notes), time.time(), owner, identifier))
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
        self._recheck_stale_questions()
        self._backfill_labels()
        while not self.stopping.is_set():
            if not self.workspace.automation_ready() or self.paused():
                self.stopping.wait(3)
                continue
            with closing(self.connection()) as db, db:
                job = db.execute(self.FAIR.format(where="status IN ('queued','retrying') AND next_retry<=?")
                                 + ' LIMIT 1', (time.time(),)).fetchone()
                if job:
                    db.execute("UPDATE automation SET status='processing',updated=? WHERE owner=? AND id=?",
                               (time.time(), job['owner'], job['id']))
            if job:
                self._prepare(job)
            else:
                self.wake.wait(3)
                self.wake.clear()

    def _recheck_stale_questions(self):
        """A job waiting on questions that an older extraction asked is read again from its original
        upload: an extraction fix must not leave a user answering for facts the resume already states.
        Unanswered jobs only; the candidate's answers and confirmed choices are never discarded."""
        from resume_bot.automation import EXTRACTION_VERSION

        with closing(self.connection()) as db, db:
            rows = db.execute("SELECT owner, id, checkpoints FROM automation WHERE status='needs_input'").fetchall()
            for row in rows:
                checkpoints = json.loads(row['checkpoints'] or '{}')
                if ('questions' not in checkpoints or not isinstance(checkpoints.get('source'), str)
                        or checkpoints.get('extraction') == EXTRACTION_VERSION):
                    continue
                for key in ('questions', 'draft', 'extraction'):
                    checkpoints.pop(key, None)
                db.execute("UPDATE automation SET status='queued',stage='Reading the resume again with improved extraction',"
                           'error=NULL,next_retry=0,checkpoints=?,updated=? WHERE owner=? AND id=?',
                           (json.dumps(checkpoints), time.time(), row['owner'], row['id']))

    def _backfill_labels(self):
        """History labels (role, candidate) for jobs handed off before they were recorded, read from the job's
        own verified build request. A request is used only when its owner and job id match the row."""
        from resume_bot.workflow import load_roles, normalize_request, role_name

        load_roles()
        with closing(self.connection()) as db, db:
            rows = db.execute('SELECT owner, id, job, role, candidate FROM automation '
                              'WHERE job IS NOT NULL AND (role IS NULL OR candidate IS NULL)').fetchall()
            for row in rows:
                try:
                    packet = json.loads((self.workspace.private_inbox / row['job']).read_text(encoding='utf-8'))
                    profile, target = normalize_request(packet)
                    if not target or target.get('automation_owner') != row['owner'] or target.get('automation_id') != row['id']:
                        continue
                    role, candidate = role_name(target), profile.get('full_name')
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                db.execute('UPDATE automation SET role=COALESCE(role,?), candidate=COALESCE(candidate,?) '
                           'WHERE owner=? AND id=?', (role, candidate, row['owner'], row['id']))

    def _prepare(self, job):
        from resume_bot.automation import (EXTRACTION_VERSION, NeedsInput, inferred_years, merge_attested,
                                           role_intelligence, source_profile, tailor_role, verified_request)
        from resume_bot.generate import (GENERIC_TOOLS, content_problems, draft_role_content, is_job_title,
                                         lacks_role_core, present_for_role, propose_projects, role_projects,
                                         role_vocabulary, same_field, skill_fits_role, skill_groups, split_project,
                                         technical_job, without_note, work_skills, named_tools, catalog_terms, draft_lines,
                                         other_field_tools)
        from resume_bot.workflow import ROLES
        from resume_bot.workflow import matched_terms
        from resume_bot.ollama import Ollama, OllamaError

        key = (job['owner'], job['id'])
        # A candidate's application (started by the operator, who confirms the drafts on the candidate's behalf):
        # drafts and proposed projects name only the candidate's own technologies (generate.evidence_tools).
        grounded = 'source' in job.keys() and job['source'] == INTAKE_SOURCE
        # Projects that make suggestions unnecessary: those about the role, or in an application every project the
        # candidate entered (all of them are printed, see present_for_role keep_own).
        own_projects = lambda facts: (facts.get('projects') or []) if grounded else role_projects(facts, target)
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
            if 'documents' in request:
                sources = []
                for index, item in enumerate(request['documents'], 1):
                    progress(12, f'File extraction: reading document {index} of {len(request["documents"])}')
                    try:
                        text = self.workspace.read_automation_source(item['extension'], base64.b64decode(item['document']))
                    except ValueError as error:
                        raise ValueError(f'Document {index}: {error}') from error
                    sources.append(f'Candidate evidence document {index}\n{text}')
                    if sum(map(len, sources)) > 75000:
                        raise ValueError('The combined document text is too long. Submit fewer documents.')
                if request.get('background'):  # the note typed beside the documents is read with them
                    sources.append('Candidate-provided facts\n' + request['background'])
                    if sum(map(len, sources)) > 75000:
                        raise ValueError('The combined document text is too long. Submit fewer documents.')
                return '\n\n'.join(sources)
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

        def draft_progress(metrics):
            progress(28 + min(1, metrics.get('chunks', 0) // 200), 'AI drafting role-specific content for your review')

        def offer_certifications(facts):
            """Certifications worth earning for the role, shown unticked in the review card (once per resume):
            only those the candidate ticks as completed are printed."""
            from resume_bot.certifications import recommended_certifications
            if not checkpoints.get('certifications_reviewed'):
                checkpoints['certification_options'] = recommended_certifications(
                    {'role': intelligence['role'], 'keywords': intelligence['terms'], 'role_title': intelligence['label']},
                    facts.get('certifications') or [])

        try:
            source = checkpoints.get('source')
            if source is None and 'identity' in request and 'documents' not in request:
                identity = request['identity']
                background = request.get('background') or ''
                if background:
                    source = 'Full name: ' + identity['full_name'] + '\nPhone: ' + identity['phone'] + '\n'
                    if identity.get('email'):
                        source += 'Email: ' + identity['email'] + '\n'
                    source += '\nCandidate-provided facts:\n' + background
                    source = checkpoint('source', source)
                else:
                    checkpoints['identity_intake'] = identity
                    checkpoints['questions'] = [{'id': 'source_text', 'section': 'profile', 'entry': None,
                                                 'field': 'source_text',
                                                 'label': 'Your actual work or education',
                                                 'explanation': 'Share your actual employer, title, dates, college '
                                                                'or completed project details. AI writes the resume '
                                                                'but cannot discover your career from a phone number.',
                                                 'value': ''}]
                    with closing(self.connection()) as db, db:
                        db.execute("UPDATE automation SET status='needs_input',stage='Awaiting real work or education facts',"
                                   "percent=20,error=?,checkpoints=?,updated=? WHERE owner=? AND id=?",
                                   ('A name and phone cannot establish work history or education. '
                                    'Add a short factual note or upload an existing resume.',
                                    json.dumps(checkpoints), updated(), *key))
                    return
            if source is None:
                source = checkpoint('source', extract())
            facts = checkpoints.get('facts')
            if facts is None:
                progress(20, 'Candidate fact validation')
                facts = checkpoint('facts', source if isinstance(source, dict) else
                                   source_profile(source, model, extraction_progress,
                                                  identity=request.get('identity'),
                                                  evidence_documents='documents' in request,
                                                  attested=request.get('attested')))
            if request.get('attested'):
                facts = merge_attested(facts, request['attested'])  # idempotent: safe after answers and retries
            with closing(self.connection()) as db, db:
                db.execute('UPDATE automation SET candidate=? WHERE owner=? AND id=?', (facts.get('full_name'), *key))
            role = request['role']
            years = request.get('years')
            if years is None:
                years = inferred_years(facts)
            intelligence = checkpoints.get('role')
            if intelligence is None:
                progress(26, f'Selected role analysis: {role}')
                intelligence = checkpoint('role', role_intelligence(role, self.workspace.root))
            if request.get('jd_keywords'):
                # AI Edit, tailored to a job description: its keywords the candidate's facts show lead the role's
                # terms (skills and evidence order, ATS keywords). Content still comes from the facts only.
                jd_terms = [t for t in request['jd_keywords'] if isinstance(t, str)]
                intelligence = {**intelligence, 'terms': list(dict.fromkeys([*jd_terms, *intelligence['terms']]))}
            if request.get('jd_strengths'):
                # The Career Objective's two priorities, as the job description asks for them (within the role's
                # approved priorities: the objective stays role-wide and names no tools, projects or business areas).
                intelligence = {**intelligence, 'jd_strengths': [s for s in request['jd_strengths'] if isinstance(s, str)]}
            target = {'role': intelligence['role'], 'keywords': intelligence['terms']}
            label = intelligence['label']
            # A designation is never a skill, and extraction notes are never resume text (any source, including a
            # resume regenerated from earlier facts).
            positions = [entry['position'] for entry in facts['experience']]
            facts = {**facts, 'skills': [s for s in facts.get('skills', []) if not is_job_title(s, positions)],
                     'education': [{**entry, 'degree': without_note(entry['degree']) or entry['degree'],
                                    'details': without_note(entry.get('details', ''))} for entry in facts['education']]}
            # The candidate's own work lines (documents, form, or typed answers), as opposed to AI drafts.
            origin = json.loads(job['origin']) if job['origin'] else None
            if origin is None:
                origin = {'bullets': [line for entry in facts['experience'] for line in entry['bullets']]}
                with closing(self.connection()) as db, db:
                    db.execute('UPDATE automation SET origin=? WHERE owner=? AND id=?', (json.dumps(origin), *key))
            # The selected role decides the resume. What the documents do not give for it (the work done in a job, the
            # skills the role needs, projects in its field) is drafted by AI and shown pre-filled with the project
            # suggestions, for one confirmation. Drafts never become resume content unconfirmed.
            # Jobs to draft: those without lines, and technical jobs whose lines barely touch the selected role, when
            # the job is in another field (a career change) or most of its lines are another field's work (a React.js
            # resume regenerated from GCP lines): the selected role decides the experience section.
            core = [t for t in role_vocabulary(target) if not matched_terms(GENERIC_TOOLS, t)]
            tools = sorted(set(named_tools()) | set(catalog_terms()))
            thin = lambda entry: (technical_job(entry['position'])
                                  and sum(bool(matched_terms(core, line)) for line in entry['bullets']) < 2
                                  and (not same_field(entry['position'], label)
                                       or 2 * sum(bool(other_field_tools(line, tools, target)) for line in entry['bullets'])
                                       > len(entry['bullets'])))
            # A technical job whose own lines for the role cover less than half of the lines it is drafted to (about
            # eight for the current job, seven for the one before, fewer for older ones) gets drafted additions for
            # the candidate to confirm: two stated lines never stand for a whole job. Jobs with substantial own
            # content keep exactly what the candidate wrote.
            sparse = lambda index, entry: (technical_job(entry['position']) and 2 * len(
                [line for line in entry['bullets'] if not other_field_tools(line, tools, target)])
                < draft_lines(index, entry['position']))
            # Every upload gets this, including one that states only employers and dates (a candidate form): the
            # AI drafts the work and skills for the selected role and the candidate confirms them once. A resume
            # never goes out with jobs, skills or projects left empty because the documents did not describe them.
            missing = ([] if checkpoints.get('drafts_reviewed') else
                       [index for index, entry in enumerate(facts['experience'])
                        if not entry['bullets'] or thin(entry) or sparse(index, entry)])
            role_skills = [s for s in facts['skills'] if skill_fits_role(s, target)]
            # A resume without a single skill for the role is never built: the skills are asked (with AI
            # suggestions) until at least one the candidate has used is confirmed.
            need_skills = not role_skills or (lacks_role_core(role_skills, target)
                                              and checkpoints.get('skills_reviewed') != label)
            questions = []
            if missing or need_skills:
                progress(28, f'AI drafting {label} content for your review')
                work, skills = draft_role_content(facts, target, label, years, missing, need_skills, model,
                                                  draft_progress, grounded=True)
                # A job without lines is always asked about; a thin job that already has lines only when there are
                # drafts to review (asking it again with nothing new would be an unnecessary question).
                questions = [self._draft_question(facts, index, work.get(index), label,
                                                  [line for line in facts['experience'][index]['bullets']
                                                   if other_field_tools(line, tools, target)])
                             for index in missing if not facts['experience'][index]['bullets'] or work.get(index)]
                if need_skills:
                    # The box holds only the candidate's own skills. AI suggestions arrive unticked beside it and
                    # reach the resume only when ticked: pre-filled suggestions were confirmed with one click.
                    have = {s.casefold() for s in role_skills}
                    questions.append({
                        'id': 'skills', 'section': 'profile', 'entry': None, 'field': 'skills', 'drafted': bool(skills),
                        'label': f'Skills for {label}' + (' (with AI suggestions)' if skills else ''),
                        'explanation': (('None of the skills you confirmed is used for ' + label + '. '
                                         if checkpoints.get('skills_reviewed') == label else '')
                                        + (f'Your skills that fit {label}. Tick a suggestion only if you have used it; '
                                           'add or remove any.' if skills else
                                           'List the tools and technologies you have used, separated by commas.')),
                        'value': ', '.join(role_skills),
                        'suggestions': [s for s in skills if s.casefold() not in have],
                        # Skills for other fields stay in the candidate's facts, off this role's resume.
                        'carry': [s for s in facts['skills'] if s not in role_skills]})
                    checkpoints['skills_review_role'] = label
            if questions:
                proposals = []
                if not own_projects(facts) and not checkpoints.get('projects_reviewed'):
                    proposals = checkpoints.get('proposals_made')
                    if proposals is None:
                        progress(29, 'Project selection: proposing role-specific projects')
                        # Grounded: the candidate's own facts only (suggested skills are a question until confirmed).
                        proposals = checkpoint('proposals_made', propose_projects(
                            facts, target, label, years,
                            model, proposal_progress,
                            avoid=[*(line for entry in facts['experience'] for line in entry['bullets']),
                                   *(line for lines in work.values() for line in lines)], grounded=True))
                checkpoints.update(draft=facts, questions=questions)
                if proposals:
                    checkpoints['proposals'] = proposals
                offer_certifications(facts)
                # Say exactly what was drafted and what is asked: "work lines" only when work lines were drafted.
                has = lambda field, drafted: any(q['field'] == field and bool(q.get('drafted')) == drafted for q in questions)
                titled = all(facts['experience'][q['entry']]['position'] for q in questions
                             if q['field'] == 'bullets' and q.get('drafted'))
                drafted = [text for field, text in (('bullets', 'work lines from your job titles and dates' if titled
                                                     else 'work lines from your employers and dates'),
                                                    ('skills', 'skill suggestions')) if has(field, True)]
                asked = [text for field, text in (('bullets', 'what you did in these jobs (or leave a job without '
                                                              'details)'), ('skills', 'your skills')) if has(field, False)]
                message = ' '.join(filter(None, [
                    f'AI drafted {" and ".join(drafted)} for {label}. Keep only what is true for you and edit anything.'
                    if drafted else '', 'Add ' + ' and '.join(asked) + '.' if asked else '', 'Then confirm once.']))
                if proposals:
                    message += ' Suggested projects are included as drafts: skip any you have not built.'
                with closing(self.connection()) as db, db:
                    db.execute("UPDATE automation SET status='needs_input',stage=?,percent=30,error=?,checkpoints=?,"
                               'updated=? WHERE owner=? AND id=?',
                               (f'Review AI drafts for {intelligence["label"]}', message, json.dumps(checkpoints),
                                updated(), *key))
                return
            progress(30, 'Candidate facts validated')
            progress(40, 'Role keywords and skill areas prepared')
            with closing(self.connection()) as db, db:  # History label for a job submitted before roles were recorded
                db.execute('UPDATE automation SET role=COALESCE(role,?) WHERE owner=? AND id=?', (intelligence['label'], *key))
            # The role resume: only what fits the selected role, from the candidate's facts (nothing added). A
            # candidate's application keeps all of their own facts: they typed them for this role.
            presented, left_out = present_for_role(facts, target, label, origin['bullets'], keep_own=grounded)
            tailored = checkpoints.get('broad_objective_tailored')
            if tailored is None:
                profile, how = tailor_role(presented, label, years, intelligence, model,
                                           ranking_progress, summary_progress)
                tailored = checkpoint('broad_objective_tailored', [profile, how])
            profile, how = tailored
            progress(55, 'Role-based summary, skills and evidence order prepared')
            if not own_projects(facts) and not checkpoints.get('projects_reviewed'):
                proposals = checkpoints.get('proposals_made')
                if proposals is None:
                    progress(56, 'Project selection: proposing role-specific projects')
                    proposals = checkpoint('proposals_made', propose_projects(
                        facts, {'role': intelligence['role'], 'keywords': intelligence['terms']},
                        intelligence['label'], years, model, proposal_progress,
                        avoid=[line for entry in facts['experience'] for line in entry['bullets']], grounded=True))
                if proposals:
                    # Suggestions are never resume content until the candidate confirms them.
                    checkpoints['proposals'] = proposals
                    offer_certifications(facts)
                    with closing(self.connection()) as db, db:
                        db.execute("UPDATE automation SET status='needs_input',stage='Review suggested projects',percent=60,"
                                   'error=?,checkpoints=?,updated=? WHERE owner=? AND id=?',
                                   (f'{len(proposals)} suggested projects are included as drafts. Edit them, skip any '
                                    'you have not built, then save.', json.dumps(checkpoints), updated(), *key))
                    return
            confirmed = checkpoints.get('confirmed_projects') or []
            # Stored facts stay complete (a later resume for another role starts from them); the approval records
            # exactly the facts this role's resume presents, which the builder checks the PDF against.
            final_facts = {**facts, 'projects': [*(facts.get('projects') or []), *confirmed]}
            shown_facts = {**presented, 'projects': [*(presented.get('projects') or []), *confirmed]}
            profile = {**profile, 'projects': [*(profile.get('projects') or []), *confirmed]}
            for holder in (final_facts, shown_facts, profile):
                if not holder['projects']:
                    holder.pop('projects')
            # Technical Skills lists every role technology the confirmed work lines and projects use, so the
            # summary, skills, experience and projects tell one consistent story.
            extra = work_skills(profile, target)
            if extra:
                for holder in (final_facts, shown_facts, profile):
                    have = {s.casefold() for s in holder.get('skills', [])}
                    holder['skills'] = [*holder.get('skills', []), *(s for s in extra if s.casefold() not in have)]
                profile['skill_groups'] = skill_groups(profile['skills'], ROLES.get(intelligence['role'], {}))
            problems = content_problems(profile, label)
            if problems:
                # Never marked verified: the candidate sees exactly what failed.
                raise ValueError('Content validation failed: ' + '; '.join(problems) + '.')
            packet, claims = verified_request(profile, role, years, intelligence)
            progress(65, 'Projects confirmed; facts and claims verified')
            source_kind = request.get('extension', 'attested form')
            if 'documents' in request:
                source_kind = 'documents'
            elif request.get('regenerated_from'):
                source_kind = 'regenerated'
            approval = {'operation': job['id'], 'approved_by': job['owner'], 'claims': claims,
                        'project_names': [split_project(p)[0] for p in packet['profile'].get('projects', [])],
                        'market_sources': intelligence['sources'],
                        'automation': {'v': 5, 'consent': request['consent'], 'submission_sha256': job['id'],
                                       'source_kind': source_kind,
                                       'source_sha256': hashlib.sha256(
                                           (source if isinstance(source, str) else
                                            json.dumps(source, sort_keys=True)).encode()).hexdigest(),
                                       'facts': shown_facts, 'summary': how, 'confirmed_projects': confirmed}}
            packet['target']['automation_owner'] = job['owner']
            packet['target']['automation_id'] = job['id']
            packet['target']['complete_sections'] = True
            match = self.workspace.enqueue(json.dumps(packet).encode(), approval=approval)
            notes = json.loads(job['notice'] or '[]')
            if left_out:
                notes.append(f'Not shown on this {label} resume ({len(left_out)}): ' + '; '.join(left_out[:4])
                             + ('; and more.' if len(left_out) > 4 else '.') + ' Your facts are kept for other roles.')
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='handed_off',stage='Waiting for the resume builder',percent=65,"
                           "payload=NULL,checkpoints='{}',error=NULL,updated=?,job=?,facts=?,notice=? WHERE owner=? AND id=?",
                           (updated(), match['name'], json.dumps(final_facts), json.dumps(notes), *key))
        except NeedsInput as gap:
            # Keep the submission and everything already done; ask only for what the resume lacks.
            checkpoints.update(draft=gap.draft, questions=gap.questions, extraction=EXTRACTION_VERSION)
            if 'documents' in request and any(question.get('field') == 'source_text' for question in gap.questions):
                checkpoints['document_intake'] = True
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
            # The resume itself lacks mandatory facts, or its content failed validation: say exactly what, and stop.
            # Never stay "in progress", and never reach 100%.
            stage = 'Content validation failed' if str(error).startswith('Content validation failed') else 'Needs your input'
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='blocked',stage=?,error=?,payload=NULL,"
                           "checkpoints='{}',updated=? WHERE owner=? AND id=?", (stage, str(error), updated(), *key))
        except Exception:
            logging.getLogger('resumeops.automation').exception('Unexpected preparation failure for %s', job['id'][:12])
            with closing(self.connection()) as db, db:
                db.execute("UPDATE automation SET status='blocked',stage='Needs operator attention',error=?,payload=NULL,"
                           "checkpoints='{}',updated=? WHERE owner=? AND id=?",
                           ('Unexpected preparation failure. Contact the ResumeOps operator.', updated(), *key))

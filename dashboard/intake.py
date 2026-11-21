"""Public multi-candidate intake: one shareable form (/apply) that any number of candidates submit at the same time.

How it fits ResumeOps:
* Every candidate gets a unique ID (RO-XXXXXXXXXX) and a private key. The browser creates the key before it sends
  the form (so a retried submission after a lost response is recognised, and the key never travels back from the
  server); only its SHA-256 is stored. The private link /apply/status#c=<id>&k=<key> is the candidate's access.
* A candidate is its own ResumeOps owner (sha256("candidate\\0" + id)), so the existing per-owner isolation applies to
  every record, file and download, and the existing automation (AI drafting for the selected role, the one-time
  review of drafts, ATS, LaTeX, PDF, Overleaf) runs unchanged.
* A submission is saved in one transaction with its preparation job (never lost, never processed twice). The same
  facts submitted again (another device) are recognised and not processed again.
* Mandatory facts are checked here (personal, education, employment unless a fresher, skills, projects, target
  role); the AI only drafts what the candidate then confirms (resume_bot never adds unconfirmed claims).
"""
import base64
import binascii
import hashlib
import json
import re
import secrets
import threading
import time
from contextlib import closing

from automation_jobs import AutomationCapacity, INTAKE_SOURCE, submission

ID_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'  # no 0/O, 1/I/L: read aloud or typed from a screenshot
KEY_PATTERN = re.compile(r'[A-Za-z0-9_-]{43,128}')  # at least 256 random bits, base64url
ID_PATTERN = re.compile(r'RO-[' + ID_ALPHABET + r']{10}')
FORM_LIMIT = 256 * 1024  # a complete form is a few kilobytes; uploads go to the prefill endpoint
TEXT_LIMITS = {'full_name': 120, 'email': 254, 'phone': 32, 'location': 120, 'linkedin': 300, 'github': 300,
               'portfolio': 300, 'current_company': 160}


class IntakeError(ValueError):
    def __init__(self, message, field=None, status=400):
        super().__init__(message)
        self.field, self.status = field, status


class IntakeDuplicate(IntakeError):
    def __init__(self, candidate):
        super().__init__(f'This application was already received as {candidate}. Open the private link from that '
                         'submission to follow it; it is not processed twice.', None, 409)
        self.candidate = candidate


def candidate_owner(candidate):
    return hashlib.sha256(('candidate\0' + candidate).encode('utf-8')).hexdigest()


def key_digest(key):
    return hashlib.sha256(('resumeops-intake-key\0' + key).encode('utf-8')).hexdigest()


def new_candidate_id():
    return 'RO-' + ''.join(secrets.choice(ID_ALPHABET) for _ in range(10))


def client_tag(address):
    """A network's tag for limits and fair queueing: a hash, so no address is stored."""
    return hashlib.sha256(('resumeops-client\0' + str(address)).encode('utf-8')).hexdigest()[:24]


def prefill_owner(session):
    """The import-queue owner of one browser's prefill uploads (never an account or candidate owner)."""
    return hashlib.sha256(('intake-prefill\0' + session).encode('utf-8')).hexdigest()


def candidate_page(html):
    """The candidate's status page: the dashboard's own resume list and review cards (automation.html and
    automation.js, unchanged), with the account sign-in replaced by the candidate's private key (candidate.js) and
    every file served from /apply/assets. The dashboard token is never placed in a public page."""
    replacements = (
        (b'content="__TOKEN__"', b'content=""'),
        (b'<title>Create a verified resume | ResumeOps</title>', b'<title>Your application | ResumeOps</title>'),
        (b'href="/builder.css"', b'href="/apply/assets/builder.css"'),
        (b'href="/automation.css">', b'href="/apply/assets/automation.css">\n'
                                     b'<link rel="stylesheet" href="/apply/assets/candidate.css">'),
        (b'<script defer src="/auth.js"></script>', b'<script defer src="/apply/assets/candidate.js"></script>'),
        (b'src="/recovery.js"', b'src="/apply/assets/recovery.js"'),
        (b'src="/automation.js"', b'src="/apply/assets/automation.js"'),
        (b'<body class="auto-page">', b'<body class="auto-page auto-candidate">'),
        (b'class="auto-brand" href="/"', b'class="auto-brand" href="/apply"'),
    )
    for old, new in replacements:
        if old not in html:
            raise RuntimeError('The status page template changed: ' + old.decode())
        html = html.replace(old, new)
    return html


def _text(form, key, label, required=True, limit=None):
    value = form.get(key, '')
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise IntakeError(f'Enter {label}.', key)
    value = ' '.join(value.split())
    if required and not value:
        raise IntakeError(f'Enter {label}.', key)
    if len(value) > (limit or TEXT_LIMITS.get(key, 500)):
        raise IntakeError(f'{label[0].upper() + label[1:]} is too long.', key)
    return value


def _lines(value):
    return [line.strip(' \t-•*') for line in (value or '').splitlines() if line.strip(' \t-•*')] \
        if isinstance(value, str) else []


def _rows(form, key):
    rows = form.get(key) or []
    if not isinstance(rows, list) or len(rows) > 20:
        raise IntakeError('Too many entries.', key)
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            raise IntakeError('Invalid entry.', key)
        if any(isinstance(value, str) and value.strip() for value in row.values()):
            kept.append({k: v if isinstance(v, str) else '' for k, v in row.items()})
    return kept


def intake_request(form):
    """(automation request, summary) from the public form; IntakeError (with the field) for anything missing.

    Mandatory: name, email, phone, city/location, target role; at least one education entry (degree, institution,
    year); at least one job (employer, job title, start, end or Present, what you did) unless the candidate is a
    fresher; technical skills; at least one project (name, technologies, what you built)."""
    from resume_bot.automation import CONSENT_VERSION

    if not isinstance(form, dict):
        raise IntakeError('Complete the application form.')
    if form.get('website'):  # a field people never see: only form-filling robots complete it
        raise IntakeError('The application could not be accepted.')
    if form.get('consent') is not True:
        raise IntakeError('Confirm that the details are accurate and agree to the processing.', 'consent')
    role = _text(form, 'role', 'the target role', limit=100)
    candidate = {key: _text(form, key, label, required) for key, label, required in (
        ('full_name', 'your full name', True), ('email', 'your email address', True),
        ('phone', 'your phone number', True), ('location', 'your city', True),
        ('linkedin', 'a LinkedIn URL', False), ('github', 'a GitHub URL', False),
        ('portfolio', 'a portfolio URL', False), ('current_company', 'your current company', False))}
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', candidate['email']):
        raise IntakeError('Enter a valid email address.', 'email')
    if not 7 <= len(re.sub(r'\D', '', candidate['phone'])) <= 15:
        raise IntakeError('Enter a valid phone number (7 to 15 digits).', 'phone')
    skills = [item.strip() for item in re.split(r'[,\n;]', form.get('skills') or '') if item.strip()] \
        if isinstance(form.get('skills'), str) else []
    if not skills:
        raise IntakeError('List your technical skills, separated by commas.', 'skills')
    education = _rows(form, 'education')
    if not education:
        raise IntakeError('Add your education: degree, institution and year.', 'education')
    for index, row in enumerate(education):
        for key, label in (('degree', 'the degree'), ('university', 'the institution'),
                           ('graduation_year', 'the year of completion')):
            if not (row.get(key) or '').strip():
                raise IntakeError(f'Education {index + 1}: enter {label}.', f'education.{index}.{key}')
    fresher = form.get('fresher') is True
    work = _rows(form, 'work_experience')
    if not work and not fresher:
        raise IntakeError('Add your employment (or tick "I am a fresher").', 'work_experience')
    for index, row in enumerate(work):
        for key, label in (('company', 'the employer'), ('position', 'the job title'), ('start', 'the start date'),
                           ('end', 'the end date (or Present)')):
            if not (row.get(key) or '').strip():
                raise IntakeError(f'Job {index + 1}: enter {label}.', f'work_experience.{index}.{key}')
        if not _lines(row.get('responsibilities')):
            raise IntakeError(f'Job {index + 1}: describe what you did, one point per line.',
                              f'work_experience.{index}.responsibilities')
    projects = _rows(form, 'projects')
    if not projects:
        raise IntakeError('Add at least one project you built.', 'projects')
    for index, row in enumerate(projects):
        for key, label in (('name', 'the project name'), ('stack', 'the technologies used')):
            if not (row.get(key) or '').strip():
                raise IntakeError(f'Project {index + 1}: enter {label}.', f'projects.{index}.{key}')
        if not _lines(row.get('description')):
            raise IntakeError(f'Project {index + 1}: describe what you built, one point per line.',
                              f'projects.{index}.description')
    platforms = _rows(form, 'coding_platforms')
    candidate.update(
        role=role, skills=', '.join(skills),
        certifications=form.get('certifications') if isinstance(form.get('certifications'), str) else '',
        achievements=form.get('achievements') if isinstance(form.get('achievements'), str) else '',
        work_experience=[{key: row.get(key, '') for key in ('company', 'position', 'start', 'end', 'location',
                                                              'responsibilities')} for row in work],
        education=[{key: row.get(key, '') for key in ('university', 'degree', 'specialization', 'graduation_year',
                                                       'cgpa')} for row in education],
        projects=[{key: row.get(key, '') for key in ('name', 'stack', 'description')} for row in projects],
        coding_platforms=[{key: row.get(key, '') for key in ('platform', 'url')} for row in platforms])
    years = form.get('years_of_experience')
    if years not in (None, ''):
        try:
            years = float(years)
        except (TypeError, ValueError):
            raise IntakeError('Enter your total experience in years.', 'years_of_experience') from None
        if not 0 <= years <= 60:
            raise IntakeError('Enter your total experience in years (0 to 60).', 'years_of_experience')
        candidate['years_of_experience'] = int(years) if years == int(years) else years
    elif fresher:
        candidate['years_of_experience'] = 0
    try:
        request = submission({'consent': CONSENT_VERSION, 'role': role, 'candidate': candidate})
    except ValueError as error:
        raise IntakeError(str(error)) from error
    summary = {'name': candidate['full_name'], 'email': candidate['email'], 'phone': candidate['phone'],
               'role': request['role']}
    return request, summary


class RateLimiter:
    """Token buckets per (purpose, client): (capacity, seconds to refill it). A campus drive shares one address, so
    a few hundred applications an hour still fit; a flood from one address stops here. Uploads cost GPU time (the AI
    reading), so they have their own, smaller budget."""
    LIMITS = {'submit': (300, 3600), 'prefill': (200, 3600), 'verify': (600, 600), 'read': (12000, 600),
              'action': (2000, 600)}

    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = {}

    def allow(self, purpose, client):
        capacity, window = self.LIMITS[purpose]
        now = time.monotonic()
        with self.lock:
            if len(self.buckets) > 20000:
                self.buckets = {key: value for key, value in self.buckets.items() if now - value[1] < window}
            tokens, last = self.buckets.get((purpose, client), (capacity, now))
            tokens = min(capacity, tokens + (now - last) * capacity / window)
            if tokens < 1:
                self.buckets[(purpose, client)] = (tokens, now)
                return False
            self.buckets[(purpose, client)] = (tokens - 1, now)
            return True


class IntakeDesk:
    def __init__(self, workspace, automation):
        self.workspace = workspace
        self.automation = automation
        self.limiter = RateLimiter()
        self.human = HumanCheck(workspace.root)
        # Requests that parse uploads or write the database run at most 16 at a time; the rest are told to retry in
        # a moment (the page retries by itself), so a burst never exhausts memory or threads.
        self.heavy = threading.BoundedSemaphore(16)
        with closing(automation.connection()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS intake (
                candidate TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL UNIQUE,
                created REAL NOT NULL, updated REAL NOT NULL, name TEXT, email TEXT, phone TEXT, role TEXT,
                form TEXT, digest TEXT, scrubbed REAL)''')
            db.execute('CREATE INDEX IF NOT EXISTS intake_digest ON intake(digest)')

    # Candidates ----------------------------------------------------------------------------------------------

    @staticmethod
    def digest(request):
        raw = json.dumps({key: value for key, value in request.items() if key != 'consent'},
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def submit(self, form, key, candidate=None, client=None):
        """Save an application (or, with ``candidate``, a correction from that candidate). Returns
        {'candidate', 'job', 'created'}. The same key again returns the same candidate (a retried request).
        ``client`` is the sender's network tag (client_tag) for the per-network limit and fair queueing."""
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise IntakeError('The page could not create a private key. Reload the page and submit again.')
        request, summary = intake_request(form)
        digest, now = self.digest(request), time.time()
        stored = json.dumps(form, ensure_ascii=False)
        with closing(self.automation.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            mine = db.execute('SELECT candidate, owner, scrubbed FROM intake WHERE key_hash=?',
                              (key_digest(key),)).fetchone()
            if candidate is not None:
                if not mine or mine['candidate'] != candidate or mine['scrubbed']:
                    raise IntakeError('This private link is not valid.', None, 403)
                job = self.automation.insert(db, mine['owner'], request, INTAKE_SOURCE, summary['name'], client)
                db.execute('UPDATE intake SET updated=?, name=?, email=?, phone=?, role=?, form=?, digest=? '
                           'WHERE candidate=?', (now, summary['name'], summary['email'], summary['phone'],
                                                  summary['role'], stored, digest, candidate))
                result = {'candidate': candidate, 'job': job, 'created': False}
            elif mine:
                if mine['scrubbed']:
                    raise IntakeError('This application was deleted.', None, 410)
                job = self.automation.request_id(request)[0]
                result = {'candidate': mine['candidate'], 'job': job, 'created': False}
            else:
                same = db.execute('SELECT candidate FROM intake WHERE digest=? AND scrubbed IS NULL',
                                  (digest,)).fetchone()
                if same:
                    raise IntakeDuplicate(same['candidate'])
                for _ in range(8):
                    candidate_id = new_candidate_id()
                    if not db.execute('SELECT 1 FROM intake WHERE candidate=?', (candidate_id,)).fetchone():
                        break
                owner = candidate_owner(candidate_id)
                db.execute('INSERT INTO intake(candidate, owner, key_hash, created, updated, name, email, phone, role, '
                           'form, digest) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (candidate_id, owner, key_digest(key), now, now, summary['name'], summary['email'],
                            summary['phone'], summary['role'], stored, digest))
                job = self.automation.insert(db, owner, request, INTAKE_SOURCE, summary['name'], client)
                result = {'candidate': candidate_id, 'job': job, 'created': True}
        self.automation.start()
        self.automation.wake.set()
        return result

    def known_key(self, key):
        """Whether an application with this private key is already saved (a retried submission creates nothing)."""
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            return False
        with closing(self.automation.connection()) as db:
            return bool(db.execute('SELECT 1 FROM intake WHERE key_hash=?', (key_digest(key),)).fetchone())

    def authorize(self, candidate, key):
        """The candidate's owner key when the private key matches (None otherwise)."""
        if not isinstance(candidate, str) or not ID_PATTERN.fullmatch(candidate) \
                or not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            return None
        with closing(self.automation.connection()) as db:
            row = db.execute('SELECT owner, key_hash, scrubbed FROM intake WHERE candidate=?', (candidate,)).fetchone()
        if not row or row['scrubbed'] or not secrets.compare_digest(row['key_hash'], key_digest(key)):
            return None
        return row['owner']

    def is_candidate_owner(self, owner):
        with closing(self.automation.connection()) as db:
            return bool(db.execute('SELECT 1 FROM intake WHERE owner=?', (owner,)).fetchone())

    def profile(self, candidate):
        with closing(self.automation.connection()) as db:
            row = db.execute('SELECT candidate, name, role, created, form FROM intake WHERE candidate=?',
                             (candidate,)).fetchone()
        if not row:
            return None
        return {'candidate': row['candidate'], 'name': row['name'], 'role': row['role'], 'created': row['created'],
                'form': json.loads(row['form'] or '{}')}

    def scrub_if_empty(self, owner):
        """After a candidate deletes their resumes: with none left, their contact details and form are removed too
        (the ID stays, marked deleted, so the operator's list stays truthful)."""
        with closing(self.automation.connection()) as db, db:
            if db.execute('SELECT 1 FROM automation WHERE owner=?', (owner,)).fetchone():
                return False
            db.execute("UPDATE intake SET name=NULL, email=NULL, phone=NULL, form=NULL, digest=NULL, scrubbed=?, "
                       'updated=? WHERE owner=? AND scrubbed IS NULL', (time.time(), time.time(), owner))
            return True

    # Operator ------------------------------------------------------------------------------------------------

    def new_key(self, candidate):
        """A new private key for a candidate who lost their link (the old link stops working)."""
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii').rstrip('=')
        with closing(self.automation.connection()) as db, db:
            changed = db.execute('UPDATE intake SET key_hash=?, updated=? WHERE candidate=? AND scrubbed IS NULL',
                                 (key_digest(key), time.time(), candidate)).rowcount
        if not changed:
            raise IntakeError('Candidate not found.', None, 404)
        return key

    def candidates(self, limit=1000):
        """Every candidate with the state of their latest resume, newest first, and queue figures."""
        with closing(self.automation.connection()) as db:
            people = db.execute('SELECT candidate, owner, created, updated, name, email, phone, role, scrubbed '
                                'FROM intake ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
            rows = db.execute("SELECT owner, id, status, stage, percent, attempts, next_retry, error, created, updated, "
                              'job, notice, role, candidate, NULL AS waiting, facts IS NOT NULL AS regenerable '
                              'FROM automation WHERE source=? ORDER BY created', (INTAKE_SOURCE,)).fetchall()
            ahead = self.automation.queue_positions(db)
        latest, versions = {}, {}
        for row in rows:
            latest[row['owner']] = row
            versions[row['owner']] = versions.get(row['owner'], 0) + 1
        state = self.workspace.automation_state()
        out = []
        for person in people:
            entry = {key: person[key] for key in ('candidate', 'created', 'updated', 'name', 'email', 'phone', 'role')}
            entry['deleted'] = bool(person['scrubbed'])
            row = latest.get(person['owner'])
            if row is not None:
                job = self.automation._status(row, state, person['owner'])
                entry.update(job={key: job.get(key) for key in (
                    'id', 'status', 'stage', 'percent', 'error', 'revision', 'overleaf', 'next_retry', 'updated')},
                    ats=(job.get('ats') or {}).get('ats_readiness'), versions=versions[person['owner']],
                    role=job.get('role') or entry['role'])
                if (person['owner'], row['id']) in ahead and row['status'] in ('queued', 'retrying'):
                    entry['job']['queue_ahead'] = ahead[(person['owner'], row['id'])]
            out.append(entry)
        return out


SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
TURNSTILE_KEY = re.compile(r'[0-9A-Za-z_-]{10,120}')
# Cloudflare's published test secrets (they accept only the dummy token and report hostname example.com).
TEST_SECRETS = {'1x0000000000000000000000000000000AA', '2x0000000000000000000000000000000AA',
                '3x0000000000000000000000000000000AA'}


class HumanCheckError(IntakeError):
    def __init__(self, message):
        super().__init__(message, None, 403)


class HumanCheck:
    """Cloudflare Turnstile for the public form, off until the operator saves a site key and secret.

    The browser passes the Turnstile check once and receives a signed pass (valid PASS_SECONDS) that covers its
    uploads and applications, so retries of a pending application never need a new challenge. A pass is good for
    PASS_USES of each kind, which stops one solved check from feeding a flood. Requests from this machine (the
    operator, release checks) are not checked. The secret never leaves the server: settings() is never sent."""
    PASS_SECONDS = 3600
    PASS_USES = {'submit': 10, 'prefill': 20}

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.uses = {}

    # Keys -------------------------------------------------------------------------------------------------------

    def _settings_path(self):
        return self.root / '.resumeops-turnstile.json'

    def settings(self):
        """{'site_key', 'secret'} when configured (environment first, then the operator's saved keys), else None."""
        import os
        site_key, secret = os.environ.get('RESUMEOPS_TURNSTILE_SITE_KEY'), os.environ.get('RESUMEOPS_TURNSTILE_SECRET')
        if not (site_key and secret):
            try:
                saved = json.loads(self._settings_path().read_text(encoding='utf-8'))
                site_key, secret = saved.get('site_key'), saved.get('secret')
            except (OSError, ValueError, AttributeError):
                return None
        if isinstance(site_key, str) and isinstance(secret, str) and TURNSTILE_KEY.fullmatch(site_key) \
                and TURNSTILE_KEY.fullmatch(secret):
            return {'site_key': site_key, 'secret': secret}
        return None

    def site_key(self):
        settings = self.settings()
        return settings['site_key'] if settings else None

    def configure(self, site_key, secret):
        """Save the keys (both empty: turn the check off). Returns the public status."""
        if site_key == '' and secret == '':
            self._settings_path().unlink(missing_ok=True)
            return self.status()
        if not (isinstance(site_key, str) and TURNSTILE_KEY.fullmatch(site_key.strip())
                and isinstance(secret, str) and TURNSTILE_KEY.fullmatch(secret.strip())):
            raise IntakeError('Paste the Turnstile site key and secret key exactly as Cloudflare shows them.', None, 400)
        path = self._settings_path()
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'site_key': site_key.strip(), 'secret': secret.strip(),
                                         'saved': time.time()}), encoding='utf-8')
        temporary.replace(path)
        return self.status()

    def status(self):
        site_key = self.site_key()
        return {'configured': bool(site_key), 'site_key': site_key,
                'test_keys': bool(site_key) and self.settings()['secret'] in TEST_SECRETS}

    # Verification -----------------------------------------------------------------------------------------------

    @staticmethod
    def siteverify(secret, token, remote_ip):
        """Cloudflare's verdict on a Turnstile token (JSON with success, hostname, error-codes)."""
        import urllib.parse
        import urllib.request
        import uuid
        data = urllib.parse.urlencode({'secret': secret, 'response': token, 'remoteip': remote_ip or '',
                                       'idempotency_key': str(uuid.uuid4())}).encode()
        request = urllib.request.Request(SITEVERIFY, data=data, method='POST',
                                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def verify(self, token, remote_ip, host):
        """Exchange a Turnstile token for a pass. Raises HumanCheckError when the check failed."""
        settings = self.settings()
        if not settings:
            raise HumanCheckError('The human check is not turned on.')
        if not isinstance(token, str) or not 0 < len(token) <= 2048:
            raise HumanCheckError('Complete the check above the Submit button, then try again.')
        try:
            result = self.siteverify(settings['secret'], token, remote_ip)
        except (OSError, ValueError) as error:
            raise IntakeError('The human check could not be confirmed right now. Trying again shortly.', None,
                              503) from error
        test = settings['secret'] in TEST_SECRETS
        if not result.get('success') or (not test and result.get('hostname') != host):
            raise HumanCheckError('The human check did not pass. Complete it again, then submit.')
        return self.issue()

    # Passes -----------------------------------------------------------------------------------------------------

    def _pass_key(self):
        path = self.root / '.resumeops-apply-pass.key'
        with self.lock:
            try:
                key = path.read_bytes()
            except OSError:
                key = b''
            if len(key) < 32:
                key = secrets.token_bytes(32)
                path.write_bytes(key)
            return key

    def issue(self):
        import hmac
        body = base64.urlsafe_b64encode(json.dumps({'n': secrets.token_hex(12), 'e': int(time.time())
                                                    + self.PASS_SECONDS}).encode()).decode().rstrip('=')
        signature = hmac.new(self._pass_key(), body.encode(), 'sha256').hexdigest()
        return {'pass': f'{body}.{signature}', 'expires': int(time.time()) + self.PASS_SECONDS}

    def check(self, value, purpose):
        """The pass's id when ``value`` is a valid, unexpired pass with uses left for ``purpose`` ('submit' or
        'prefill'); raise HumanCheckError otherwise. A use is counted only by record(), once something was saved, so
        retries of the same pending application never use the pass up."""
        import hmac
        message = 'Complete the check above the Submit button, then submit again. Your form is kept.'
        if not isinstance(value, str) or value.count('.') != 1 or len(value) > 400:
            raise HumanCheckError(message)
        body, signature = value.split('.')
        expected = hmac.new(self._pass_key(), body.encode(), 'sha256').hexdigest()
        if not secrets.compare_digest(signature, expected):
            raise HumanCheckError(message)
        try:
            claims = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            nonce, expires = str(claims['n']), int(claims['e'])
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise HumanCheckError(message) from None
        now = time.time()
        if expires < now:
            raise HumanCheckError('The human check expired. Complete it again, then submit. Your form is kept.')
        with self.lock:
            if len(self.uses) > 50000:
                self.uses = {k: v for k, v in self.uses.items() if v[1] >= now}
            count, _ = self.uses.get((nonce, purpose), (0, expires))
            if count >= self.PASS_USES[purpose]:
                raise HumanCheckError('This browser has sent many applications. Complete the check again to send '
                                      'more.')
            self.uses.setdefault((nonce, purpose), (0, expires))
        return nonce

    def record(self, nonce, purpose):
        with self.lock:
            count, expires = self.uses.get((nonce, purpose), (0, time.time() + self.PASS_SECONDS))
            self.uses[(nonce, purpose)] = (count + 1, expires)

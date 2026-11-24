"""Validated role-targeted requests and evidence-only ATS analysis."""
import json
import os
import re
from pathlib import Path

from .profile import ProfileError, validate_profile

TEMPLATES = {'standard': 'Standard', 'compact': 'Compact'}

# Role catalog: the bundled roles.json plus an operator-managed data file (role-catalog.json in the
# data root, or RESUMEOPS_ROLE_CATALOG). Same id replaces, a new id adds, "hidden": true retires a role
# from the picker while saved jobs that use it still render. Edited without changing code.
CATALOG_ENV = 'RESUMEOPS_ROLE_CATALOG'
BUNDLED_CATALOG = Path(__file__).with_name('roles.json')
ROLES = {}
ALIASES = {}
CATALOG_STATUS = {'source': None, 'error': None}
_catalog_stamp = None


def _text(value, field, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f'{field}: expected text up to {limit} characters')
    return ' '.join(value.split())


def _texts(value, field, limit=60, maximum=40):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{field}: expected a list of up to {maximum} entries')
    return list(dict.fromkeys(_text(item, field, limit) for item in value))


def validate_catalog(data):
    """Return (roles, aliases) from a catalog document, or raise ValueError naming the problem."""
    if not isinstance(data, dict) or not isinstance(data.get('roles'), list) or len(data['roles']) > 200:
        raise ValueError('Catalog must be an object with a "roles" list (up to 200 roles)')
    roles = {}
    for index, role in enumerate(data['roles']):
        where = f'roles[{index}]'
        if not isinstance(role, dict) or set(role) - {'id', 'name', 'family', 'titleVariations', 'groups', 'keywords',
                                                      'projectThemes', 'hidden', 'resumeGroups'}:
            raise ValueError(f'{where}: unknown fields')
        identifier = role.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,59}', identifier) or identifier == 'custom':
            raise ValueError(f'{where}.id: use 2-60 lowercase letters, digits or hyphens')
        groups = role.get('groups', {})
        if not isinstance(groups, dict) or len(groups) > 12:
            raise ValueError(f'{where}.groups: expected up to 12 named skill groups')
        clean_groups = {_text(name, f'{where}.groups', 40): _texts(items, f'{where}.groups.{name}') for name, items in groups.items()}
        # resumeGroups: the role's Technical Skills categories as recruiters and job descriptions name them
        # ("Frontend Technologies", "State Management", "Build Tools & Package Managers"). They group a candidate's
        # confirmed skills on the resume and are the catalog behind the skills review; never resume content alone.
        resume_groups = role.get('resumeGroups', {})
        if not isinstance(resume_groups, dict) or len(resume_groups) > 12:
            raise ValueError(f'{where}.resumeGroups: expected up to 12 named skill categories')
        clean_resume_groups = {_text(name, f'{where}.resumeGroups', 40): _texts(items, f'{where}.resumeGroups.{name}')
                               for name, items in resume_groups.items()}
        keywords = list(dict.fromkeys([*(term for items in clean_groups.values() for term in items),
                                       *_texts(role.get('keywords', []), f'{where}.keywords', maximum=80)]))
        if not keywords:
            raise ValueError(f'{where}: add at least one skill group or keyword')
        roles[identifier] = {
            'id': identifier, 'name': _text(role.get('name'), f'{where}.name'),
            'family': _text(role.get('family', identifier), f'{where}.family', 60),
            'titleVariations': _texts(role.get('titleVariations', []), f'{where}.titleVariations', 100, 20),
            'groups': clean_groups, 'resumeGroups': clean_resume_groups, 'keywords': keywords,
            'projectThemes': _texts(role.get('projectThemes', []), f'{where}.projectThemes', 160, 10),
            'hidden': role.get('hidden') is True,
        }
    aliases = data.get('aliases', {})
    if not isinstance(aliases, dict) or len(aliases) > 500:
        raise ValueError('aliases: expected an object of term -> list of alternative spellings')
    return roles, {_text(term, 'aliases', 60): _texts(names, f'aliases.{term}') for term, names in aliases.items()}


def catalog_path():
    return Path(os.environ.get(CATALOG_ENV) or Path.cwd() / 'role-catalog.json')


def load_roles(force=False):
    """(Re)load the merged catalog when the bundled or operator file changed. Returns ROLES."""
    global _catalog_stamp
    override = catalog_path()
    stamp = (BUNDLED_CATALOG.stat().st_mtime_ns, str(override),
             override.stat().st_mtime_ns if override.is_file() else None)
    if stamp == _catalog_stamp and not force:
        return ROLES
    roles, aliases = validate_catalog(json.loads(BUNDLED_CATALOG.read_text(encoding='utf-8')))
    status = {'source': 'bundled', 'error': None}
    if override.is_file():
        try:
            extra_roles, extra_aliases = validate_catalog(json.loads(override.read_text(encoding='utf-8')))
            roles.update(extra_roles)
            aliases.update(extra_aliases)
            status['source'] = 'bundled+' + override.name
        except (OSError, ValueError) as exc:  # a bad edit must not take the site down
            status['error'] = f'{override.name} ignored: {exc}'
    ROLES.clear()
    ROLES.update(roles)
    ALIASES.clear()
    ALIASES.update(aliases)
    CATALOG_STATUS.update(status)
    _catalog_stamp = stamp
    return ROLES


def role_by_title(title):
    """The catalog role whose name or title variation matches ``title`` (case-insensitive), or None."""
    wanted = ' '.join(str(title).split()).casefold()
    load_roles()
    for role in ROLES.values():
        if wanted in {role['name'].casefold(), *(t.casefold() for t in role['titleVariations'])}:
            return role
    return None


load_roles()


class DatabaseUnavailable(RuntimeError):
    pass


def validate_keywords(keywords):
    if not isinstance(keywords, list) or len(keywords) > 50 or any(
        not isinstance(term, str) or not 1 <= len(term.strip()) <= 100 for term in keywords
    ):
        raise ProfileError('Enter up to 50 keywords of 1–100 characters each')
    return list(dict.fromkeys(term.strip() for term in keywords))


def normalize_request(payload):
    if not isinstance(payload, dict) or 'profile' not in payload:
        return validate_profile(payload), None
    if set(payload) != {'profile', 'target'} or not isinstance(payload['target'], dict):
        raise ProfileError('Expected a candidate profile and target')
    target = payload['target']
    if set(target) - {'role', 'keywords', 'template', 'experience_years', 'approved', 'role_title', 'automation_owner', 'automation_id',
                      'complete_sections'}:
        raise ProfileError('Unknown target option')
    if 'approved' in target and target['approved'] is not True:
        raise ProfileError('Approval marker must be true when present')
    role = target.get('role', '')
    template = target.get('template', 'standard')
    keywords = target.get('keywords', [])
    title = target.get('role_title')
    if role == 'custom':
        if not isinstance(title, str) or not valid_role_title(title):
            raise ProfileError('Enter a valid target job title')
    elif not isinstance(role, str) or role not in ROLES or title is not None:
        raise ProfileError('Select a target role')
    if not isinstance(template, str) or template not in TEMPLATES:
        raise ProfileError('Select a supported resume template')
    keywords = validate_keywords(keywords)
    years = target.get('experience_years')
    if years is not None and (type(years) is not int or not 0 <= years <= 25):
        raise ProfileError('Experience level must be from 0 to 25+ years')
    normalized = {
        'role': role, 'template': template, 'keywords': keywords
    }
    if role == 'custom':
        normalized['role_title'] = ' '.join(title.split())
    if years is not None:
        normalized['experience_years'] = years
    if target.get('approved') is True:
        # Set only by the dashboard after the candidate approved every line; render verbatim.
        normalized['approved'] = True
    private = ('automation_owner', 'automation_id')
    if any(key in target for key in private):
        if not normalized.get('approved') or any(
                not isinstance(target.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', target[key])
                for key in private):
            raise ProfileError('Invalid private automation request')
        normalized.update({key: target[key] for key in private})
    if 'complete_sections' in target and target['complete_sections'] is not True:
        raise ProfileError('Complete sections cannot be disabled for a new resume')
    # Every newly queued target-role resume uses the six-section layout, including manual and AI-reviewed jobs.
    normalized['complete_sections'] = True
    return validate_profile(payload['profile']), normalized


MONTHS = {m: i for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep',
                                       'oct', 'nov', 'dec'))}


def _month_index(value, end=False, today=None):
    """Month number (year*12+month) of a resume date, or None if it is not clearly a date."""
    import datetime
    text = (value or '').strip().lower()
    if not text:
        return None
    if re.search(r'present|current|now|till date|to date|ongoing', text):
        today = today or datetime.date.today()
        return today.year * 12 + today.month - 1
    match = re.fullmatch(r'(\d{1,2})\s*[/.-]\s*(\d{4})', text)
    if match and 1 <= int(match.group(1)) <= 12:
        return int(match.group(2)) * 12 + int(match.group(1)) - 1
    match = re.match(r'(\d{4})\s*[/.-]\s*(\d{1,2})\b', text)
    if match and 1 <= int(match.group(2)) <= 12:
        return int(match.group(1)) * 12 + int(match.group(2)) - 1
    match = re.search(r'\b([a-z]{3})[a-z]*\.?,?\s+\'?(\d{4})\b', text)
    if match and match.group(1) in MONTHS:
        return int(match.group(2)) * 12 + MONTHS[match.group(1)]
    match = re.fullmatch(r'(19|20)\d{2}', text)
    if match:
        return int(text) * 12 + (11 if end else 0)
    return None


def dated_months(profile, today=None):
    """Months covered by dated experience entries (overlaps merged), or None if none are dated.
    An entry without an end date counts as ongoing only if its end is written as Present etc."""
    spans = []
    for entry in profile.get('experience', []):
        start = _month_index(entry.get('start'), today=today)
        end = _month_index(entry.get('end'), end=True, today=today) if entry.get('end') else None
        if start is None or end is None or end < start:
            continue
        spans.append((start, end + 1))
    if not spans:
        return None
    spans.sort()
    total, (low, high) = 0, spans[0]
    for start, end in spans[1:]:
        if start <= high:
            high = max(high, end)
        else:
            total, low, high = total + high - low, start, end
    return total + high - low


def experience_evidence(profile, target, today=None):
    """Compare the claimed experience level with the dated roles. ``used`` is the number the
    generator may state: the claim when the dated roles support it, otherwise the dated years;
    None when nothing is dated (the claim cannot be verified, so it is never stated)."""
    claimed = (target or {}).get('experience_years')
    months = dated_months(profile, today)
    dated = months // 12 if months is not None else None
    if dated is None:
        used = None
    elif claimed is None:
        used = dated
    else:
        used = min(claimed, dated) if claimed > dated + 1 else claimed if claimed <= dated else dated
    supported = not claimed or (dated is not None and claimed <= dated + 1)
    return {'claimed': claimed, 'dated_months': months, 'dated_years': dated, 'used': used,
            'supported': supported}


def valid_role_title(value):
    return len(value.strip()) <= 100 and any(char.isalpha() for char in value) and not any(
        ord(char) < 32 or ord(char) == 127 for char in value)


def role_name(target):
    return ROLES[target['role']]['name'] if target['role'] in ROLES else target['role_title']


def role_keywords(target):
    return list({term.casefold(): term for term in
                 [*ROLES.get(target['role'], {}).get('keywords', []), *target.get('keywords', [])]}.values())


def term_pattern(term):
    """A term, its catalog aliases, an optional plural "s", and an optional version ("Java 17")."""
    spellings = sorted({' '.join(t.split()) for t in [term, *ALIASES.get(term, [])]}, key=len, reverse=True)
    body = '|'.join(re.escape(spelling) for spelling in spellings)
    return re.compile(r'(?<![\w.#+])(?:' + body + r')s?(?:\s?v?\d+(?:\.\d+)*\+?)?(?![\w#+])', re.IGNORECASE)


def matched_terms(terms, text):
    normalized = ' '.join(text.split())
    return [term for term in terms if term_pattern(term).search(normalized)]


def analyze(profile, target, keywords=None, today=None):
    """Explainable 100-point ATS readiness (resume_bot.ats) for a catalog or custom role."""
    from . import ats
    load_roles()
    keywords = role_keywords(target) if keywords is None else keywords
    # An automation headline names the requested role. It is an objective, not a skill claim; the
    # summary of an automated resume restates verified facts, so it is not separate evidence either.
    automated = bool(target.get('automation_owner'))
    evidence = [*([] if automated else [profile['headline'], profile['summary']]), *profile['skills'],
                *profile['certifications']]
    evidence.extend(profile.get('competencies', []))
    evidence.extend(profile.get('projects', []))
    evidence.extend(profile.get('achievements', []))
    for entry in profile['experience']:
        evidence.extend([entry['position'], *entry['bullets']])
    for entry in profile['education']:
        evidence.extend([entry['degree'], entry['details']])
    experience = experience_evidence(profile, target, today)
    report = ats.score(profile, {**target, '_groups': ROLES.get(target['role'], {}).get('groups')}, keywords,
                       role_title=role_name(target), matched_terms=matched_terms, experience=experience,
                       evidence_text=' '.join(evidence))
    return {'role': target['role'], **({'role_title': target['role_title']} if 'role_title' in target else {}),
            'template': target['template'], **report, 'experience': experience}


def _legacy_analyze(profile, target, keywords=None, today=None):
    """The v2 50/30/20 rubric, kept for reference by older reports; not used for new scores."""
    keywords = role_keywords(target) if keywords is None else keywords
    headline_evidence = [] if target.get('automation_owner') else [profile['headline']]
    evidence = [*headline_evidence, profile['summary'], *profile['skills'], *profile['certifications']]
    evidence.extend(profile.get('competencies', []))
    evidence.extend(profile.get('projects', []))
    evidence.extend(profile.get('achievements', []))
    for entry in profile['experience']:
        evidence.extend([entry['position'], *entry['bullets']])
    for entry in profile['education']:
        evidence.extend([entry['degree'], entry['details']])
    matched = matched_terms(keywords, ' '.join(evidence))
    missing = [term for term in keywords if term not in matched]
    coverage = round(100 * len(matched) / len(keywords)) if keywords else 0
    format_checks = (
        (10, bool(profile['full_name'] and profile['email']), 'Include an identifiable name and email.'),
        (8, bool(profile['headline']), 'Add a verified headline.'),
        (6, bool(profile['summary']), 'Add a concise summary of confirmed experience.'),
        (6, bool(profile['skills']), 'List relevant skills the candidate can substantiate.'),
    )
    experience = experience_evidence(profile, target, today)
    level = ('Dated roles support the selected experience level.' if experience['supported'] else
             f"Selected {experience['claimed']} years, but dated roles show "
             f"{experience['dated_years'] if experience['dated_years'] is not None else 'no'} years; "
             'add the missing dated roles or lower the experience level.')
    evidence_checks = (
        # Freshers have no employment: described projects ("Name - details") are their contributions.
        (6, (any(isinstance(item, str) and ' - ' in item for item in profile.get('projects') or [])
             if target.get('experience_years') == 0 else
             any(entry['bullets'] for entry in profile['experience'])),
         ('Describe verified project contributions.' if target.get('experience_years') == 0 else
          'Describe verified work contributions.')),
        (4, (any(entry.get('end') for entry in profile['education'])
             if target.get('experience_years') == 0 else
             any(entry['start'] for entry in profile['experience'])),
         ('Provide a confirmed graduation year.' if target.get('experience_years') == 0 else
          'Provide confirmed dates for employment.')),
        (5, bool(profile['education'] or profile.get('projects') or profile['certifications']),
         'Include verified education, projects, or certifications when available.'),
        (5, experience['supported'], level),
    )
    breakdown = [
        {'label': 'Role keyword alignment', 'points': round(coverage / 2), 'max': 50},
        {'label': 'Section structure', 'points': sum(points for points, passed, _ in format_checks if passed), 'max': 30},
        {'label': 'Evidence completeness', 'points': sum(points for points, passed, _ in evidence_checks if passed), 'max': 20},
    ]
    suggestions = [tip for _, passed, tip in (*format_checks, *evidence_checks) if not passed]
    if missing:
        suggestions.append('Review missing terms and add only those supported by the candidate’s experience.')
    return {
        'role': target['role'],
        **({'role_title': target['role_title']} if 'role_title' in target else {}),
        'template': target['template'],
        'score': coverage,
        'ats_readiness': sum(part['points'] for part in breakdown),
        'breakdown': breakdown, 'suggestions': suggestions,
        'matched': matched, 'missing': missing, 'experience': experience,
        'method': 'ResumeOps rubric: 50 keyword, 30 structure, 20 evidence points. Employer ATS rankings can differ.',
    }


OUTBOX = '.resumeops-mongo-outbox.json'


def _records(report):
    """The documents a finished resume contributes to MongoDB (role catalogue + ATS summary)."""
    ats = report['ats']
    role = ats['role']
    analysis = {'_id': report['source_sha256'], 'role': role, 'template': ats['template'],
                'score': ats['score']}
    if 'ats_readiness' in ats:
        analysis['ats_readiness'] = ats['ats_readiness']
    if role not in ROLES:
        analysis['role_title'] = ats.get('role_title', '')
        return [('resume_analyses', analysis)]
    return [('role_keywords', {'_id': role, 'name': ROLES[role]['name'], 'keywords': ROLES[role]['keywords']}),
            ('resume_analyses', analysis)]


def _write_records(uri, records):
    """Idempotent upserts by _id, each read back to prove it was stored. Existing documents with
    other ids are never touched."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        raise DatabaseUnavailable('Install the MongoDB support dependency') from None
    try:
        with MongoClient(uri, timeoutMS=3500) as client:
            db = client[os.environ.get('RESUMEOPS_MONGODB_DATABASE', 'resumeops')]
            for collection, document in records:
                db[collection].replace_one({'_id': document['_id']}, document, upsert=True)
                if db[collection].find_one({'_id': document['_id']}) != document:
                    raise DatabaseUnavailable('MongoDB did not return the saved document')
    except PyMongoError:
        raise DatabaseUnavailable('Configured MongoDB is unavailable; the save is queued and retried') from None


def _ping(uri):
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        raise DatabaseUnavailable('Install the MongoDB support dependency') from None
    try:
        with MongoClient(uri, timeoutMS=3500) as client:
            client.admin.command('ping')
    except PyMongoError:
        raise DatabaseUnavailable('Configured MongoDB is unavailable') from None


def _outbox(folder):
    path = Path(folder) / OUTBOX
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return path, data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return path, {}


def persist_analysis(report, outbox=None):
    """Save the ATS summary to MongoDB when RESUMEOPS_MONGODB_URI is set. Output files remain the
    system of record: if MongoDB is unreachable and ``outbox`` is given, the documents are queued
    there (keyed by _id, so retries are idempotent) and the resume still completes."""
    uri = os.environ.get('RESUMEOPS_MONGODB_URI')
    status = {'configured': bool(uri), 'saved': False, 'queued': False}
    if not uri or not report.get('ats'):
        return status
    records = _records(report)
    try:
        _write_records(uri, records)
        status['saved'] = True
    except DatabaseUnavailable as exc:
        if outbox is None:
            raise
        from .storage import write_json
        path, pending = _outbox(outbox)
        for collection, document in records:
            pending[collection + ':' + str(document['_id'])] = [collection, document]
        write_json(path, pending)
        status.update(queued=True, error=str(exc))
    return status


def flush_outbox(folder):
    """Retry queued MongoDB saves. Returns a health summary; never raises."""
    import time
    uri = os.environ.get('RESUMEOPS_MONGODB_URI')
    path, pending = _outbox(folder)
    status = {'configured': bool(uri), 'pending': len(pending), 'checked': time.time()}
    if not uri:
        return status
    try:
        if pending:
            _write_records(uri, [tuple(item) for item in pending.values()])
        else:
            _ping(uri)
            status['ok'] = True
            return status
    except DatabaseUnavailable as exc:
        status.update(ok=False, error=str(exc))
        return status
    from .storage import write_json
    write_json(path, {})
    status.update(ok=True, pending=0, flushed=len(pending))
    return status

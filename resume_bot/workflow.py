"""Validated role-targeted requests and evidence-only ATS analysis."""
import json
import os
import re
from pathlib import Path

from .profile import ProfileError, validate_profile

ROLES = {role['id']: role for role in json.loads(
    Path(__file__).with_name('roles.json').read_text(encoding='utf-8'))['roles']}
TEMPLATES = {'standard': 'Standard', 'compact': 'Compact'}


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
    if set(target) - {'role', 'keywords', 'template', 'experience_years', 'approved', 'role_title', 'automation_owner', 'automation_id'}:
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


def matched_terms(terms, text):
    normalized = ' '.join(text.split())
    return [term for term in terms if re.search(
        r'(?<!\w)' + re.escape(' '.join(term.split())) + r'(?!\w)', normalized, re.IGNORECASE)]


def analyze(profile, target, keywords=None, today=None):
    keywords = role_keywords(target) if keywords is None else keywords
    # An automation headline names the requested role. It is an objective, not a skill claim.
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

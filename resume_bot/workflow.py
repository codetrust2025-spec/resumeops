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
    if set(target) - {'role', 'keywords', 'template', 'experience_years'}:
        raise ProfileError('Unknown target option')
    role = target.get('role', '')
    template = target.get('template', 'standard')
    keywords = target.get('keywords', [])
    if not isinstance(role, str) or role not in ROLES:
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
    if years is not None:
        normalized['experience_years'] = years
    return validate_profile(payload['profile']), normalized


def role_keywords(target):
    return list({term.casefold(): term for term in
                 [*ROLES[target['role']]['keywords'], *target['keywords']]}.values())


def matched_terms(terms, text):
    normalized = ' '.join(text.split())
    return [term for term in terms if re.search(
        r'(?<!\w)' + re.escape(' '.join(term.split())) + r'(?!\w)', normalized, re.IGNORECASE)]


def analyze(profile, target, keywords=None):
    keywords = role_keywords(target) if keywords is None else keywords
    evidence = [profile['headline'], profile['summary'], *profile['skills'], *profile['certifications']]
    evidence.extend(profile.get('projects', []))
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
    evidence_checks = (
        (8, any(entry['bullets'] for entry in profile['experience']), 'Describe verified work contributions.'),
        (6, any(entry['start'] for entry in profile['experience']), 'Provide confirmed dates for employment.'),
        (6, bool(profile['education'] or profile.get('projects') or profile['certifications']),
         'Include verified education, projects, or certifications when available.'),
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
        'template': target['template'],
        'score': coverage,
        'ats_readiness': sum(part['points'] for part in breakdown),
        'breakdown': breakdown, 'suggestions': suggestions,
        'matched': matched, 'missing': missing,
        'method': 'ResumeOps rubric: 50 keyword, 30 structure, 20 evidence points. Employer ATS rankings can differ.',
    }


def persist_analysis(report):
    """Optional Mongo projection. Application artifacts stay in the private filesystem."""
    uri = os.environ.get('RESUMEOPS_MONGODB_URI')
    if not uri or not report.get('ats'):
        return
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        raise DatabaseUnavailable('Install the MongoDB support dependency') from None
    try:
        with MongoClient(uri, timeoutMS=3500) as client:
            db = client[os.environ.get('RESUMEOPS_MONGODB_DATABASE', 'resumeops')]
            ats = report['ats']
            role = ats['role']
            db.role_keywords.replace_one({'_id': role}, {
                '_id': role, 'name': ROLES[role]['name'], 'keywords': ROLES[role]['keywords'],
            }, upsert=True)
            db.resume_analyses.replace_one({'_id': report['source_sha256']}, {
                '_id': report['source_sha256'], 'role': role, 'template': ats['template'],
                'score': ats['score'],
                **({'ats_readiness': ats['ats_readiness']} if 'ats_readiness' in ats else {}),
            }, upsert=True)
    except (PyMongoError, ValueError):
        raise DatabaseUnavailable('Configured MongoDB is unavailable; retry is automatic') from None

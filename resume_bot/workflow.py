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


def normalize_request(payload):
    if not isinstance(payload, dict) or 'profile' not in payload:
        return validate_profile(payload), None
    if set(payload) != {'profile', 'target'} or not isinstance(payload['target'], dict):
        raise ProfileError('Expected a candidate profile and target')
    target = payload['target']
    if set(target) - {'role', 'keywords', 'template'}:
        raise ProfileError('Unknown target option')
    role = target.get('role', '')
    template = target.get('template', 'standard')
    keywords = target.get('keywords', [])
    if not isinstance(role, str) or role not in ROLES:
        raise ProfileError('Select a target role')
    if not isinstance(template, str) or template not in TEMPLATES:
        raise ProfileError('Select a supported resume template')
    if not isinstance(keywords, list) or len(keywords) > 50 or any(
        not isinstance(term, str) or not 1 <= len(term.strip()) <= 100 for term in keywords
    ):
        raise ProfileError('Enter up to 50 keywords of 1–100 characters each')
    return validate_profile(payload['profile']), {
        'role': role, 'template': template, 'keywords': list(dict.fromkeys(term.strip() for term in keywords))
    }


def role_keywords(target):
    return list({term.casefold(): term for term in
                 [*ROLES[target['role']]['keywords'], *target['keywords']]}.values())


def analyze(profile, target, keywords=None):
    keywords = role_keywords(target) if keywords is None else keywords
    evidence = [profile['headline'], profile['summary'], *profile['skills'], *profile['certifications']]
    for entry in profile['experience']:
        evidence.extend([entry['position'], *entry['bullets']])
    for entry in profile['education']:
        evidence.extend([entry['degree'], entry['details']])
    text = ' '.join(' '.join(evidence).split())
    matched = [term for term in keywords if re.search(
        r'(?<!\w)' + re.escape(' '.join(term.split())) + r'(?!\w)', text, re.IGNORECASE)]
    missing = [term for term in keywords if term not in matched]
    return {
        'role': target['role'],
        'template': target['template'],
        'score': round(100 * len(matched) / len(keywords)) if keywords else 0,
        'matched': matched, 'missing': missing,
        'method': 'Literal target keyword coverage in candidate-supplied facts',
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
            }, upsert=True)
    except (PyMongoError, ValueError):
        raise DatabaseUnavailable('Configured MongoDB is unavailable; retry is automatic') from None

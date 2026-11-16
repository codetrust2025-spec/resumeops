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


def evidence_text(profile):
    evidence = [profile['headline'], profile['summary'], *profile['skills'], *profile['certifications']]
    for entry in profile['experience']:
        evidence.extend([entry['position'], *entry['bullets']])
    for entry in profile['education']:
        evidence.extend([entry['degree'], entry['details']])
    for entry in profile.get('projects', []):
        evidence.extend([entry['name'], *entry['technologies'], *entry['bullets']])
    return ' '.join(' '.join(evidence).split())


def analyze(profile, target, keywords=None):
    keywords = role_keywords(target) if keywords is None else keywords
    text = evidence_text(profile)
    matched = [term for term in keywords if _contains(text, term)]
    missing = [term for term in keywords if term not in matched]
    return {
        'role': target['role'],
        'template': target['template'],
        'score': round(100 * len(matched) / len(keywords)) if keywords else 0,
        'matched': matched, 'missing': missing,
        'method': 'Literal target keyword coverage in candidate-supplied facts',
    }


def _contains(text, term):
    return re.search(r'(?<!\w)' + re.escape(' '.join(term.split())) + r'(?!\w)', text, re.IGNORECASE) is not None


CATALOG = json.loads(Path(__file__).with_name('keyword_catalog.json').read_text(encoding='utf-8'))['roles']
ACTION_VERBS = ('developed', 'implemented', 'designed', 'optimized', 'built', 'created', 'managed',
                'led', 'integrated', 'automated', 'improved', 'reduced', 'delivered', 'migrated',
                'maintained', 'reviewed', 'tested', 'deployed', 'worked', 'collaborated')


def keyword_vocabulary():
    terms = [term for role in ROLES.values() for term in role['keywords']]
    for role in CATALOG.values():
        for group in role['groups'].values():
            terms.extend(group)
    return list({term.casefold(): term for term in terms}.values())


def job_description_keywords(text, limit=50):
    """Known role/tool terms that literally occur in a pasted job description. No network scraping."""
    if not isinstance(text, str) or len(text) > 50000:
        raise ProfileError('Paste a job description of up to 50,000 characters')
    clean = ' '.join(text.split())
    found = [term for term in keyword_vocabulary() if _contains(clean, term)]
    return sorted(found, key=lambda term: clean.casefold().find(' '.join(term.split()).casefold()))[:limit]


def assess(profile, target):
    """Level 1 style ATS breakdown, computed only from candidate-supplied facts.

    Weighting follows the Level 1 project (keywords 50, sections 30, content 20). Suggestions
    never propose claims: they name gaps the candidate may fill only if the fact is true.
    """
    coverage = analyze(profile, target)
    sections = {'headline': bool(profile['headline']), 'summary': bool(profile['summary']),
                'skills': bool(profile['skills']), 'experience': bool(profile['experience']),
                'education': bool(profile['education']),
                'projects or certifications': bool(profile.get('projects') or profile['certifications'])}
    section_score = round(100 * sum(sections.values()) / len(sections))
    bullets = [b for e in profile['experience'] for b in e['bullets']] + [
        b for p in profile.get('projects', []) for b in p['bullets']]
    if bullets:
        quantified = sum(bool(re.search(r'\d', b)) for b in bullets) / len(bullets)
        verbs = sum(b.split()[0].casefold().rstrip(',.') in ACTION_VERBS for b in bullets) / len(bullets)
        content_score = round(100 * (0.5 * quantified + 0.5 * verbs))
    else:
        quantified = verbs = 0
        content_score = 0
    overall = round(0.5 * coverage['score'] + 0.3 * section_score + 0.2 * content_score)
    missing_information = [name for name, present in sections.items() if not present]
    for key, label in (('phone', 'phone'), ('location', 'location'), ('linkedin', 'LinkedIn')):
        if not profile[key]:
            missing_information.append(label)
    if any(not e['bullets'] for e in profile['experience']):
        missing_information.append('achievements for every role')
    priorities = CATALOG.get(target['role'], {}).get('priorities', {})
    text = evidence_text(profile)
    priority_gaps = {level: [t for t in terms if not _contains(text, t)]
                     for level, terms in priorities.items()}
    suggestions = []
    if coverage['missing']:
        suggestions.append('Only if the candidate has real experience with them, add evidence for: '
                           + ', '.join(coverage['missing'][:6]) + '.')
    if bullets and quantified < 0.5:
        suggestions.append('Add measured outcomes the candidate can verify (numbers, scale, time saved).')
    if bullets and verbs < 0.5:
        suggestions.append('Start achievement bullets with an action verb such as Built, Led or Improved.')
    if missing_information:
        suggestions.append('Missing information: ' + ', '.join(missing_information) + '.')
    if not suggestions:
        suggestions.append('All target keywords and core sections are supported by candidate facts.')
    return {**coverage, 'overall': overall, 'passes': overall >= 80,
            'breakdown': {'keywords': coverage['score'], 'sections': section_score, 'content': content_score},
            'sections': sections, 'missing_information': missing_information,
            'priority_gaps': priority_gaps, 'suggestions': suggestions,
            'weights': {'keywords': 50, 'sections': 30, 'content': 20}}


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

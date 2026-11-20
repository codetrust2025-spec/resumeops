"""Explainable 100-point ATS readiness. Scores only what the resume states; never adds anything.

Eight categories, each with the checks that earned or lost its points:
  role relevance 15, keyword coverage 20, skills alignment 15, professional experience 15,
  project relevance 10, resume structure 10, content quality 10, evidence completeness 5.
Missing role keywords are reported as suggestions to add only if the candidate has used them.
"""
import re
from collections import Counter

from .certifications import recommended_certifications

FIRST_PERSON = re.compile(r'\b(?:I|me|my|mine|myself|we|our)\b')
CLICHES = re.compile(r'\b(?:results[- ]driven|proven track record|go-getter|rock ?star|ninja|guru|synergy|'
                     r'dynamic professional|passionate about|seasoned professional|hard[- ]working|team player|'
                     r'detail[- ]oriented|self[- ]starter|think outside the box|best[- ]in[- ]class|cutting[- ]edge|'
                     r'world[- ]class|highly motivated|leveraged)\b', re.IGNORECASE)
NUMBER = re.compile(r'\d')
LIST_MARKER = re.compile(r'^\s*(?:[-*•‣▪●–·]|\d+[.)])\s+')


def _check(items, points, passed, tip, detail=''):
    items.append({'check': tip if not passed else detail or tip, 'points': points if passed else 0,
                  'max': points, 'passed': bool(passed), 'tip': None if passed else tip})


def _category(key, label, checks):
    return {'key': key, 'label': label, 'points': sum(c['points'] for c in checks),
            'max': sum(c['max'] for c in checks), 'checks': checks}


def _words(text):
    return len(re.findall(r'[A-Za-z0-9][\w.+#/-]*', text or ''))


def score(profile, target, keywords, *, role_title, matched_terms, experience, evidence_text):
    """Return the explainable report. ``matched_terms(terms, text)`` is the shared keyword matcher."""
    automation = bool(target.get('automation_owner'))
    fresher = target.get('experience_years') == 0 or (experience.get('used') == 0 and not profile['experience'])
    skills = profile.get('skills') or []
    projects = profile.get('projects') or []
    bullets = [b for job in profile['experience'] for b in job['bullets']]
    matched = matched_terms(keywords, evidence_text)
    missing = [term for term in keywords if term not in matched]
    relevant_skills = [s for s in skills if matched_terms(keywords, s)]
    categories = []

    checks = []
    title = (profile.get('headline') or '').strip().casefold()
    _check(checks, 5, title == role_title.casefold() or role_title.casefold() in title,
           f'Use the target role "{role_title}" as the resume headline.', 'Headline names the target role.')
    _check(checks, 3, bool(profile.get('summary')) and (role_title.casefold() in profile['summary'].casefold()
                                                       or bool(matched_terms(keywords, profile['summary']))),
           'Let the career objective describe the confirmed experience that fits the role.', 'Career objective is written for the role.')
    work_text = ' '.join([*bullets, *(job['position'] for job in profile['experience']), *projects])
    _check(checks, 4, bool(matched_terms(keywords, work_text)),
           'Show role-relevant work in experience or project bullets.', 'Experience or projects use role technology.')
    _check(checks, 3, len(relevant_skills) >= 2, 'List at least two confirmed skills the role asks for.',
           f'{len(relevant_skills)} listed skills match the role.')
    categories.append(_category('role_relevance', 'Job role relevance', checks))

    checks = []
    target_count = max(8, (len(keywords) + 1) // 2)
    earned = min(20, round(20 * len(matched) / target_count)) if keywords else 0
    checks.append({'check': f'{len(matched)} of {len(keywords)} role keywords are supported by the resume; '
                            f'{target_count} or more earns full points.',
                   'points': earned, 'max': 20, 'passed': earned == 20,
                   'tip': None if earned == 20 else 'Add missing keywords only where the candidate has really used them.'})
    categories.append(_category('keyword_coverage', 'Supported keyword coverage', checks))

    checks = []
    share = len(relevant_skills) / len(skills) if skills else 0
    _check(checks, 9, share >= 0.5, 'Lead the skills section with skills the role uses.',
           f'{round(100 * share)}% of listed skills are relevant to the role.')
    groups = [name for name, items in (target.get('_groups') or {}).items() if matched_terms(items, ' '.join(skills))]
    _check(checks, 6, len(groups) >= 3 or (not target.get('_groups') and len(relevant_skills) >= 4),
           'Confirmed skills cover few of the role\'s areas (for example language, framework, database, testing).',
           f'Skills cover {len(groups)} role areas: {", ".join(groups)}.' if groups else 'Skills cover the role areas.')
    categories.append(_category('skills_alignment', 'Skills alignment', checks))

    checks = []
    if fresher:
        _check(checks, 5, bool(profile['education']), 'Add education with the institution and degree.', 'Education is listed.')
        _check(checks, 5, any(entry.get('end') for entry in profile['education']), 'Add the graduation year.',
               'Graduation year is stated.')
        # A fresher's contributions are the described projects ("Name - what was built") or internship bullets.
        _check(checks, 5, any(' - ' in p for p in projects) or any(job['bullets'] for job in profile['experience']),
               'Describe what you built in at least one project.', 'Projects or internships describe hands-on work.')
    else:
        _check(checks, 4, bool(profile['experience']) and all(job['start'] for job in profile['experience']),
               'Give every role its start date.', 'Every role has dates.')
        # Depth, not just presence: a single line per role reads as an underdeveloped experience section.
        _check(checks, 4, bool(profile['experience']) and all(len(job['bullets']) >= 3 for job in profile['experience']),
               'Describe at least three responsibilities for every role.', 'Every role describes the work in depth.')
        dated = experience.get('dated_years')
        _check(checks, 4, experience.get('supported', False),
               f"Selected {experience.get('claimed')} years, but dated roles show "
               f"{dated if dated is not None else 'no'} years; add the missing dated roles or lower the experience level.",
               'Dated roles support the experience level.')
        _check(checks, 3, bool(matched_terms(keywords, ' '.join(bullets))),
               'Mention the role technology the work actually used in the bullets.', 'Bullets name role technology.')
    categories.append(_category('experience', 'Professional experience', checks))

    checks = []
    _check(checks, 4, bool(projects), 'Add a project: a real one, or confirm one of the suggested proposals.',
           f'{len(projects)} project(s) listed.')
    _check(checks, 4, bool(projects) and bool(matched_terms(keywords, ' '.join(projects))),
           'Show the role technology used in the projects.', 'Projects use role technology.')
    _check(checks, 2, bool(projects), 'Only include projects the candidate built or confirmed.',
           'Projects are candidate-supplied or candidate-confirmed.')
    categories.append(_category('project_relevance', 'Project relevance', checks))

    checks = []
    _check(checks, 3, bool(profile['full_name'] and profile['email']), 'Include the candidate name and email.',
           'Name and email present.')
    _check(checks, 2, bool(profile.get('summary')), 'Add a career objective.', 'Career objective present.')
    _check(checks, 2, bool(skills), 'Add a technical skills section.', 'Technical skills section present.')
    _check(checks, 2, bool(profile['experience'] or profile['education']), 'Add experience or education.',
           'Experience or education present.')
    _check(checks, 1, bool(profile.get('phone') or profile.get('location')), 'Add a phone number or location.',
           'Phone or location present.')
    categories.append(_category('structure', 'Resume structure', checks))

    checks = []
    texts = [*bullets, *projects, profile.get('summary') or '']
    duplicates = [text for text, count in Counter(t.casefold() for t in bullets).items() if count > 1]
    _check(checks, 2, not duplicates, 'Remove repeated bullet points.', 'No repeated bullets.')
    _check(checks, 2, not any(LIST_MARKER.match(b) for b in bullets), 'Remove list markers typed inside bullet text.',
           'Bullets carry no stray list markers.')
    awkward = [b for b in bullets if not 5 <= _words(b) <= 45]
    _check(checks, 2, not awkward, 'Keep each bullet between 5 and 45 words.', 'Bullets are a readable length.')
    _check(checks, 2, not any(FIRST_PERSON.search(t) for t in texts), 'Write bullets without "I", "my" or "we".',
           'No first-person phrasing.')
    _check(checks, 2, not any(CLICHES.search(t) for t in texts), 'Replace generic phrases with concrete technical work.',
           'No generic filler phrases.')
    categories.append(_category('content_quality', 'Content quality', checks))

    checks = []
    _check(checks, 2, fresher or (bool(profile['experience']) and all(job['start'] for job in profile['experience'])),
           'Provide confirmed employment dates.', 'Employment dates confirmed.')
    _check(checks, 1, any(entry.get('end') for entry in profile['education']) or not profile['education'],
           'Provide the graduation year.', 'Graduation year present.')
    _check(checks, 1, bool(profile.get('linkedin') or profile.get('github') or profile.get('portfolio')),
           'Add a LinkedIn, GitHub or portfolio link if the candidate has one.', 'Profile link present.')
    _check(checks, 1, bool(profile['certifications'] or profile.get('achievements') or projects),
           'Add confirmed certifications, achievements or projects.', 'Supporting evidence present.')
    categories.append(_category('evidence', 'Evidence completeness', checks))

    total = sum(c['points'] for c in categories)
    tips = [c['tip'] for category in categories for c in category['checks'] if c['tip']]
    if missing:
        tips.append('Missing role keywords are suggestions: add one only if the candidate has used it.')
    return {
        'ats_readiness': total, 'score': round(100 * len(matched) / len(keywords)) if keywords else 0,
        'categories': categories,
        'breakdown': [{'label': c['label'], 'points': c['points'], 'max': c['max']} for c in categories],
        'suggestions': tips, 'recommendations': tips, 'matched': matched, 'missing': missing,
        'source_backed_skills': skills, 'role_relevant_skills': relevant_skills,
        'suggested_skills': missing[:15],
        # Advice only: a certification is printed once the candidate states they have completed it.
        'recommended_certifications': recommended_certifications(target, profile.get('certifications') or []),
        'method': ('ResumeOps explainable rubric: role relevance 15, keyword coverage 20, skills alignment 15, '
                   'experience 15, projects 10, structure 10, content quality 10, evidence 5. Scores only stated, '
                   'candidate-supported content; employer ATS rankings can differ.'),
    }

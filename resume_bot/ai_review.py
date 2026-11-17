"""Translate reviewed AI drafting suggestions into the existing watcher contract."""
import re

from .ai_first import IntakeError, validate_intake
from .workflow import ROLES, analyze, normalize_request


def role_intelligence(role):
    from .workflow import role_keywords

    catalog = ROLES.get(role)
    if not catalog:
        raise IntakeError('Choose a supported target role')
    terms = role_keywords({'role': role, 'keywords': []})
    pulse = {'java', 'javascript', 'python', 'react', 'angular', 'aws', 'node.js', 'css', 'kubernetes'}
    return {
        'label': catalog['name'],
        'terms': terms,
        'sources': [
            {'label': 'ResumeOps role catalog (curated keywords)', 'url': '', 'terms': terms},
            {'label': 'Naukri Pulse (published April 2024, overlapping role keywords)',
             'url': 'https://www.naukri.com/blog/stay-updated-with-top-it-skills-in-high-demand-from-recruiter-with-naukri-pulse/',
             'terms': [term for term in terms if term.casefold() in pulse]},
        ],
    }


def claims_for_review(draft):
    suggestions = draft['suggestions']
    claims = []

    def add(section, text, index=None):
        if text:
            claims.append({'id': str(len(claims)), 'section': section, 'text': text, 'index': index})

    add('Summary', suggestions['professional_summary']['text'])
    for section, field in [('Skill', 'technical_skills'), ('Competency', 'core_competencies'),
                           ('Achievement', 'key_achievements')]:
        for text in suggestions[field]['items']:
            add(section, text)
    for section, field, index in [('Responsibility', 'experience_responsibilities', 'experience_index'),
                                   ('Project detail', 'project_writeups', 'project_index')]:
        for entry in suggestions[field]:
            for text in entry['suggestions']:
                add(section, text, entry[index])
    from .ingest import _section
    for text in _section(draft.get('source', ''), r'certifications?')[:12]:
        add('Earned certification', text)
    return claims


def approved_resume(draft, approval):
    if not isinstance(approval, dict) or approval.get('attested') is not True:
        raise IntakeError('Confirm the facts before continuing')
    selection = approval.get('claims')
    if not isinstance(selection, dict):
        raise IntakeError('Expected a selection of reviewed claims')
    claims = {claim['id']: claim for claim in claims_for_review(draft)}
    if selection.keys() - claims.keys():
        raise IntakeError('The review contains an unknown suggestion')
    candidate = validate_intake(draft['candidate'])
    experience = [{**item, 'bullets': []} for item in candidate['work_experience']]
    project_details = [[] for _ in candidate['projects']]
    profile = {key: candidate[key] for key in
               ('full_name', 'email', 'phone', 'location', 'current_company', 'linkedin', 'github', 'portfolio')}
    profile['education'] = [
        {'institution': item['university'], 'degree': item['degree'], 'end': item['graduation_year'],
         'details': ' | '.join(filter(None, [item['specialization'], 'CGPA ' + item['cgpa'] if item['cgpa'] else '']))}
        for item in candidate['education']]
    profile['experience'] = experience
    profile['skills'] = []
    profile['certifications'] = []
    profile['achievements'] = []
    profile['coding_platforms'] = [' '.join(filter(None, item.values())) for item in candidate['coding_platforms']]
    source = draft.get('source', '')
    for key, claim in claims.items():
        if key not in selection:
            continue
        text = selection[key]
        if not isinstance(text, str) or not text.strip() or len(text) > 2500:
            raise IntakeError('Review text must be nonempty and at most 2500 characters')
        text = text.strip()
        # Quantified claims must occur verbatim in the imported candidate evidence.
        if re.search(r'\d\s*%|\$\s*\d|\b\d[\d,.]*\s+(?:users|customers|hours|days|employees)\b', text, re.I):
            if text.casefold() not in source.casefold():
                raise IntakeError('Remove quantified claims that are absent from the source resume')
        section = claim['section']
        if section == 'Summary':
            profile['summary'] = text
        elif section in ('Skill', 'Competency'):
            if text.casefold() not in {term.casefold() for term in profile['skills']}:
                profile['skills'].append(text)
        elif section == 'Responsibility':
            experience[claim['index']]['bullets'].append(text)
        elif section == 'Project detail':
            project_details[claim['index']].append(text)
        elif section == 'Earned certification':
            if text != claim['text']:
                raise IntakeError('Include only the certification as written in the source resume')
            profile['certifications'].append(text)
        elif section == 'Achievement':
            if text.casefold() not in source.casefold():
                raise IntakeError('Confirm achievements that occur in the source resume')
            profile['achievements'].append(text)
    profile['projects'] = [
        item['name'] + (' - ' + ' '.join(project_details[index]) if project_details[index] else '')
        for index, item in enumerate(candidate['projects'])
    ]
    if profile['projects']:
        profile['projects'] = profile['projects'][:30]
    target = {'role': candidate['role'], 'template': 'standard',
              'experience_years': min(25, int(candidate['years_of_experience']))}
    profile, target = normalize_request({'profile': profile, 'target': target})
    return {'profile': profile, 'target': target, 'ats': analyze(profile, target)}

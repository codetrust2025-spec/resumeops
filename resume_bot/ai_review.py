"""Translate reviewed AI drafting suggestions into the existing watcher contract."""
import re

from .ai_first import IntakeError, unsubstantiated_quantification, validate_intake
from .workflow import ROLES, analyze, matched_terms, normalize_request

# The nearest catalog role for a title the catalog does not list, so no title is left without keywords (seen live:
# "Software Tester" got none, and its resume scored 0 for keywords and skills). The resume keeps the title as typed
# (a custom role); only the keywords come from the nearest role. The first field that matches wins, specific fields
# before general ones; among its roles, the one whose technologies the title names most ("Azure DevOps Engineer":
# Azure Cloud Engineer), else the first. A technical title in no listed field ("Software Developer") takes GENERAL_ROLE.
# A title outside technology ("Warehouse Planning Manager", "Quality Control Manager") assumes no catalog terms.
NEAREST_ROLES = (
    (r'secur|cyber|penetration|\bsoc\b', ('cloud-engineer',)),
    (r'\bqa\b|quality|\btest|\bsdet\b', ('software-tester', 'automation-tester')),
    (r'data engineer|\betl\b|\belt\b|big data|data platform|data pipeline|data warehous',
     ('data-engineer', 'azure-data-engineer', 'gcp-data-engineer')),
    (r'generative|\bgen ?ai\b|\bllm', ('generative-ai-engineer',)),
    (r'data scien', ('data-scientist',)),
    (r'machine learning|deep learning|computer vision|\bml\b|\bai\b|\bnlp\b', ('ml-engineer', 'data-scientist')),
    (r'data analyst|business analyst|reporting|business intelligence|\bbi\b|\bmis\b|analytics|power ?bi|tableau',
     ('data-analyst',)),
    (r'salesforce|\bsfdc\b|\bcrm\b', ('salesforce-developer',)),
    (r'mobile|android|\bios\b|flutter|react native|swift|kotlin',
     ('mobile-app-developer', 'android-developer', 'ios-developer')),
    (r'full[- ]?stack|\bmern\b|\bmean\b', ('fullstack-developer', 'java-fullstack-developer', 'mern-stack-developer')),
    (r'designer|\bux\b', ('ui-developer',)),
    (r'front[- ]?end|\bui\b|react|angular|\bvue|javascript|typescript|web developer',
     ('frontend-developer', 'react-developer', 'angular-developer', 'ui-developer')),
    (r'database|\bdba\b|\bsql\b', ('backend-developer',)),
    (r'devops|\bbuild\b|release|\bsre\b|site reliability|platform|infrastructure|cloud|\baws\b|azure|\bgcp\b|'
     r'systems? (?:engineer|administrator)|network|linux|support engineer|technical support',
     ('cloud-engineer', 'devops-engineer', 'aws-cloud-engineer', 'azure-cloud-engineer', 'gcp-cloud-engineer')),
    (r'back[- ]?end|\bjava\b|python|\bnode|\.net|dotnet|c#|\bapi\b|django|spring|golang|\bgo\b|\bphp\b|ruby|rust',
     ('backend-developer', 'java-backend-developer', 'python-backend-developer', 'nodejs-developer', 'dotnet-developer')),
)
GENERAL_ROLE = 'software-engineer'


def nearest_role(title):
    """The id of the catalog role whose keywords suit a title the catalog does not list, or None."""
    from .generate import NON_SOFTWARE_TITLE, technical_job
    from .market import title_words

    words = title_words(title)
    # A title that contains a catalog title: "Senior Software Engineer", "Big Data Engineer", "Senior SDET". The
    # longest such title wins; a tie ("AWS Data Engineer": Data Engineer or AWS Engineer) is left to the fields below.
    contained = {}
    for key, row in ROLES.items():
        size = max((len(found) for found in map(title_words, [row['name'], *row['titleVariations']])
                    if found and found <= words), default=0)
        if size:
            contained[key] = size
    if contained:
        best = max(contained.values())
        found = [key for key, size in contained.items() if size == best]
        if len(found) == 1:
            return found[0]
    if NON_SOFTWARE_TITLE.search(title):
        return None
    for pattern, keys in NEAREST_ROLES:
        if re.search(pattern, title, re.IGNORECASE):
            present = [key for key in keys if key in ROLES]
            if present:
                # A more specific role only when it names more of the title than the field's first role does.
                named = max(present, key=lambda key: len(matched_terms(ROLES[key]['keywords'], title)))
                return named
    return GENERAL_ROLE if GENERAL_ROLE in ROLES and technical_job(title) else None


def role_intelligence(role, root=None):
    from .market import relevant_sources
    from .workflow import valid_role_title

    from .workflow import load_roles, role_by_title

    if not valid_role_title(role):
        raise IntakeError('Enter a valid target job title')
    load_roles()
    title = ' '.join(role.split())
    match = ROLES.get(title.casefold()) or role_by_title(title)
    exact = match['id'] if match else None
    if exact:
        title = ROLES[exact]['name']
        keywords = ROLES[exact]['keywords']
        sources = relevant_sources(title, root) if root is not None else []
        relevance = {
            term: sum(source['relevance'] for source in sources if matched_terms([term], ' '.join(source['terms'])))
            for term in keywords
        }
        terms = sorted(keywords, key=relevance.__getitem__, reverse=True)[:40]
        return {'label': title, 'role': exact, 'terms': terms,
                'sources': [*sources, {'label': 'ResumeOps role catalog (curated)', 'url': '', 'terms': keywords}],
                'keyword_relevance': {term: relevance[term] or 1 for term in terms}}
    sources = relevant_sources(title, root)
    keywords = list(dict.fromkeys(term for row in sources for term in row['terms']))
    if not keywords:
        nearest = nearest_role(title)
        keywords = ROLES[nearest]['keywords'] if nearest else []
        sources.append({'label': 'ResumeOps role catalog (curated)' + (f': nearest role {ROLES[nearest]["name"]}'
                                                                       if nearest else ''),
                        'url': '', 'terms': keywords})
    terms = keywords[:40]
    keyword_relevance = {
        term: sum(row.get('relevance', 0) for row in sources if term in row['terms'])
        for term in terms
    }
    return {'label': title, 'role': exact or 'custom', 'terms': terms, 'sources': sources,
            'keyword_relevance': keyword_relevance}


def claims_for_review(draft):
    suggestions = draft['suggestions']
    claims = []
    source = draft.get('source', '')
    candidate = draft['candidate']
    intake_evidence = ' '.join(
        value for category in ('work_experience', 'education', 'projects')
        for entry in candidate[category] for value in entry.values()
    )
    source_lines = [' '.join(line.split()) for line in source.splitlines() if line.strip()]

    def add(section, text, index=None):
        if not text:
            return
        lines = source_lines
        if section == 'Responsibility':
            employer = candidate['work_experience'][index]['company']
            lines = [line for offset, line in enumerate(source_lines)
                     if matched_terms([employer], ' '.join(source_lines[max(0, offset-4):offset+1]))]
        # Use literal evidence only. Paraphrases and claims with extra details still need confirmation.
        if matched_terms([text], ' '.join(lines)):
            provenance = 'verified_from_source'
            reason = 'This wording was found in your supplied resume. Confirm it is accurate.'
        elif section in ('Skill', 'Competency') and not matched_terms([text], source + ' ' + intake_evidence):
            provenance = 'unsupported_role_keyword'
            reason = 'Not found in your resume or details. Include only if you genuinely have this skill.'
        else:
            provenance = 'unverified_suggestion'
            reason = 'No matching statement was found in the supplied resume. Confirm before including.'
        claims.append({'id': str(len(claims)), 'section': section, 'text': text, 'index': index,
                       'provenance': provenance, 'reason': reason})

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
    # A pending draft created before custom titles must replay its original normalized payload.
    legacy_review = isinstance(draft.get('intelligence'), dict) and 'role' not in draft['intelligence']
    profile['competencies'] = []
    profile['certifications'] = []
    profile['achievements'] = []
    profile['coding_platforms'] = [' '.join(filter(None, item.values())) for item in candidate['coding_platforms']]
    source = draft.get('source', '')
    audit = []
    used_content = set()
    for key, claim in claims.items():
        if key not in selection:
            continue
        text = selection[key]
        if not isinstance(text, str) or not text.strip() or len(text) > 2500:
            raise IntakeError('Review text must be nonempty and at most 2500 characters')
        text = text.strip()
        # Review edits cannot reintroduce invented numbers/headcounts. Candidate approval
        # is not independent evidence of measured results or team size.
        if unsubstantiated_quantification(text, source):
            raise IntakeError('Remove quantified claims that are absent from the source resume')
        section = claim['section']
        if not legacy_review and section in ('Responsibility', 'Project detail'):
            normalized = ' '.join(re.findall(r'\w+', text.casefold()))
            if normalized in used_content:
                raise IntakeError('Keep work responsibilities and project details distinct; remove duplicate text')
            used_content.add(normalized)
        provenance = claim['provenance']
        if text != claim['text']:
            provenance = 'candidate_confirmed'
        audit.append({'id': key, 'section': section, 'text': text, 'provenance': provenance,
                      'candidate_confirmed': True, 'index': claim['index']})
        if section == 'Summary':
            profile['summary'] = text
        elif section in ('Skill', 'Competency'):
            if text.casefold() not in {term.casefold() for term in profile['skills'] + profile['competencies']}:
                profile['skills' if section == 'Skill' or legacy_review else 'competencies'].append(text)
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
    research = draft.get('intelligence') or role_intelligence(candidate['role'])
    known_terms = list(dict.fromkeys(term for row in ROLES.values() for term in row['keywords']))
    known_terms.extend(research['terms'])
    candidate_technologies = ' '.join([source, *profile['skills'], *profile['competencies']])
    for selected in ([] if legacy_review else audit):
        if selected['section'] not in ('Responsibility', 'Project detail'):
            continue
        terms = matched_terms(known_terms, selected['text'])
        if set(terms) - set(matched_terms(terms, candidate_technologies)):
            raise IntakeError('A work or project bullet includes a technology not found in your resume or confirmed skills.')
    profile['projects'] = [
        item['name'] + (' - ' + ' '.join(project_details[index]) if project_details[index] else '')
        for index, item in enumerate(candidate['projects'])
    ]
    if profile['projects']:
        profile['projects'] = profile['projects'][:30]
    role = research.get('role') or role_intelligence(candidate['role'])['role']
    target = {'role': role, 'template': 'standard', 'keywords': [] if legacy_review else research['terms'],
              'experience_years': min(25, int(candidate['years_of_experience']))}
    if role == 'custom':
        target['role_title'] = research['label']
    profile, target = normalize_request({'profile': profile, 'target': target})
    ats = analyze(profile, target)
    quality = {
        'verified_claims': len(audit),
        'keyword_matches': len(ats['matched']),
        'role_keywords_available': bool(ats['matched'] or ats['missing']),
        'experience_dates_support_level': ats['experience']['supported'],
        'role_title': research['label'],
    }
    return {'profile': profile, 'target': target, 'ats': ats, 'quality': quality, 'approved_claims': audit,
            'project_names': [item['name'] for item in candidate['projects'][:30]]}

"""Audit a candidate-approved resume: every factual line must come from the candidate's own
entries or from a statement the candidate approved (verified, edited, or explicitly opted in)."""
import json
import re
import unicodedata

APPROVED_PROVENANCE = {'verified_from_source', 'unverified_suggestion', 'unsupported_role_keyword',
                       'candidate_confirmed'}
CLAIM_FIELDS = (('summary', ('Summary',)), ('skills', ('Skill', 'Competency')),
                ('certifications', ('Earned certification',)), ('achievements', ('Achievement',)))


def normalize(text):
    text = unicodedata.normalize('NFKC', text)
    # LaTeX typesets these ASCII sequences as dashes and curly quotes; compare them as such.
    text = text.replace('---', '-').replace('--', '-').replace('``', '"').replace("''", '"').replace('`', "'")
    text = text.translate({0x2013: '-', 0x2014: '-', 0x2212: '-', 0x2022: ' ', 0x00b7: ' ',
                           0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x00a0: ' '})
    return re.sub(r'\s+', ' ', text).strip().casefold()


def claim_violations(profile, record):
    """Fields generated from the review that do not trace to an approved claim in the record."""
    problems = []
    if record.get('attested') is not True:
        problems.append('approval record is not attested')
    claims = record.get('claims')
    if not isinstance(claims, list) or any(not isinstance(claim, dict) or not isinstance(claim.get('text'), str)
                                           for claim in claims):
        return ['approval has invalid claim data']
    for claim in claims:
        if claim.get('provenance') not in APPROVED_PROVENANCE or claim.get('candidate_confirmed') is not True:
            problems.append(f"claim {claim.get('id')} lacks candidate approval")
    by_section = {}
    for claim in claims:
        by_section.setdefault(claim.get('section'), []).append(claim.get('text', ''))

    def traced(text, sections):
        return any(normalize(text) == normalize(item) for section in sections for item in by_section.get(section, []))

    for field, sections in CLAIM_FIELDS:
        values = profile.get(field) or []
        for value in ([values] if isinstance(values, str) else values):
            if value and not traced(value, sections):
                problems.append(f'{field}: {value!r} was not approved')
    for index, job in enumerate(profile.get('experience') or []):
        for bullet in job.get('bullets') or []:
            if not any(claim.get('section') == 'Responsibility' and claim.get('index') == index
                       and normalize(claim['text']) == normalize(bullet) for claim in claims):
                problems.append(f'experience {index} has an unapproved bullet')
    projects = profile.get('projects') or []
    names = record.get('project_names', [])
    if not isinstance(names, list) or len(names) != len(projects) or any(not isinstance(name, str) for name in names):
        return [*problems, 'saved project names do not match the reviewed profile']
    for index, (name, project) in enumerate(zip(names, projects)):
        details = [claim['text'] for claim in claims if claim.get('section') == 'Project detail'
                   and claim.get('index') == index]
        if project != name + (' - ' + ' '.join(details) if details else ''):
            problems.append(f'project {index} does not match the approved details')
    return problems


def records_for(folder, job):
    if not re.fullmatch(r'[a-z0-9-]+-[0-9a-f]{64}\.json', job):
        return []
    records = []
    for path in sorted((folder / job[:-5]).glob('*.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def matches_pdf_text(observed, profile, years):
    parts = [profile['full_name'], profile.get('headline', '')]
    parts.extend(profile.get(key, '') for key in ('email', 'phone', 'location', 'linkedin', 'github', 'portfolio'))
    experience = profile['experience']
    employer = profile.get('current_company')
    if employer and not any(entry['company'].casefold() == employer.casefold() for entry in experience):
        parts.extend(['Current company:', employer])
    if profile['summary']:
        parts.extend(['PROFESSIONAL SUMMARY', profile['summary']])
    if profile['skills']:
        parts.extend(['TECHNICAL SKILLS', 'Skills:', *profile['skills']])
    work = ['PROFESSIONAL EXPERIENCE'] if experience else []
    for entry in experience:
        work.extend([entry['position'], entry['company'], entry['start'], entry['end'], entry['location'],
                     *entry['bullets']])
    academic = []
    if profile.get('projects'):
        academic.extend(['PROJECTS', *profile['projects']])
    if profile['education']:
        academic.append('EDUCATION')
        for entry in profile['education']:
            academic.extend(entry[key] for key in ('degree', 'institution', 'start', 'end', 'details'))
    parts.extend(academic + work if years is not None and years < 1 else work + academic)
    for key, label in [('achievements', 'ACHIEVEMENTS'), ('coding_platforms', 'CODING PLATFORMS'),
                       ('certifications', 'CERTIFICATIONS')]:
        if profile.get(key):
            parts.extend([label, *profile[key]])

    def tokens(text):
        return re.findall(r"\w+(?:[.'@+#/-]\w+|[+#](?!\w))*|[%$]", normalize(text))

    expected = tokens(' '.join(parts))
    joined = re.sub(r'(?<=\w)-[ \t]*\r?\n[ \t]*(?=\w)', '', observed)
    return tokens(observed) == expected or tokens(joined) == expected

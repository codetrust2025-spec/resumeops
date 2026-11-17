"""Audit a candidate-approved resume: every factual line must come from the candidate's own
entries or from a statement the candidate approved (verified, edited, or explicitly opted in)."""
import re
import unicodedata

# Fixed template wording that carries no candidate facts.
TEMPLATE_TEXT = ('PROFESSIONAL SUMMARY', 'TECHNICAL SKILLS', 'PROFESSIONAL EXPERIENCE', 'PROJECTS',
                 'EDUCATION', 'ACHIEVEMENTS', 'CODING PLATFORMS', 'CERTIFICATIONS', 'Skills:',
                 'Current company:')
APPROVED_PROVENANCE = {'verified_from_source', 'unverified_suggestion', 'unsupported_role_keyword',
                       'candidate_confirmed'}
CLAIM_FIELDS = (('summary', ('Summary',)), ('skills', ('Skill', 'Competency')),
                ('certifications', ('Earned certification',)), ('achievements', ('Achievement',)))


def normalize(text):
    text = unicodedata.normalize('NFKC', text)
    text = text.translate({0x2013: '-', 0x2014: '-', 0x2212: '-', 0x2022: ' ', 0x00b7: ' ',
                           0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x00a0: ' '})
    return re.sub(r'\s+', ' ', text).strip().casefold()


def claim_violations(profile, record):
    """Fields generated from the review that do not trace to an approved claim in the record."""
    problems = []
    if record.get('attested') is not True:
        problems.append('approval record is not attested')
    claims = record.get('claims') or []
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
    for job in profile.get('experience') or []:
        for bullet in job.get('bullets') or []:
            if not traced(bullet, ('Responsibility',)):
                problems.append(f'experience bullet {bullet!r} was not approved')
    details = [normalize(item) for item in by_section.get('Project detail', [])]
    for project in profile.get('projects') or []:
        name, _, rest = project.partition(' - ')
        remainder = normalize(rest)
        for detail in sorted(details, key=len, reverse=True):
            remainder = remainder.replace(detail, ' ')
        if re.search(r'\w', remainder):
            problems.append(f'project {name!r} has unapproved detail {remainder.strip()!r}')
    return problems


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def unaccounted_pdf_words(pdf_text, profile):
    """Words in the PDF that come neither from the rendered profile nor from template wording.

    The profile is the exact approved request, so a non-empty result means the PDF contains
    text that nobody entered or approved."""
    text = normalize(pdf_text.replace('ﬁ', 'fi').replace('ﬂ', 'fl'))
    allowed = {normalize(item) for item in _strings(profile) if item and item.strip()}
    allowed |= {normalize(item) for item in TEMPLATE_TEXT}
    ordered = sorted(allowed, key=len, reverse=True)

    def strip(value):
        # Rendering joins fields with separators; remove whole values, longest first.
        for item in ordered:
            value = re.sub(r'(?<!\w)' + re.escape(item) + r'(?!\w)', ' ', value)
        return re.findall(r'[^\W_]{2,}', value)

    leftovers = strip(text)
    if leftovers:
        # Text wrapped by the PDF can split a value across a hyphenated line break; retry that.
        leftovers = strip(re.sub(r'(\w)- (\w)', r'\1\2', text))
    return leftovers

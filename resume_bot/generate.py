"""Role-based resume content that stays inside the verified facts.

* career objective: Ollama drafts a role-led paragraph. Invalid drafts are retried before a
  validated, evidence-safe fallback is used. The watcher revalidates before rendering.
* skill groups: the candidate's own skills, grouped by the role's areas.
* project proposals: role-specific project *suggestions* for candidates without projects. They are
  never resume content until the candidate confirms them (``confirmed_project``).
* role drafts: when the documents do not say what the candidate did in a job (or list no skills), Ollama
  drafts role-focused lines from the job title and dates (``draft_role_content``). Like proposals they are
  shown pre-filled for the candidate to keep, edit or clear, and are never resume content unconfirmed.

The selected target role steers all of it: drafts, proposals and the summary use the role's technologies
and only those of the candidate's skills that fit the role, so a Frontend resume is not mixed with cloud work.
"""
import json
import re

from .ats import CLICHES, FIRST_PERSON, LIST_MARKER
from .ollama import generate
from .workflow import ALIASES, ROLES, load_roles, matched_terms

SUMMARY_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['summary'],
                  'properties': {'summary': {'type': 'string', 'maxLength': 600}}}
PROPOSAL_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['projects'],
    'properties': {'projects': {'type': 'array', 'minItems': 2, 'maxItems': 3, 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['name', 'problem', 'stack', 'architecture', 'features', 'responsibilities', 'bullets'],
        'properties': {
            'name': {'type': 'string', 'maxLength': 70}, 'problem': {'type': 'string', 'maxLength': 240},
            'stack': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'maxLength': 40}},
            'architecture': {'type': 'string', 'maxLength': 280},
            'features': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string', 'maxLength': 120}},
            'responsibilities': {'type': 'string', 'maxLength': 240},
            'bullets': {'type': 'array', 'minItems': 3, 'maxItems': 5,
                        'items': {'type': 'string', 'minLength': 65, 'maxLength': 220}},
        }}}}}
METRIC = re.compile(r'\d+(?:[.,]\d+)?\s*(?:%|percent|x\b|times\b|users?\b|customers?\b|clients?\b|requests?\b|'
                    r'transactions?\b|ms\b|seconds?\b|hours?\b|days?\b|weeks?\b|months?\b|million|thousand|k\b)'
                    r'|\b(?:doubled|tripled|halved|quadrupled)\b'
                    r'|\b(?:increased|reduced|improved|boosted|cut|grew)\b[^.]*\bby\s+(?:\d|a factor|half|over)', re.IGNORECASE)
# Claims of real-world use. "client-side" is technology, not a customer claim.
PRODUCTION_CLAIM = re.compile(r'\b(?:in production|production users|served (?:\d|over|thousands|millions)|deployed to \d|revenue|'
                              r'profit|saved \$|paying customers|(?:\d[\d,]*|thousands of|millions of) (?:users|clients|customers))\b',
                              re.IGNORECASE)
PLAIN_WORDS = [(re.compile(p, re.IGNORECASE), r) for p, r in (
    (r'^\s*utiliz(?:ed|ing)\b', 'Used'), (r'^\s*leverag(?:ed|ing)\b', 'Used'), (r'^\s*spearhead(?:ed|ing)\b', 'Led'),
    (r'\butiliz(?:e|es|ed|ing)\b', 'use'), (r'\bleverag(?:e|es|ed|ing)\b', 'use'),
    (r'\b(?:robust|seamless(?:ly)?|cutting[- ]edge|state[- ]of[- ]the[- ]art)\s+', ''))]


# What a removed filler word can leave behind ("ensuring a robust and current setup" -> "ensuring a and current
# setup"; "leveraging its" -> ", use its"): such a drafted line is not offered.
BROKEN_WORDING = re.compile(r'\b(?:a|an|the)\s+(?:and|or|of|for|to|with|in)\b|,\s+use\b', re.IGNORECASE)


def plain_words(text):
    """Deterministic plain wording for proposal bullets ("Utilized X" -> "Used X"); facts are untouched."""
    for pattern, replacement in PLAIN_WORDS:
        text = pattern.sub(replacement, text)
    text = ' '.join(text.split())
    return text[:1].upper() + text[1:]


# Phrases that read as machine-written or are not resume style (a summary has no pronouns).
AI_TELLS = re.compile(r'\b(?:this candidate|the candidate|they|their|them|he|she|his|her|skill ?set|brings an?|'
                      r'possess(?:es|ing)?|actionable insights|leverag(?:e|es|ed|ing)|seamless(?:ly)?|spearhead(?:ed|ing|s)?|'
                      r'demonstrat(?:ing|es|ed) (?:expertise|proficiency|skills?)|adept|robust|utiliz(?:e|es|ed|ing)|'
                      r'expertise in|proficient in|well[- ]versed)\b', re.IGNORECASE)
# Qualities a summary may claim only when the candidate's own facts say so: "scalable backend systems" is an
# unsupported claim when no bullet mentions scale.
QUALITY_CLAIMS = re.compile(r'\b(?:scalab(?:le|ility)|high[- ]performance|high[- ]performing|large[- ]scale|'
                            r'enterprise[- ](?:grade|level|scale)|mission[- ]critical|production[- ](?:grade|ready)|'
                            r'highly[- ]available|high[- ]availability|fault[- ]tolerant|resilient|world[- ]class|'
                            r'best[- ]in[- ]class|innovative|complex|efficient(?:ly)?|optimi[sz]ed|performant|'
                            r'reliable|reliability|secure|distributed|real[- ]time|cloud[- ]native|end[- ]to[- ]end)\b',
                            re.IGNORECASE)
# Verbs that claim a level of responsibility. A summary may use one only when the candidate's own
# responsibilities do ("managing infrastructure" is inflated when the resume says "provisioned").
CLAIM_VERBS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ('led', r'\b(?:led|leads?|leading)\b'), ('managed', r'\bmanag(?:e|es|ed|ing|ement)\b'),
    ('architected', r'\barchitect(?:s|ed|ing|ure)?\b'), ('designed', r'\bdesign(?:s|ed|ing)?\b'),
    ('owned', r'\bown(?:s|ed|ing|ership)?\b'), ('drove', r'\b(?:drive|drives|drove|driving)\b'),
    ('optimized', r'\boptimi[sz](?:e|es|ed|ing|ation)\b'), ('scaled', r'\bscal(?:e|es|ed|ing)\b'),
    ('mentored', r'\bmentor(?:s|ed|ing)?\b'), ('maintained', r'\bmaintain(?:s|ed|ing)?\b'),
    ('deployed', r'\bdeploy(?:s|ed|ing|ment)?\b'), ('migrated', r'\bmigrat(?:e|es|ed|ing|ion)\b'),
    ('automated', r'\bautomat(?:e|es|ed|ing|ion)\b'), ('monitored', r'\bmonitor(?:s|ed|ing)?\b'),
    ('administered', r'\badminist(?:er|ers|ered|ering|ration)\b'), ('supervised', r'\bsupervis(?:e|es|ed|ing)\b'))]
# A sentence that only lists confirmed skills ("Works with Python, SQL and Docker.").
LIST_SENTENCE = re.compile(r'\s*(?:also\s+)?(?:works with|hands-on with|transferable, hands-on skills include|'
                           r'(?:technical |core )?skills include|tools include|familiar with)\b', re.IGNORECASE)
PUNCTUATION = str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"', '–': '-', '—': '-',
                             '−': '-', '…': '...', ' ': ' ', '•': ' ', '·': ' ', '°': ''})


def plain(text):
    """Generated or edited text reduced to characters the LaTeX template always typesets."""
    import unicodedata
    text = unicodedata.normalize('NFKC', str(text)).translate(PUNCTUATION)
    return ' '.…22399 tokens truncated…(details):
    """Education details without a CGPA of 8.0 or less; nothing else is changed or added."""
    parts = [part.strip() for part in str(details or '').split(' | ')]
    kept = []
    for part in parts:
        grade = GRADE.search(part)
        if grade:
            value, scale = float(grade.group(1)), float(grade.group(2) or 10)
            if not scale or value / scale * 10 <= 8.0:
                continue
        if part:
            kept.append(part)
    return ' | '.join(kept)


class _Doc:
    """Collects LaTeX and the plain reading-order text side by side."""

    def __init__(self):
        self.tex, self.text = [PREAMBLE], []

    def raw(self, tex, *text):
        self.tex.append(tex)
        self.text.extend(t for t in text if t)

    def section(self, title):
        self.raw(r'\section*{' + title + '}\n', title)

    def bullets(self, items, terms=(), limit=2):
        """One bullet per item; up to ``limit`` of ``terms`` (the resume's technologies) bold in each."""
        items = [_bullet(item) for item in items if _bullet(item)]
        if items:
            self.raw(r'\begin{itemize}' + '\n' + ''.join(r'\item ' + emphasized(i, terms, limit) + '\n' for i in items) +
                     r'\end{itemize}' + '\n', *items)

    def entry(self, title, place, dates, extra='', muted_place=False):
        """'Title | Place' left, dates right; an optional muted second line."""
        place_tex = r'{\small\color{ink}' + paragraph(place) + '}' if muted_place else paragraph(place)
        left = (r'\textbf{' + paragraph(title) + '}' + (r' \textbar{} ' + place_tex if place else '')
                if title else r'\textbf{' + paragraph(place) + '}')
        right = r'\hfill{\small\color{ink}' + paragraph(dates) + '}' if dates else ''
        self.raw(left + right + r'\par' + '\n', title, place, dates)
        if extra:
            self.raw(r'{\small\color{ink}' + paragraph(extra) + r'}\par' + '\n', extra)


def _split_project(project):
    if ' - ' not in project:
        return project, [], []
    name, rest = project.split(' - ', 1)
    parts = [p.strip() for p in rest.split(' | ') if p.strip()]
    stack = []
    if parts and parts[0].startswith('Tech stack: '):
        stack = [s.strip() for s in parts.pop(0)[len('Tech stack: '):].split(',') if s.strip()]
    return name, stack, parts


def _build(profile, template='standard', years=None, *, complete_sections=False, career_objective=False):
    doc = _Doc()
    doc.raw(r'{\centering {\LARGE\bfseries ' + paragraph(profile['full_name']) + r'}\par' + '\n', profile['full_name'])
    if profile.get('headline'):
        doc.raw(r'\vspace{1pt}{\large\color{accent} ' + paragraph(profile['headline']) + r'}\par' + '\n', profile['headline'])
    contact = [profile[k] for k in ('email', 'phone', 'location', 'linkedin', 'github', 'portfolio') if profile.get(k)]
    doc.raw(r'\vspace{2pt}{\small ' + r' \textbar{} '.join(paragraph(x) for x in contact) + r'}\par}' + '\n', *contact)
    employer = profile.get('current_company')
    if employer and not any(item['company'].casefold() == employer.casefold() for item in profile['experience']):
        doc.raw(r'{\small Current company: ' + paragraph(employer) + r'}\par' + '\n', 'Current company:', employer)

    # The technologies to highlight: the listed skills that are tools, not concepts or practices.
    concept_groups = {'concepts', 'architecture & practices', 'practices'}
    concepts = {item for group in profile.get('skill_groups') or [] if group['name'].casefold() in concept_groups
                for item in group['items']}
    terms = [skill for skill in profile.get('skills') or [] if skill not in concepts]

    def summary():
        if profile.get('summary'):
            doc.section('Career Objective')
            lead = [profile['headline']] if profile.get('headline') else []
            if career_objective:
                from .generate import OBJECTIVE_EMPHASIS
                tex = emphasized(profile['summary'], [*lead, *OBJECTIVE_EMPHASIS], 3)
            else:
                # Existing revisions keep their original LaTeX when rebuilt or retried.
                sentences = re.split(r'(?<=[.!?]) (?=[A-Z0-9])', profile['summary'])
                tex = ' '.join(emphasized(s, lead + terms if i == 0 else terms, 2 if i == 0 and lead else 1)
                               for i, s in enumerate(sentences))
            doc.raw(tex + r'\par' + '\n', profile['summary'])

    def skills():
        if profile.get('skills'):
            doc.section('Technical Skills')
        if profile.get('skills'):
            groups = profile.get('skill_groups') or [{'name': 'Skills', 'items': profile['skills']}]
            for group in groups:
                doc.raw(r'\textbf{' + paragraph(group['name']) + r':} ' + paragraph(', '.join(group['items'])) + r'\par' + '\n',
                        group['name'] + ':', ', '.join(group['items']))

    def competencies():
        if profile.get('competencies'):
            doc.section('Core Competencies')
            doc.raw(paragraph(', '.join(profile['competencies'])) + r'\par' + '\n', ', '.join(profile['competencies']))

    def experience():
        if profile.get('experience'):
            doc.section('Professional Work Experience')
        for x in profile['experience']:
            doc.entry(x['position'], x['company'], ' -- '.join(p for p in (x['start'], x['end']) if p), x.get('location', ''))
            doc.bullets(x['bullets'], terms)
            doc.raw(r'\vspace{2pt}' + '\n')

    def projects():
        if profile.get('projects'):
            doc.section('Projects')
        for project in profile.get('projects', []):
            name, stack, details = _split_project(project)
            # The technologies sit beside the project title, comma-separated.
            doc.entry(name, ', '.join(stack), '', muted_place=True)
            doc.bullets(details, terms)

    def education():
        if profile.get('education'):
            doc.section('Education')
        for x in profile['education']:
            doc.entry(x['degree'], x['institution'], ' -- '.join(p for p in (x['start'], x['end']) if p),
                      education_details(x.get('details', '')))

    def listed(field, title):
        if profile.get(field):
            doc.section(title)
            doc.bullets(profile[field])

    def certifications():
        listed('certifications', 'Certifications')

    # Career Objective, Technical Skills, Professional Work Experience, Projects, Certifications, Education. A section
    # with nothing in it is never printed: an empty heading reads as an unfinished resume.
    if complete_sections:
        order = (summary, skills, experience, projects, certifications, education, competencies)
    elif years is not None and years < 1:
        order = (summary, skills, competencies, projects, certifications, education, experience)
    else:
        order = (summary, skills, competencies, experience, projects, certifications, education)
    for part in order:
        part()
    listed('achievements', 'Achievements')
    listed('coding_platforms', 'Coding Platforms')
    doc.raw(r'\end{document}' + '\n')
    content = ''.join(doc.tex)
    # Use the page well without adding content: short resumes get a larger type and roomier spacing,
    # long ones the compact spacing. Derived from the content only, so re-rendering is identical.
    lines = sum(max(1, len(fragment) // 95) for fragment in doc.text)
    if template == 'standard' and lines <= 55:
        content = (content.replace('[10pt,a4paper]', '[11pt,a4paper]')
                   .replace(r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{3pt}')
                   .replace(r'{8pt}{4pt}', r'{11pt}{5pt}').replace('itemsep=1pt', 'itemsep=2pt'))
    elif template == 'standard' and lines > 120:
        template = 'compact'
    if template == 'compact':
        content = content.replace(r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{1pt}')
        content = content.replace(r'{8pt}{4pt}', r'{5pt}{2pt}').replace('itemsep=1pt', 'itemsep=0pt')
    return content, doc.text


def render(profile, template="standard", years=None, *, complete_sections=False, career_objective=False):
    """Render the resume using the layout recorded when the generation job was submitted."""
    return _build(profile, template, years, complete_sections=complete_sections, career_objective=career_objective)[0]


def expected_text(profile, years=None, *, complete_sections=False):
    """The resume text in the reading order of the compiled PDF (for the strict PDF text check)."""
    return _build(profile, 'standard', years, complete_sections=complete_sections)[1]

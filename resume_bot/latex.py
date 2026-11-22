"""Self-contained ATS-readable one/two-page LaTeX CV. Never executes AI-generated TeX.

One block model drives both the LaTeX and ``expected_text`` (the reading order of the compiled PDF),
so the strict PDF text check always matches the template exactly.
"""
import re

LIST_MARKER = re.compile(r'^\s*(?:[-*\u2022\u2023\u25aa\u25cf\u2013\u00b7]|\d+[.)])\s+')
PREAMBLE = r'''\documentclass[10pt,a4paper]{article}
\usepackage[margin=1.45cm,top=1.25cm,bottom=1.3cm]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\usepackage{xcolor}
\definecolor{ink}{HTML}{3A4656}
\definecolor{accent}{HTML}{1F4E79}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{2pt}
\setlength{\emergencystretch}{2em}
\titleformat{\section}{\bfseries\color{accent}\normalsize\scshape}{}{0pt}{}[\vspace{-3pt}\titlerule]
\titlespacing*{\section}{0pt}{8pt}{4pt}
\setlist[itemize]{leftmargin=1.2em,topsep=1pt,itemsep=1pt,parsep=0pt,partopsep=0pt,label=\textbullet}
\begin{document}
\raggedright
'''


def escape(s):
    # Replace each character in one pass so our escape sequences aren't re-escaped.
    d={'\\':r'\textbackslash{}', '&':r'\&', '%':r'\%', '$':r'\$',
       '#':r'\#', '_':r'\_', '{':r'\{','}':r'\}', '~':r'\textasciitilde{}',
       '^':r'\textasciicircum{}'}
    return ''.join(d.get(ch,ch) for ch in str(s))


def paragraph(s):
    return escape(s).replace('\n',' ')


def _bullet(text):
    return LIST_MARKER.sub('', text).strip()


def emphasized(text, terms, limit):
    """LaTeX for ``text`` with the first ``limit`` distinct ``terms`` it names (in reading order) set in bold.
    Only typesetting changes: the words, and so the PDF text check, stay exactly the same."""
    spans = []
    for term in sorted({t for t in terms if len(t) >= 2}, key=len, reverse=True):
        flags = 0 if len(term) <= 3 else re.IGNORECASE  # "Go", "Git": exact case, never "go" or "digit"
        match = re.search(r'(?<![\w.+#/-])' + re.escape(term) + r'(?![\w+#/-]|\.\w)', text, flags)
        if match and not any(start < match.end() and match.start() < end for start, end in spans):
            spans.append((match.start(), match.end()))
    spans = sorted(spans)[:max(0, limit)]
    out, position = [], 0
    for start, end in spans:
        out += [paragraph(text[position:start]), r'\textbf{' + paragraph(text[start:end]) + '}']
        position = end
    return ''.join(out) + paragraph(text[position:])


# CGPA is printed only above 8.0 (on a 10-point scale, or its equivalent on another stated scale).
GRADE = re.compile(r'\b(?:CGPA|GPA|CPI)\b\s*[:=-]?\s*(\d+(?:\.\d+)?)(?:\s*(?:/|out of)\s*(\d+(?:\.\d+)?))?', re.IGNORECASE)


def education_details(details):
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


def _build(profile, template='standard', years=None, *, complete_sections=False, career_objective=False, fit=None):
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
    if fit and template == 'standard' and lines <= 120:
        # The one-page fit (chosen by the builder): the 10pt standard type with tighter vertical spacing.
        content = fitted(content, fit)
    elif template == 'standard' and lines <= 55:
        content = (content.replace('[10pt,a4paper]', '[11pt,a4paper]')
                   .replace(r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{3pt}')
                   .replace(r'{8pt}{4pt}', r'{11pt}{5pt}').replace('itemsep=1pt', 'itemsep=2pt'))
    elif template == 'standard' and lines > 120:
        template = 'compact'
    if template == 'compact':
        content = content.replace(r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{1pt}')
        content = content.replace(r'{8pt}{4pt}', r'{5pt}{2pt}').replace('itemsep=1pt', 'itemsep=0pt')
    return content, doc.text


# One-page fit: when a standard resume leaves only a few lines on page 2, the builder tries the same resume with the
# 10pt standard type and tighter VERTICAL spacing (top and bottom margins, paragraph, section and list spacing, line
# spread) and keeps it only when it fits one page. Every word stays; for a 10pt resume the font, size and line width
# are unchanged, so each line breaks exactly where it did (a short resume printed in the larger 11pt type returns to
# the normal 10pt). Seen live: a QA Automation Engineer resume printed only its last project lines and Education on
# page 2.
FIT_LEVELS = {
    'snug': ((r'top=1.25cm,bottom=1.3cm', r'top=1cm,bottom=1cm'), (r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{1pt}'),
             (r'{8pt}{4pt}', r'{6pt}{2pt}'), ('itemsep=1pt', 'itemsep=0pt')),
    'tight': ((r'top=1.25cm,bottom=1.3cm', r'top=0.9cm,bottom=0.9cm'), (r'\setlength{\parskip}{2pt}', r'\setlength{\parskip}{1pt}'),
              (r'{8pt}{4pt}', r'{5pt}{2pt}'), ('itemsep=1pt', 'itemsep=0pt'), ('topsep=1pt', 'topsep=0pt'),
              ('\\begin{document}\n', '\\linespread{0.96}\n\\begin{document}\n')),
}


def fitted(content, fit):
    for old, new in FIT_LEVELS[fit]:
        if content.count(old) != 1:
            raise ValueError(f'One-page fit {fit}: the layout setting {old!r} is not in the standard preamble')
        content = content.replace(old, new)
    return content


def render(profile, template="standard", years=None, *, complete_sections=False, career_objective=False, fit=None):
    """Render the resume using the layout recorded when the generation job was submitted. ``fit`` (a FIT_LEVELS
    name): the one-page fit of a standard 10pt resume, chosen by the builder; ignored by the other layouts."""
    return _build(profile, template, years, complete_sections=complete_sections, career_objective=career_objective,
                  fit=fit)[0]


def expected_text(profile, years=None, *, complete_sections=False):
    """The resume text in the reading order of the compiled PDF (for the strict PDF text check)."""
    return _build(profile, 'standard', years, complete_sections=complete_sections)[1]

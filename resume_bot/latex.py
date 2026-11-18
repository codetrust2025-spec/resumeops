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


class _Doc:
    """Collects LaTeX and the plain reading-order text side by side."""

    def __init__(self):
        self.tex, self.text = [PREAMBLE], []

    def raw(self, tex, *text):
        self.tex.append(tex)
        self.text.extend(t for t in text if t)

    def section(self, title):
        self.raw(r'\section*{' + title + '}\n', title)

    def bullets(self, items):
        items = [_bullet(item) for item in items if _bullet(item)]
        if items:
            self.raw(r'\begin{itemize}' + '\n' + ''.join(r'\item ' + paragraph(i) + '\n' for i in items) +
                     r'\end{itemize}' + '\n', *items)

    def entry(self, title, place, dates, extra=''):
        """'Title | Place' left, dates right; an optional muted second line."""
        left = r'\textbf{' + paragraph(title) + '}' + (r' \textbar{} ' + paragraph(place) if place else '')
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


def _build(profile, template='standard', years=None):
    doc = _Doc()
    doc.raw(r'{\centering {\LARGE\bfseries ' + paragraph(profile['full_name']) + r'}\par' + '\n', profile['full_name'])
    if profile.get('headline'):
        doc.raw(r'\vspace{1pt}{\large\color{accent} ' + paragraph(profile['headline']) + r'}\par' + '\n', profile['headline'])
    contact = [profile[k] for k in ('email', 'phone', 'location', 'linkedin', 'github', 'portfolio') if profile.get(k)]
    doc.raw(r'\vspace{2pt}{\small ' + r' \textbar{} '.join(paragraph(x) for x in contact) + r'}\par}' + '\n', *contact)
    employer = profile.get('current_company')
    if employer and not any(item['company'].casefold() == employer.casefold() for item in profile['experience']):
        doc.raw(r'{\small Current company: ' + paragraph(employer) + r'}\par' + '\n', 'Current company:', employer)

    def summary():
        if profile.get('summary'):
            doc.section('Professional Summary')
            doc.raw(paragraph(profile['summary']) + r'\par' + '\n', profile['summary'])

    def skills():
        if profile.get('skills'):
            doc.section('Technical Skills')
            groups = profile.get('skill_groups') or [{'name': 'Skills', 'items': profile['skills']}]
            for group in groups:
                doc.raw(r'\textbf{' + paragraph(group['name']) + r':} ' + paragraph(', '.join(group['items'])) + r'\par' + '\n',
                        group['name'] + ':', ', '.join(group['items']))
        if profile.get('competencies'):
            doc.section('Core Competencies')
            doc.raw(paragraph(', '.join(profile['competencies'])) + r'\par' + '\n', ', '.join(profile['competencies']))

    def experience():
        if not profile['experience']:
            return
        doc.section('Professional Experience')
        for x in profile['experience']:
            doc.entry(x['position'], x['company'], ' -- '.join(p for p in (x['start'], x['end']) if p), x.get('location', ''))
            doc.bullets(x['bullets'])
            doc.raw(r'\vspace{2pt}' + '\n')

    def projects():
        if not profile.get('projects'):
            return
        doc.section('Projects')
        for project in profile['projects']:
            name, stack, details = _split_project(project)
            doc.entry(name, '', '', 'Tech stack: ' + ', '.join(stack) if stack else '')
            doc.bullets(details)

    def education():
        if not profile['education']:
            return
        doc.section('Education')
        for x in profile['education']:
            doc.entry(x['degree'], x['institution'], ' -- '.join(p for p in (x['start'], x['end']) if p), x.get('details', ''))

    def listed(field, title):
        if profile.get(field):
            doc.section(title)
            doc.bullets(profile[field])

    fresher = years is not None and years < 1
    order = (summary, skills, projects, education, experience) if fresher else (summary, skills, experience, projects, education)
    for part in order:
        part()
    listed('certifications', 'Certifications')
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


def render(profile, template="standard", years=None):
    """``years`` is the verified experience level for this build: under one year (a fresher) puts
    projects and education before experience; otherwise experience comes first."""
    return _build(profile, template, years)[0]


def expected_text(profile, years=None):
    """The resume text in the reading order of the compiled PDF (for the strict PDF text check)."""
    return _build(profile, 'standard', years)[1]

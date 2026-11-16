"""Self-contained ATS-readable one/two-page LaTeX CV. Never executes AI-generated TeX."""
import re


def escape(s):
    # Replace each character in one pass so our escape sequences aren't re-escaped.
    d={'\\':r'\textbackslash{}', '&':r'\&', '%':r'\%', '$':r'\$',
       '#':r'\#', '_':r'\_', '{':r'\{','}':r'\}', '~':r'\textasciitilde{}',
       '^':r'\textasciicircum{}'}
    return ''.join(d.get(ch,ch) for ch in str(s))


def paragraph(s):
    return escape(s).replace('\n',' ')


def render(profile, template="standard"):
    out=[r'''\documentclass[10pt,a4paper]{article}
\usepackage[margin=1.53cm,top=1.35cm,bottom=1.4cm]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\usepackage{xcolor}
\definecolor{ink}{HTML}{152338}
\definecolor{accent}{HTML}{21618C}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{2.5pt}
\setlength{\emergencystretch}{2em}
\titleformat{\section}{\bfseries\color{accent}\normalsize}{}{0pt}{}[\titlerule]
\titlespacing*{\section}{0pt}{9pt}{4pt}
\setlist[itemize]{leftmargin=1.25em,topsep=1pt,itemsep=0pt,parsep=0pt,partopsep=0pt}
\begin{document}
\raggedright
''']
    out.append(r'{\centering {\LARGE\bfseries '+paragraph(profile['full_name'])+r'}\\[3pt]'+'\n')
    if profile['headline']:
        out.append(r'{\color{ink}\large '+paragraph(profile['headline'])+r'}\\[3pt]'+'\n')
    contact=[profile[k] for k in ('email','phone','location','linkedin','github','portfolio') if profile.get(k)]
    out.append(r'{\small '+r'\quad $\vert$ \quad '.join(paragraph(x) for x in contact)+r'}\par}'+'\n')
    employer = profile.get('current_company')
    if employer and not any(item['company'].casefold() == employer.casefold() for item in profile['experience']):
        out.append(r'{\small Current company: '+paragraph(employer)+r'}\par'+'\n')
    if profile['summary']:
        out.append(r'\section*{PROFESSIONAL SUMMARY}'+'\n'+paragraph(profile['summary'])+'\n')
    if profile['skills']:
        out.append(r'\section*{TECHNICAL SKILLS}'+'\n'+r'\textbf{Skills:} '+r' \textbullet{} '.join(paragraph(x) for x in profile['skills'])+'\n')
    if profile['experience']:
        out.append(r'\section*{PROFESSIONAL EXPERIENCE}'+'\n')
        for x in profile['experience']:
            dates=' -- '.join(p for p in (x['start'],x['end']) if p)
            out.append(r'\textbf{'+paragraph(x['position'])+r'} \textbar{} '+paragraph(x['company'])+(r'\\' if dates or x['location'] else '')+'\n')
            if dates or x['location']:
                out.append(r'{\small\color{ink}'+paragraph(dates)+
                           (r' \quad $\cdot$ \quad '+paragraph(x['location']) if x['location'] else '')+r'}\\[1pt]'+'\n')
            if x['bullets']:
                out.append(r'\begin{itemize}'+'\n')
                for b in x['bullets']:
                    out.append(r'\item '+paragraph(b)+'\n')
                out.append(r'\end{itemize}'+'\n')
            out.append(r'\vspace{2pt}'+'\n')
    if profile.get('projects'):
        out.append(r'\section*{PROJECTS}'+'\n'+r'\begin{itemize}'+'\n')
        for project in profile['projects']:
            out.append(r'\item '+paragraph(project)+'\n')
        out.append(r'\end{itemize}'+'\n')
    if profile['education']:
        out.append(r'\section*{EDUCATION}'+'\n')
        for x in profile['education']:
            dates=' -- '.join(p for p in (x['start'],x['end']) if p)
            out.append(r'\textbf{'+paragraph(x['degree'])+r'} \textbar{} '+paragraph(x['institution'])+(r'\\' if x['start'] or x['end'] or x['details'] else '')+'\n')
            extra='  |  '.join(i for i in (dates,x['details']) if i)
            if extra:
                out.append(r'{\small '+paragraph(extra)+r'}\\[2pt]'+'\n')
    if profile['certifications']:
        out.append(r'\section*{CERTIFICATIONS}'+'\n')
        for c in profile['certifications']:
            out.append(r'\textbullet{} '+paragraph(c)+r'\quad '+'\n')
    out.append(r'\end{document}'+'\n')
    content = ''.join(out)
    if template == 'compact':
        content = content.replace(r'\setlength{\parskip}{2.5pt}', r'\setlength{\parskip}{1.5pt}')
        content = content.replace(r'{9pt}{4pt}', r'{6pt}{3pt}')
    return content

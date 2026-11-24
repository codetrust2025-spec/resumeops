"""Resume generation standards, checked on every built resume for its selected role.

The report lists each standard with whether this resume meets it and why. A resume is "verified to the standards"
only when every applicable check passes; otherwise the report names exactly what needs the candidate's attention
(for example a certification to confirm, or more skills to tick). Nothing here changes the resume or adds content:
facts the candidate has not confirmed are never added to make a check pass."""
from .generate import (SKILL_BREADTH, TITLE_SKILLS, _near_duplicate, draft_lines, role_entry, skill_groups,
                       split_project, technical_job, title_problems, work_skills)
from .workflow import role_name

SECTIONS = ('Career Objective', 'Technical Skills', 'Professional Work Experience', 'Projects', 'Certifications',
            'Education')
PROJECTS = (2, 3)          # projects on a role resume
PROJECT_BULLETS = (4, 5)   # bullets per project
KEYWORD_COVERAGE = 50      # % of the role's search keywords the resume shows
ATS_READY = 70             # ATS readiness out of 100


def _check(checks, key, label, ok, detail, applies=True):
    checks.append({'id': key, 'label': label, 'ok': bool(ok) if applies else None, 'detail': detail})


def standards_report(profile, target, years=None, ats=None, pdf_checks=None):
    """{'met', 'applicable', 'verified', 'checks': [{id, label, ok (None: not applicable), detail}]}."""
    title = role_name(target)
    role = role_entry(target)
    skills = profile.get('skills') or []
    experience = profile.get('experience') or []
    projects = [split_project(p) for p in profile.get('projects') or []]
    fresher = years is not None and years < 1
    checks = []

    present = {'Career Objective': bool(profile.get('summary')), 'Technical Skills': bool(skills),
               'Professional Work Experience': bool(experience), 'Projects': bool(projects),
               'Certifications': bool(profile.get('certifications')), 'Education': bool(profile.get('education'))}
    needed = [s for s in SECTIONS if not (s == 'Professional Work Experience' and fresher and not experience)]
    absent = [s for s in needed if not present[s]]
    _check(checks, 'sections', 'Six sections in order', not absent,
           'Career Objective, Technical Skills, Professional Work Experience, Projects, Certifications and Education.'
           if not absent else 'Missing: ' + ', '.join(absent) + '.')

    problems = title_problems(profile.get('headline'), title, skills)
    _check(checks, 'title', 'Resume title for the selected role', not problems,
           f'"{profile.get("headline", "")}" names {title} first, then up to {TITLE_SKILLS} listed skills.'
           if not problems else problems[0].capitalize() + '.')

    coverage = (ats or {}).get('score')
    _check(checks, 'keywords', 'Job description keyword coverage',
           coverage is not None and coverage >= KEYWORD_COVERAGE,
           f'{coverage}% of the role\'s recruiter search keywords appear in the resume.' if coverage is not None
           else 'No keyword report.', applies=coverage is not None)

    groups = profile.get('skill_groups') or (skill_groups(skills, role) if skills else [])
    other = sum(len(g['items']) for g in groups if g['name'] in ('Other skills', 'Skills'))
    _check(checks, 'skills', 'Technical skills categorized and comprehensive',
           len(groups) >= 3 and other <= len(skills) // 3 and len(skills) >= SKILL_BREADTH,
           f'{len(skills)} skills in {len(groups)} categories.' + (
               f' Tick more of the role\'s skills you have used (at least {SKILL_BREADTH}).'
               if len(skills) < SKILL_BREADTH else ''))

    short, repeated = [], []
    for rank, job in enumerate(experience):
        if technical_job(job['position']) and len(job['bullets']) < min(7, draft_lines(rank, job['position'])):
            short.append(f"{job['company']} ({len(job['bullets'])} lines)")
        others = [line for other in experience if other is not job for line in other['bullets']]
        repeated += [line for line in job['bullets'] if _near_duplicate(line, others)]
    _check(checks, 'experience', 'Professional experience depth and variety', not short and not repeated,
           'Every technical job has 7-8 distinct lines.' if not short and not repeated else
           ' '.join(filter(None, ['Fewer lines than the role needs: ' + ', '.join(short) + '.' if short else '',
                                  f'{len(repeated)} line(s) repeat another job.' if repeated else ''])),
           applies=bool(experience))

    work = [line for job in experience for line in job['bullets']]
    thin = [name for name, stack, bullets in projects if not stack or not PROJECT_BULLETS[0] <= len(bullets)]
    echoed = [name for name, _, bullets in projects if any(_near_duplicate(b, work) for b in bullets)]
    count_ok = PROJECTS[0] <= len(projects) <= (len(projects) if fresher else PROJECTS[1])
    _check(checks, 'projects', 'Projects: 2-3, 4-5 points each, stack beside the title',
           count_ok and not thin and not echoed,
           f'{len(projects)} project(s).' + (' Needs a stack and 4-5 points: ' + ', '.join(thin) + '.' if thin else '')
           + (' Repeats work lines: ' + ', '.join(echoed) + '.' if echoed else ''), applies=bool(projects))

    _check(checks, 'certifications', 'Certifications', bool(profile.get('certifications')),
           f"{len(profile.get('certifications') or [])} confirmed by the candidate." if profile.get('certifications')
           else 'None confirmed. Tick a recommended certification only after completing it.')

    incomplete = [e.get('degree') or e.get('institution') or 'An entry' for e in profile.get('education') or []
                  if not (e.get('degree') and e.get('institution') and (e.get('end') or e.get('start')))]
    _check(checks, 'education', 'Education: college, degree and years', bool(profile.get('education')) and not incomplete,
           'College, degree and duration; CGPA shown only above 8.0.' if not incomplete else
           'Add the college, degree and years for: ' + ', '.join(incomplete) + '.')

    missing = work_skills(profile, target)
    _check(checks, 'consistency', 'Skills, experience and projects consistent', not missing,
           'Every role technology used in the work and projects is listed in Technical Skills.' if not missing
           else 'Used but not listed: ' + ', '.join(missing) + '.')

    readiness = (ats or {}).get('ats_readiness')
    _check(checks, 'ats', 'ATS compatibility', readiness is not None and readiness >= ATS_READY,
           f'ATS readiness {readiness}/100.', applies=readiness is not None)

    document = pdf_checks or {}
    _check(checks, 'document', 'PDF, LaTeX and Overleaf output',
           bool(document.get('searchable')) and bool(document.get('pages')),
           f"Searchable PDF, {document.get('pages')} page(s); LaTeX source and Overleaf project built from it.",
           applies=pdf_checks is not None)

    applicable = [c for c in checks if c['ok'] is not None]
    met = sum(1 for c in applicable if c['ok'])
    return {'met': met, 'applicable': len(applicable), 'verified': met == len(applicable), 'checks': checks}

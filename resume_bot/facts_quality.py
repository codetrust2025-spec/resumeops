"""Candidate facts, tidied before any resume is drafted from them: never new facts, only the same facts said cleanly.

* Skills typed without commas ("Azure data factory Azure Databricks pyspark") are split into the technologies they
  name, each with its proper name (Azure Data Factory, Azure Databricks, PySpark).
* Technology names in work lines and project stacks get their proper capitalisation ("azure migrate" ->
  "Azure Migrate"), and job titles, institutions and specialisations are title-cased ("Senior data engineer" ->
  "Senior Data Engineer"); acronyms and deliberate casing are kept. Only letter case changes, never the words.
* A sentence typed both as a job's work and as a project's description is kept once, with the project.
* Placeholders ("NA", "N/A", "-", "none") are not facts.
* Weak openings ("Worked on", "Responsible for") and lines claiming another job title are found, so the standards
  report names them.
"""
import re

from .workflow import ALIASES, ROLES, load_roles, matched_terms, term_pattern

# Technologies the catalog does not list as role skills but candidates name (cloud migration and infrastructure).
# Product names only: a practice such as "Networking" is an ordinary word in a sentence ("Set up networking
# configurations"), never recased there (it is still a skill, from the role catalog).
EXTRA_SKILLS = ['Azure Migrate', 'Azure Site Recovery', 'Azure Virtual Machines', 'Azure Backup', 'Azure Monitor',
                'Azure Active Directory', 'Microsoft Entra ID', 'Azure Kubernetes Service', 'VMware',
                'Windows Server', 'Active Directory', 'PowerShell', 'Shell Scripting', 'Unix', 'Jenkins', 'Ansible',
                'Terraform', 'Docker', 'Kubernetes', 'Linux', 'Python', 'SQL', 'Excel', 'Tableau', 'Power BI']
PLACEHOLDER = re.compile(r'^\s*(?:n\.?\s*a\.?|n/a|na|nil|none|null|nothing|not applicable|-+|\.+)\s*$', re.IGNORECASE)
SMALL_WORDS = {'a', 'an', 'and', 'as', 'at', 'by', 'for', 'in', 'of', 'on', 'or', 'the', 'to', 'with', 'from', 'via'}
# Short designations that are acronyms when typed in lowercase ("sde ii", "qa engineer").
ACRONYMS = {'sde', 'sdet', 'qa', 'ui', 'ux', 'ml', 'ai', 'bi', 'etl', 'it', 'hr', 'gcp', 'aws', 'api', 'iot', 'erp',
            'crm', 'sap', 'mis', 'dba', 'ii', 'iii', 'iv', 'ece', 'eee', 'cse', 'ece', 'mca', 'bca', 'mba'}
WEAK_OPENER = re.compile(r'^\s*(?:worked (?:on|in|with|as)|responsible for|involved in|handled|helped|assisted|'
                         r'participated in|was part of|part of|did|doing|i have|i am|i was)\b', re.IGNORECASE)
ROLE_CLAIM = re.compile(r'\bas an? (?:[A-Za-z/.+-]+ ){0,3}(?:engineer|developer|analyst|consultant|architect|'
                        r'administrator|tester|lead)\b', re.IGNORECASE)


def is_placeholder(value):
    return isinstance(value, str) and bool(PLACEHOLDER.match(value))


def skill_vocabulary():
    """Every technology name ResumeOps knows, in its proper spelling, longest first (so "Azure Data Factory" is found
    before "Azure")."""
    from .generate import named_tools
    load_roles()
    names = [*(term for role in ROLES.values() for term in role['keywords']),
             *(item for role in ROLES.values() for items in (role.get('groups') or {}).values() for item in items),
             *(item for role in ROLES.values() for items in (role.get('resumeGroups') or {}).values() for item in items),
             *named_tools(), *EXTRA_SKILLS]
    seen, out = set(), []
    for name in names:
        key = ' '.join(name.split()).casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(' '.join(name.split()))
    return sorted(out, key=len, reverse=True)


def _spans(text, vocabulary):
    """[(start, end, canonical name)] of the known technologies in ``text``: the longest typed match wins, so
    "azure migrate" is Azure Migrate (not the "Azure" alias of Microsoft Azure followed by "migrate")."""
    found = [(match.start(), match.end(), term) for term in vocabulary for match in term_pattern(term).finditer(text)]
    spans = []
    # Longest typed match first; on a tie, the name typed exactly ("databricks" is Databricks, not the "Azure
    # Databricks" it is also an alias of), and never a name that adds words the candidate did not type.
    for start, end, term in sorted(found, key=lambda item: (item[0] - item[1],
                                                            text[item[0]:item[1]].casefold() != item[2].casefold(),
                                                            _adds_words(text[item[0]:item[1]], item[2]), item[0])):
        if not any(start < e and s < end for s, e, _ in spans):
            spans.append((start, end, term))
    return sorted(spans)


def _adds_words(typed, term):
    """Whether the proper name ``term`` has a word the typed text does not ("Azure" for "databricks")."""
    have = set(re.findall(r'[a-z0-9]+', typed.casefold()))
    return any(word not in have and not any(word in h or h in word for h in have)
               for word in re.findall(r'[a-z0-9]+', term.casefold()))


def _named(typed, term):
    """How a typed technology is printed: its proper name when that only changes case or spacing, otherwise as
    typed (title-cased when typed in lower case)."""
    if typed.casefold() == term.casefold() or (typed == typed.lower() and not _adds_words(typed, term)):
        return term
    return smart_title(typed) if typed == typed.lower() else typed


def _gap_skills(gap):
    """Words between two known technologies: separate skills when joined by "and", "or" or "&"; a fragment of a
    technology name ("js") is dropped."""
    out = []
    for part in re.split(r'\s+(?:and|or|&)\s+|^(?:and|or|&)\s+|\s+(?:and|or|&)$', gap.strip(' ,;/|&-'),
                         flags=re.IGNORECASE):
        part = (part or '').strip(' ,;/|&-')
        if not part or is_placeholder(part) or part.casefold() in {'js', 'ms', 'and', 'or', 'etc'} or len(part) < 2:
            continue
        out.append(smart_title(part) if part == part.lower() else part)
    return out


def smart_title(text):
    """Title case for names typed in lower case ("rajamahendri institute of engineering and technology"): small words
    stay lower case, acronyms and deliberate casing (B.Tech, LTM, iOS) are kept."""
    words = str(text or '').split()
    out = []
    for index, word in enumerate(words):
        bare = word.strip('()[],.;:').casefold()
        if any(ch.isupper() for ch in word[1:]) or any(ch.isdigit() for ch in word) or '.' in word.strip('.'):
            out.append(word)  # deliberate casing, versions, abbreviations
        elif bare in ACRONYMS:
            out.append(word.upper())
        elif index and bare in SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:])
    return ' '.join(out)


def needs_title(text):
    """A name typed mostly in lower case (at least one significant word starts with a lower-case letter)."""
    return any(word[:1].islower() and word.strip('()[],.;:').casefold() not in SMALL_WORDS
               for word in str(text or '').split())


def tool_vocabulary():
    """Named technologies only (no practices such as "Streaming Pipelines" or "Dashboards", which are ordinary
    words in a sentence), longest first."""
    from .generate import named_tools
    seen, out = set(), []
    for name in [*named_tools(), *EXTRA_SKILLS]:
        key = ' '.join(name.split()).casefold()
        if key not in seen:
            seen.add(key)
            out.append(' '.join(name.split()))
    return sorted(out, key=len, reverse=True)


def canonical_case(text, vocabulary=None):
    """``text`` with each named technology typed entirely in lower case given its proper name ("using azure migrate
    and ASR" -> "using Azure Migrate and ASR"). Only letter case changes; aliases, deliberate spellings and ordinary
    words ("dashboards") are kept."""
    vocabulary = vocabulary or tool_vocabulary()
    text = str(text or '')
    out, position = [], 0
    for start, end, term in _spans(text, vocabulary):
        typed = text[start:end]
        out.append(text[position:start])
        same = typed.casefold().rstrip('s') == term.casefold().rstrip('s') or typed.casefold() == term.casefold()
        out.append(term + typed[len(term):] if same and typed == typed.lower() and len(typed) >= len(term) else typed)
        position = end
    return ''.join(out) + text[position:]


def split_skills(items, vocabulary=None):
    """The candidate's skills as separate technologies with their proper names. An entry naming several known
    technologies without commas is split into them; words between them that are not a known technology stay as one
    skill, title-cased. Order and the candidate's other entries are kept; duplicates are dropped."""
    vocabulary = vocabulary or skill_vocabulary()
    out = []
    for item in items or []:
        text = ' '.join(str(item).split()).strip(' ,;/|')
        if not text or is_placeholder(text):
            continue
        spans = _spans(text, vocabulary)
        if not spans:
            out.append(smart_title(text) if text == text.lower() and len(text.split()) <= 4 else text)
            continue
        if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(text):
            out.append(_named(text, spans[0][2]))
            continue
        position = 0
        for start, end, term in spans:
            out.extend(_gap_skills(text[position:start]))
            out.append(_named(text[start:end], term))
            position = end
        out.extend(_gap_skills(text[position:]))
    seen, kept = set(), []
    for skill in out:
        if skill.casefold() not in seen:
            seen.add(skill.casefold())
            kept.append(skill)
    return kept


def _project_parts(project):
    from .generate import split_project
    return split_project(project)


def _join_project(name, stack, parts):
    pieces = (['Tech stack: ' + ', '.join(stack)] if stack else []) + list(parts)
    return name + (' - ' + ' | '.join(pieces) if pieces else '')


def normalize_facts(facts):
    """The same facts, tidied (see the module docstring). Idempotent; returns a new dict."""
    from .generate import _near_duplicate
    vocabulary, tools = skill_vocabulary(), tool_vocabulary()
    facts = {**facts}
    facts['skills'] = split_skills(facts.get('skills') or [], vocabulary)
    experience = []
    for job in facts.get('experience') or []:
        job = {**job}
        if job.get('position') and needs_title(job['position']):
            job['position'] = smart_title(job['position'])
        job['bullets'] = [canonical_case(line, tools) for line in job.get('bullets') or []
                          if not is_placeholder(line)]
        experience.append(job)
    projects = []
    for project in facts.get('projects') or []:
        name, stack, parts = _project_parts(project)
        if is_placeholder(name):
            continue
        stack = split_skills(stack, vocabulary)
        parts = [canonical_case(part, tools) for part in parts if part and not is_placeholder(part)]
        projects.append(_join_project(name, stack, parts))
    if 'projects' in facts:
        facts['projects'] = projects
    # A sentence typed as both a job's work and a project's description is the project's: printed once, there.
    project_lines = [part for project in projects for part in _project_parts(project)[2]]
    if project_lines:
        for job in experience:
            job['bullets'] = [line for line in job['bullets']
                              if not (line.casefold() in {p.casefold() for p in project_lines}
                                      or _near_duplicate(line, project_lines))]
    facts['experience'] = experience
    education = []
    for entry in facts.get('education') or []:
        entry = {**entry}
        if entry.get('institution') and needs_title(entry['institution']):
            entry['institution'] = smart_title(entry['institution'])
        if entry.get('degree') and needs_title(entry['degree']):
            entry['degree'] = smart_title(entry['degree'])
        details = [part.strip() for part in str(entry.get('details') or '').split(' | ') if part.strip()]
        if details and not re.search(r'\d|cgpa|gpa|percent', details[0], re.IGNORECASE) \
                and len(details[0].split()) <= 6 and needs_title(details[0]):
            details[0] = smart_title(details[0])
        entry['details'] = ' | '.join(details)
        education.append(entry)
    facts['education'] = education
    return facts


def weak_lines(lines):
    """The lines that open weakly ("Worked on ...") or claim another job title ("... as an Azure Cloud Engineer")."""
    return [line for line in lines or [] if WEAK_OPENER.search(line) or ROLE_CLAIM.search(line)]

"""Extract source text and build source-evidenced fields; fail closed on unsupported/unclear input."""
from .ollama import generate
import json
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from .profile import validate_profile, ProfileError


class ExtractionError(ValueError):
    pass


def read_resume_text(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ('.txt','.md'):
        text = path.read_text(encoding='utf8')
    elif ext == '.pdf':
        try:
            proc = subprocess.run(['pdftotext','-layout',str(path),'-'],capture_output=True,encoding='utf-8',timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ExtractionError('pdftotext missing or timed out') from exc
        if proc.returncode != 0:
            raise ExtractionError('PDF text extraction failed')
        text = proc.stdout
    elif ext == '.docx':
        try:
            with zipfile.ZipFile(path) as z:
                content = z.read('word/document.xml')
            ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            root=ET.fromstring(content)
            text='\n'.join(''.join(t.text or '' for t in p.findall('.//w:t',ns))
                           for p in root.findall('.//w:p',ns))
        except (KeyError,zipfile.BadZipFile,ET.ParseError) as exc:
            raise ExtractionError('Cannot read DOCX') from exc
    else:
        raise ExtractionError(f'Unsupported source: {ext}; use .json/.pdf/.docx/.txt')
    if len(text.strip()) < 120:
        raise ExtractionError('Source is too short or scanned/image-only; cannot verify details')
    if len(text) > 75_000:
        raise ExtractionError('Source too long; refusing truncation that could lose facts')
    return text


def _norm(text):
    return re.sub(r'\s+', ' ', str(text)).strip().casefold()


def _get(field, source, required=False, lenient=False):
    if not isinstance(field,dict):
        raise ExtractionError('AI field lacks quoted source evidence')
    value = field.get('value','')
    evidence = field.get('evidence','')
    if not isinstance(value,str) or not isinstance(evidence,str):
        raise ExtractionError('AI evidence is not text')
    value,evidence=value.strip(),evidence.strip()
    if required and not value:
        raise ExtractionError('Missing required field from source')
    if not value:
        return ''
    # Require the value itself to occur verbatim in the source, avoiding inferred experience and skills.
    if _norm(value) not in _norm(source):
        raise ExtractionError(f'Unverified extracted value: {value[:60]}')
    if lenient:
        # Review mode: a verbatim value is kept even when its quoted context is not verbatim. Table and
        # form layouts make the model join cells ("Company Name: Example Pvt Ltd"), and dropping the value
        # lost real facts. Every entry is tied to its own lines again later (automation.source_profile).
        return value
    if _norm(evidence) not in _norm(source):
        raise ExtractionError(f'Unverified extracted value: {value[:60]}')
    if _norm(value) not in _norm(evidence):
        raise ExtractionError(f'Evidence does not support value: {value[:60]}')
    return value


SCALARS = ('full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github', 'portfolio',
           'current_company', 'summary')


def _review_schema():
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties),
                'additionalProperties': False}

    def array(items):
        return {'type': 'array', 'items': items}

    evidence = obj({key: {'type': 'string'} for key in ('value', 'evidence')})
    fields = {key: evidence for key in SCALARS}
    fields.update(skills=array(evidence), certifications=array(evidence), projects=array(evidence))
    fields['experience'] = array(obj({
        **dict.fromkeys(('company', 'position', 'start', 'end', 'location'), evidence),
        'bullets': array(evidence),
    }))
    fields['education'] = array(obj(dict.fromkeys(
        ('institution', 'degree', 'start', 'end', 'details'), evidence)))
    return obj(fields)


REPAIRABLE = ('full_name', 'email')  # recovered deterministically from the source when the model misses them
SECTIONS = re.compile(r'^(professional\s+)?(summary|profile|objective|about me|(technical\s+|core\s+|key\s+)?skills|'
                      r'(work\s+|professional\s+)?experience|employment( history)?|education|academic.*|'
                      r'projects?|personal projects|certifications?|achievements|awards|languages|interests|'
                      r'contact|references)\s*:?$', re.I)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<![\w/])\+?\d[\d ().-]{7,}\d(?![\w/])')
URL = re.compile(r'(?:https?://|www\.)[^\s|,;()<>]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|io|dev|me|net|org|in|app|site)/[^\s|,;()<>]*', re.I)
PRESENT = re.compile(r'present|current|now|till date|to date|ongoing', re.I)


def _lines(source):
    return [line.strip() for line in source.splitlines() if line.strip()]


def _looks_like_name(line):
    words = line.split()
    return (2 <= len(words) <= 5 and not SECTIONS.match(line) and not re.search(r'[\d@/:|,]', line)
            and all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", word) for word in words)
            and sum(word[0].isupper() for word in words) >= len(words) - 1
            # "Senior Data Analyst" is a headline, not a person.
            and not any(TITLE_WORDS.match(word.strip('.')) for word in words))


NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.'-]*")
TITLE_WORDS = re.compile(r'^(?:resume|cv|curriculum|vitae|senior|junior|lead|principal|staff|chief|head|software|'
                         r'data|engineer|developer|manager|analyst|consultant|intern|tester|architect|scientist|'
                         r'designer|specialist|administrator|executive|associate|programmer|officer|trainee|'
                         r'fresher|graduate|student|full|stack|frontend|backend|devops|cloud|qa)$', re.I)
NAME_PARTICLES = {'de', 'da', 'del', 'van', 'von', 'der', 'bin', 'binti', 'al', 'el', 'la', 'le', 'di', 'du'}
SEPARATORS = re.compile(r'\s*(?:\||•|·|–|—|\s-\s|,)\s*')
INSTITUTION = re.compile(r'universit|college|institut|school|academy|vidyalaya|polytechnic', re.I)


def _complete_name(name, head):
    """A model may quote only part of the name line ("Priya Raman" from "Priya Example Raman").
    Use the verbatim name segment of a header line that contains the quoted words in order.
    Segments with job-title words, contact details or headings are never treated as names."""
    words = _norm(name).split()
    if not words:
        return name
    for line in head[:3]:
        segment = SEPARATORS.split(line, maxsplit=1)[0].strip()
        tokens = segment.split()
        if (not len(words) < len(tokens) <= 8 or SECTIONS.match(segment) or EMAIL.search(segment)
                or URL.search(segment) or not all(NAME_TOKEN.fullmatch(token) for token in tokens)
                or any(TITLE_WORDS.match(token.strip('.')) for token in tokens)
                or not all(token[0].isupper() or token in NAME_PARTICLES for token in tokens)):
            continue
        remaining = iter(token.casefold() for token in tokens)
        if all(word in remaining for word in words):  # every quoted word, in the same order
            return segment
    return name


def _complete_degree(entry, lines):
    """Keep the branch with the degree when the model quoted only "B.Tech" from a line such as
    "B.Tech, Computer Science, Example University, 2022". The result is a verbatim span."""
    degree = entry.get('degree') or ''
    if not degree:
        return
    pattern = re.compile(r'\s+'.join(map(re.escape, degree.split())), re.I)
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        rest = line[match.end():]
        branch = re.match(r"(\s*(?:,|:|\s-\s|\s–\s|\(|\s+in\s+)\s*)([A-Za-z&/.' ]{2,80}?)\s*(\)|,|\||\s-\s|\s–\s|\d|$)",
                          rest, re.I)
        if not branch:
            return
        text = branch.group(2).strip()
        institution = _norm(entry.get('institution') or '')
        if (not text or len(text.split()) > 7 or INSTITUTION.search(text) or re.fullmatch(r'(?i)hons?\.?|honours', text)
                or (institution and (_norm(text) in institution or institution in _norm(text)))
                or _norm(text) in _norm(degree)):
            return
        if '(' in branch.group(1):
            if branch.group(3) != ')':
                return
            end = match.end() + branch.end(3)  # keep "M.Tech (Data Science)" balanced
        else:
            end = match.end() + branch.start(2) + len(branch.group(2).rstrip())
        entry['degree'] = line[match.start():end]
        return


def _section(source, names):
    """Lines under a heading such as "Projects" until the next heading (verbatim, bullets stripped)."""
    out, inside = [], False
    for line in _lines(source):
        header = SECTIONS.match(line.rstrip(':'))
        if header:
            inside = bool(re.fullmatch(names, line.rstrip(':').strip(), re.I))
            continue
        if inside:
            item = re.sub(r'^[-*\u2022\u25cf\u25aa\u2013\u00b7]\s*', '', line).strip()
            if item:
                out.append(item)
    return out


def _split_list(value):
    parts = [part.strip(' .') for part in re.split(r'\s*[,;|\u2022\u00b7]\s*|\s+/\s+', value)]
    return [part for part in parts if part]


# Form-style resumes (often a two-column Word table) state facts as labelled rows:
# "5. Company Name" | "Example Works Pvt Ltd". Only these known labels are read; a label never matches
# a sentence, and a preferred or target role is not a past job.
FORM_LABELS = [(key, re.compile(pattern, re.I)) for key, pattern in (
    ('full_name', r"(?:full |candidate(?:'s)? )?name"),
    ('email', r'e-?mail(?: id| address)?'),
    ('phone', r'(?:phone|mobile|contact)(?: number| no\.?)?'),
    ('company', r'(?:current |present |last |previous )?(?:company|employer|organi[sz]ation)(?: name)?'),
    ('position', r'(?:current |present |job |last )?(?:designation|job title|position)'),
    ('period', r'(?:employment |work(?:ing)? |service |job |experience )?(?:period|duration|tenure|dates?)|working since|'
               r'date of joining|joining date'),
    ('degree', r'(?:highest |educational |education )?(?:qualification|degree)(?: name)?'),
    ('institution', r'(?:college|university|institute|institution|school)(?: name)?'),
    ('education_end', r'(?:education |degree )?(?:completion|passing|passed[- ]?out|pass[- ]?out|graduation)(?: year)?|'
                      r'year of (?:passing|graduation|completion)|education (?:completion |passing |passed[- ]?out )?year'),
    ('skills', r'(?:(?:primary|key|core|main|technical|it|professional)\s+)*skills(?: set)?|technolog(?:y|ies)|'
               r'tech(?:nical)? stack'),
)]
FORM_ROW = re.compile(r'\s*(?:\d{1,2}[.)]\s*)?([A-Za-z][A-Za-z \'./&-]{1,48}?)\s*:?\s*')
FORM_INLINE = re.compile(r'\s*(?:\d{1,2}[.)]\s*)?([A-Za-z][A-Za-z \'./&-]{1,48}?)\s*:\s*(\S.*)')


def _form_label(text):
    text = ' '.join(text.split())
    return next((key for key, pattern in FORM_LABELS if pattern.fullmatch(text)), None)


def form_fields(source):
    """{key: (verbatim value, line index)} for known labelled rows; the first row of each label wins."""
    lines = _lines(source)
    found = {}
    for index, line in enumerate(lines):
        inline = FORM_INLINE.fullmatch(line)
        key = _form_label(inline.group(1)) if inline else None
        if key:
            found.setdefault(key, (inline.group(2).strip(), index))
            continue
        row = FORM_ROW.fullmatch(line)
        key = _form_label(row.group(1)) if row else None
        if not key or index + 1 >= len(lines):
            continue
        value = lines[index + 1]
        following = FORM_ROW.fullmatch(value)
        if (following and _form_label(following.group(1))) or SECTIONS.match(value.rstrip(':')):
            continue  # an empty row: never take the next label or heading as the value
        found.setdefault(key, (value, index + 1))
    return found


def _states_span(source, value):
    """True when the resume writes `value` as both ends of a range ("Jan 2019 - Jan 2019", "2019 to 2019")."""
    same = r'\s+'.join(map(re.escape, value.split()))
    return bool(re.search(rf'(?<![\w]){same}\s*(?:-|–|—|to|till|until)\s*{same}(?![\w])', source, re.I))


def _repair_form(out, source):
    """Fill fields the model left empty from labelled rows, copying the row values verbatim."""
    form = {key: value for key, (value, _) in form_fields(source).items()}
    if not form:
        return
    if not out['full_name'] and form.get('full_name') and _looks_like_name(form['full_name']):
        out['full_name'] = form['full_name']
    if not out['email'] and form.get('email') and EMAIL.search(form['email']):
        out['email'] = EMAIL.search(form['email']).group(0)
    if not out['phone'] and form.get('phone') and PHONE.search(form['phone']):
        out['phone'] = PHONE.search(form['phone']).group(0).strip()
    company, position = form.get('company', ''), form.get('position', '')
    start = end = ''
    if form.get('period'):
        parts = re.split(r'\s+(?:-|\u2013|\u2014|to|till)\s+|\s*[\u2013\u2014]\s*|(?<=\d)-(?=\d|[A-Z])', form['period'], maxsplit=1)
        start, end = (parts + [''])[:2]
    if company and position:
        jobs = out['experience']
        match = [job for job in jobs if _norm(job.get('position')) == _norm(position)
                 or _norm(job.get('company')) == _norm(company)]
        if not jobs:
            jobs.append({'company': '', 'position': '', 'start': '', 'end': '', 'location': '', 'bullets': []})
            match = jobs
        elif len(jobs) == 1:
            match = jobs
        for job in match[:1]:
            for key, value in (('company', company), ('position', position), ('start', start), ('end', end)):
                if not job.get(key) and value:
                    job[key] = value.strip()
    degree, school = form.get('degree', ''), form.get('institution', '')
    if degree or school:
        schools = out['education']
        if not schools:
            schools.append({'institution': '', 'degree': '', 'start': '', 'end': '', 'details': ''})
        if len(schools) == 1:
            year = form.get('education_end', '')
            for key, value in (('institution', school), ('degree', degree),
                               ('end', year if re.fullmatch(r'(?:[A-Za-z]{3,9}\.? )?\d{4}', year) else '')):
                if not schools[0].get(key) and value:
                    schools[0][key] = value
    if form.get('skills'):
        known = {skill.casefold() for skill in out['skills']}
        out['skills'] += [skill for skill in _split_list(form['skills']) if skill.casefold() not in known]


def repair(out, source, name_hint=''):
    """Deterministic, verbatim-only fixes for common model slips. Every value set here is copied
    from the source text (or moved between fields) and is never invented."""
    lines = _lines(source)
    head = lines[:6]
    _repair_form(out, source)
    # One stated date is never both ends of a span ("2019 - 2019" for "..., 2019"). The same value counts
    # as a span only when the resume writes it as one; the year appearing elsewhere (in a job's dates) is
    # not evidence of a span for this entry.
    for section, keep in (('education', 'end'), ('experience', 'start')):
        for entry in out[section]:
            stated = (entry.get('start') or '').strip()
            if stated and _norm(stated) == _norm(entry.get('end')) and not _states_span(source, stated):
                entry['start' if keep == 'end' else 'end'] = ''
    # An email cell often carries a note ("asha@example.com (personal address)"): keep the address only.
    if out['email'] and not EMAIL.fullmatch(out['email']):
        match = EMAIL.search(out['email'])
        out['email'] = match.group(0) if match else ''
    # Name: the model sometimes leaves it empty or puts it in the headline.
    if out['full_name'] and out['headline'] and _norm(out['headline']) == _norm(out['full_name']):
        out['headline'] = ''
    if not out['full_name'] and name_hint:
        # A rejected, non-verbatim quote ("Venkata Reddy") only ever selects a verbatim header segment.
        completed = _complete_name(name_hint, head)
        out['full_name'] = completed if completed != name_hint else ''
    if not out['full_name'] and out['headline'] and _looks_like_name(out['headline']) and \
            any(_norm(out['headline']) == _norm(line) for line in head):
        out['full_name'], out['headline'] = out['headline'], ''
    if not out['full_name']:
        out['full_name'] = next((line for line in head if _looks_like_name(line)), '')
    out['full_name'] = _complete_name(out['full_name'], head)
    for entry in out['education']:
        _complete_degree(entry, lines)
    if out['full_name'] and not out['headline']:
        index = next((i for i, line in enumerate(lines) if _norm(line) == _norm(out['full_name'])), None)
        following = lines[index + 1] if index is not None and index + 1 < len(lines) else ''
        if following and len(following.split()) <= 8 and not SECTIONS.match(following) and \
                not re.search(r'[@\d|]', following) and not URL.search(following):
            out['headline'] = following
    # Contact details found verbatim in the text.
    if not out['email']:
        match = EMAIL.search(source)
        out['email'] = match.group(0) if match else ''
    if not out['phone']:
        match = PHONE.search(source)
        out['phone'] = match.group(0).strip() if match else ''
    urls = [url.rstrip('.') for url in URL.findall(source) if '@' not in url]
    for key, host in (('linkedin', 'linkedin.com'), ('github', 'github.com')):
        if not out[key]:
            out[key] = next((url for url in urls if host in url.lower()), '')
    if not out['portfolio']:  # only a link in the header block, never an employer's site
        header_urls = [url.rstrip('.') for line in head for url in URL.findall(line) if '@' not in url]
        out['portfolio'] = next((url for url in header_urls
                                 if not re.search(r'linkedin\.com|github\.com', url, re.I)), '')
    # Skills: one item per skill, no duplicates; fall back to a "Skills" section.
    skills = [part for value in out['skills'] for part in _split_list(value)]
    if not skills:
        for line in _section(source, r'(technical\s+|core\s+|key\s+)?skills'):
            skills += _split_list(re.sub(r'^[^:]{1,40}:\s*', '', line))
    seen, unique = set(), []
    for skill in skills:
        if skill.casefold() not in seen and _norm(skill) in _norm(source):
            seen.add(skill.casefold())
            unique.append(skill)
    out['skills'] = unique
    # The model may return only one of several verbatim project titles. Complete
    # the list from the actual Projects section, without inventing project details.
    # Description/bullet sentences and technical-stack labels are not project titles.
    existing = {_norm(item) for item in out['projects']}
    for item in _section(source, r'(personal\s+)?projects?'):
        if len(out['projects']) >= 30:
            break
        title = _norm(item)
        if (not 2 <= len(item.split()) <= 16 or len(item) > 130
                or re.match(r'^(?:tech(?:nologies)?|stack|tools|role|description|features|impact|responsibilities)\s*:', item, re.I)
                or re.match(r'^(?:built|developed|implemented|created|designed|used|integrated|deployed|achieved|reduced|improved|maintained|worked)\b', item, re.I)):
            continue
        if not any(title in known or known in title for known in existing if known):
            out['projects'].append(item)
            existing.add(title)
    # Current company: the employer of a role that the source says is ongoing.
    if not out['current_company']:
        out['current_company'] = next((entry['company'] for entry in out['experience']
                                       if entry.get('company') and PRESENT.search(entry.get('end') or '')), '')
    return out


EXPERIENCE_FIELDS = ('company', 'position', 'start', 'end', 'location')
EDUCATION_FIELDS = ('institution', 'degree', 'start', 'end', 'details')


def _placeholder(item, fields):
    """An entry in which every field and bullet is empty. Models pad arrays with these; they carry
    no claim, so they are dropped instead of being reported as missing information."""
    def empty(field):
        if isinstance(field, dict):
            field = field.get('value')
        return field is None or (isinstance(field, str) and not field.strip())
    return all(empty(item.get(key)) for key in fields) and all(empty(b) for b in item.get('bullets') or [])


def extract_profile(source, ai, *, for_review=False, on_progress=None):
    """The model must quote source spans for every fact. All claims get substring validation."""
    prompt = '''Return ONLY a JSON object describing this candidate's actual resume, NOT an improved version.
For every individual factual field use {"value":"EXACT verbatim substring from input","evidence":"EXACT surrounding substring that includes value"}.
If missing, use {"value":"","evidence":""}. Do not infer or invent anything. Do not reorder words in a quote.
Field guide:
- full_name: the person's name, normally the first line of the resume. Never put the name in headline.
- headline: the job title written under the name (for example "Software Engineer"), not the name.
- current_company: the employer of a role whose end date is Present/Current, if any.
- portfolio: a personal website URL that is not LinkedIn or GitHub.
- skills: ONE skill per item. Split lists like "Python, SQL, Docker" into separate items.
- projects: one item per project line from a Projects section.
Schema:
{"full_name":field,"headline":field,"email":field,"phone":field,"location":field,
 "linkedin":field,"github":field,"portfolio":field,"current_company":field,"summary":field,
 "skills":[field],
 "experience":[{"company":field,"position":field,"start":field,"end":field,"location":field,"bullets":[field]}],
 "education":[{"institution":field,"degree":field,"start":field,"end":field,"details":field}],
 "certifications":[field],"projects":[field]}
For a multi-line bullet, quote its exact text including words; text must be in the source after whitespace normalization.
DO NOT output empty experience/education entries. Preserve correct source spelling.
INPUT:
''' + source
    if for_review:
        prompt = prompt.replace('INPUT:\n',
            'Use short verbatim quotes. Set evidence equal to value when possible. '
            'Include EVERY bullet of every role (never drop one), and leave uncertain fields blank.\nINPUT:\n',
            1)
        data = generate(ai, prompt, schema=_review_schema(), on_progress=on_progress)
    else:
        data = generate(ai, prompt, on_progress=on_progress)

    def read_field(field, required=False, lenient=False):
        # Only fields that are tied back to their entry afterwards (employer with title, institution with
        # degree) may keep a verbatim value whose quoted context is not verbatim. Dates, locations and the
        # rest keep the strict check: a header address or a lone year must not become a job location or a
        # start date.
        try:
            return _get(field, source, required and not for_review, lenient=for_review and lenient)
        except ExtractionError:
            if not for_review:
                raise
            return ''
    if not isinstance(data,dict):
        raise ExtractionError('AI returned invalid profile')
    out={}
    for key in SCALARS:
        out[key]=read_field(data.get(key,{}),key in ('full_name','email') and key not in REPAIRABLE)
    for key in ('skills','certifications','projects'):
        if not isinstance(data.get(key,[]),list):
            raise ExtractionError(f'Invalid {key}')
        out[key]=[value for v in data.get(key,[]) if (value := read_field(v, True))]
    out['experience']=[]
    for item in data.get('experience',[]):
        if not isinstance(item,dict) or not isinstance(item.get('bullets',[]),list):
            raise ExtractionError('Invalid experience')
        if _placeholder(item, EXPERIENCE_FIELDS):
            continue
        entry={k:read_field(item.get(k,{}),k in ('company','position','start'), k in ('company','position'))
               for k in EXPERIENCE_FIELDS}
        entry['bullets']=[value for v in item.get('bullets',[]) if (value := read_field(v, True))]
        out['experience'].append(entry)
    out['education']=[]
    for item in data.get('education',[]):
        if not isinstance(item,dict):
            raise ExtractionError('Invalid education')
        if _placeholder(item, EDUCATION_FIELDS):
            continue
        out['education'].append({k:read_field(item.get(k,{}),k in ('institution','degree'), k in ('institution','degree'))
                                 for k in EDUCATION_FIELDS})
    hint = (data.get('full_name') or {}).get('value', '') if isinstance(data.get('full_name'), dict) else ''
    repair(out, source, hint if isinstance(hint, str) else '')
    if for_review:
        return out
    for key in ('portfolio', 'current_company'):
        if not out[key]:
            out.pop(key)
    if not out['projects']:
        out.pop('projects')
    try:
        return validate_profile(out)
    except ProfileError as exc:
        raise ExtractionError(f'Source does not provide complete reliable profile: {exc}') from exc


def load_profile(path, ai=None):
    path=Path(path)
    if path.suffix.lower()=='.json':
        return validate_profile(json.loads(path.read_text(encoding='utf8')))
    if ai is None:
        raise ExtractionError('Ollama required for unstructured resume sources')
    return extract_profile(read_resume_text(path),ai)

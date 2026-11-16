"""Extract source text and build source-evidenced fields; fail closed on unsupported/unclear input."""
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


def _get(field, source, required=False):
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
    if _norm(value) not in _norm(source) or _norm(evidence) not in _norm(source):
        raise ExtractionError(f'Unverified extracted value: {value[:60]}')
    if _norm(value) not in _norm(evidence):
        raise ExtractionError(f'Evidence does not support value: {value[:60]}')
    return value


def _review_schema():
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties),
                'additionalProperties': False}

    def array(items):
        return {'type': 'array', 'items': items}

    evidence = obj({key: {'type': 'string'} for key in ('value', 'evidence')})
    fields = {key: evidence for key in
              ('full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github', 'summary')}
    fields.update(skills=array(evidence), certifications=array(evidence))
    fields['experience'] = array(obj({
        **dict.fromkeys(('company', 'position', 'start', 'end', 'location'), evidence),
        'bullets': array(evidence),
    }))
    fields['education'] = array(obj(dict.fromkeys(
        ('institution', 'degree', 'start', 'end', 'details'), evidence)))
    return obj(fields)


def extract_profile(source, ai, *, for_review=False):
    """The model must quote source spans for every fact. All claims get substring validation."""
    prompt = '''Return ONLY a JSON object describing this candidate's actual resume, NOT an improved version.
For every individual factual field use {"value":"EXACT verbatim substring from input","evidence":"EXACT surrounding substring that includes value"}.
If missing, use {"value":"","evidence":""}. Do not infer or invent anything. Do not reorder words in a quote.
Schema:
{"full_name":field,"headline":field,"email":field,"phone":field,"location":field,
 "linkedin":field,"github":field,"summary":field,
 "skills":[field],
 "experience":[{"company":field,"position":field,"start":field,"end":field,"location":field,"bullets":[field]}],
 "education":[{"institution":field,"degree":field,"start":field,"end":field,"details":field}],
 "certifications":[field]}
For a multi-line bullet, quote its exact text including words; text must be in the source after whitespace normalization.
DO NOT output empty experience/education entries. Preserve correct source spelling.
INPUT:
''' + source
    if for_review:
        prompt = prompt.replace('INPUT:\n',
            'Use short verbatim quotes. Set evidence equal to value when possible. '
            'Include at most three short bullets per role, and leave uncertain fields blank.\nINPUT:\n',
            1)
        data = ai.generate(prompt, schema=_review_schema())
    else:
        data = ai.generate(prompt)

    def read_field(field, required=False):
        try:
            return _get(field, source, required and not for_review)
        except ExtractionError:
            if not for_review:
                raise
            return ''
    if not isinstance(data,dict):
        raise ExtractionError('AI returned invalid profile')
    out={}
    for key in ('full_name','headline','email','phone','location','linkedin','github','summary'):
        out[key]=read_field(data.get(key,{}),key in ('full_name','email'))
    for key in ('skills','certifications'):
        if not isinstance(data.get(key,[]),list):
            raise ExtractionError(f'Invalid {key}')
        out[key]=[value for v in data.get(key,[]) if (value := read_field(v, True))]
    out['experience']=[]
    for item in data.get('experience',[]):
        if not isinstance(item,dict) or not isinstance(item.get('bullets',[]),list):
            raise ExtractionError('Invalid experience')
        entry={k:read_field(item.get(k,{}),k in ('company','position','start'))
               for k in ('company','position','start','end','location')}
        entry['bullets']=[value for v in item.get('bullets',[]) if (value := read_field(v, True))]
        out['experience'].append(entry)
    out['education']=[]
    for item in data.get('education',[]):
        if not isinstance(item,dict):
            raise ExtractionError('Invalid education')
        out['education'].append({k:read_field(item.get(k,{}),k in ('institution','degree'))
                                 for k in ('institution','degree','start','end','details')})
    if for_review:
        out['skills'] = list(dict.fromkeys(out['skills']))
        return out
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

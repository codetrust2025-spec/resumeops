"""Prefill for the public candidate form: values the candidate then checks, edits and submits as their own.

Two sources, neither of which is resume content by itself:
* ``quick_prefill(text)``: read at once without the GPU, so it works for a crowd. Contact details, labelled form
  rows ("Full Name", "Company Name", "Employment Dates"), employer/date tables and catalog technologies found in the
  document. Only text the document states is used.
* ``profile_to_form(profile)``: the AI extraction of the same document (the persistent import queue), mapped to the
  form's fields when it is ready.
"""
import re
import tempfile
from pathlib import Path

from .ingest import EMAIL, PHONE, URL, _lines, _looks_like_name, form_fields, read_resume_text, repair

PREFILL_EXTENSIONS = ('pdf', 'docx')
PREFILL_LIMIT = 6 * 1024 * 1024


def document_text(extension, raw):
    """The text of an uploaded PDF or DOCX (ValueError when it cannot be read)."""
    from .ingest import ExtractionError

    if extension not in PREFILL_EXTENSIONS:
        raise ValueError('Upload a PDF or DOCX file.')
    if not 0 < len(raw) <= PREFILL_LIMIT:
        raise ValueError('Upload a PDF or DOCX file of up to 6 MB.')
    with tempfile.TemporaryDirectory(prefix='resumeops-prefill-') as folder:
        path = Path(folder) / ('upload.' + extension)
        path.write_bytes(raw)
        try:
            return read_resume_text(path, minimum_characters=40)
        except ExtractionError as error:
            raise ValueError(str(error)) from error


def _blank():
    return {'full_name': '', 'headline': '', 'email': '', 'phone': '', 'location': '', 'linkedin': '', 'github': '',
            'portfolio': '', 'current_company': '', 'summary': '', 'skills': [], 'certifications': [], 'projects': [],
            'experience': [], 'education': []}


def quick_prefill(text):
    """Form values stated in the document text (empty strings and lists for anything it does not state)."""
    from .generate import catalog_terms, named_tools
    from .workflow import matched_terms

    lines = _lines(text)
    labelled = {key: value for key, (value, _) in form_fields(text).items()}
    email = EMAIL.search(labelled.get('email', '')) or EMAIL.search(text)
    phone = PHONE.search(labelled.get('phone', '')) or PHONE.search(text)
    name = labelled.get('full_name', '')
    if not name:
        name = next((line for line in lines[:6] if _looks_like_name(line)), '')
    links = [match.group(0).rstrip('.') for match in URL.finditer(text)]
    linkedin = next((link for link in links if 'linkedin.com' in link.lower()), '')
    github = next((link for link in links if 'github.com' in link.lower()), '')
    profile = _blank()
    profile['full_name'] = name
    repair(profile, text)  # labelled employment/education rows and employer/date tables, verbatim
    vocabulary = sorted(set(named_tools()) | set(catalog_terms()), key=len, reverse=True)
    stated_skills = labelled.get('skills', '')
    found = [term for term in vocabulary if matched_terms([term], stated_skills or text)]
    skills = []
    for term in found:  # "Spring Boot" covers "Spring"; keep the longest names
        if not any(term != kept and term.casefold() in kept.casefold() for kept in skills):
            skills.append(term)
    form = profile_to_form({**profile, 'skills': skills[:25]})
    form.update(full_name=name.strip(), email=email.group(0) if email else '',
                phone=' '.join(phone.group(0).split()) if phone else '', linkedin=linkedin, github=github)
    return form


def _year(value):
    found = re.findall(r'(?:19|20)\d{2}', value or '')
    return found[-1] if found else ''


def profile_to_form(profile):
    """The candidate form's fields from an extracted profile (resume_bot.profile shape)."""
    from .generate import split_project

    form = {key: str(profile.get(key) or '') for key in ('full_name', 'email', 'phone', 'location', 'linkedin',
                                                          'github', 'portfolio', 'current_company')}
    form['skills'] = ', '.join(profile.get('skills') or [])
    form['certifications'] = '\n'.join(profile.get('certifications') or [])
    form['achievements'] = '\n'.join(profile.get('achievements') or [])
    form['work_experience'] = [
        {'company': job.get('company', ''), 'position': job.get('position', ''), 'start': job.get('start', ''),
         'end': job.get('end', ''), 'location': job.get('location', ''),
         'responsibilities': '\n'.join(job.get('bullets') or [])}
        for job in profile.get('experience') or [] if job.get('company') or job.get('position')]
    form['education'] = [
        {'university': entry.get('institution', ''), 'degree': entry.get('degree', ''), 'specialization': '',
         'graduation_year': _year(entry.get('end', '')), 'cgpa': ''}
        for entry in profile.get('education') or [] if entry.get('institution') or entry.get('degree')]
    projects = []
    for project in profile.get('projects') or []:
        name, stack, details = split_project(project)
        projects.append({'name': name, 'stack': ', '.join(stack), 'description': '\n'.join(details)})
    form['projects'] = projects
    return form

"""Candidate profile validation. Only user-supplied or source-evidenced data is admissible."""
import re
from datetime import date


class ProfileError(ValueError):
    pass


def _string(value, field, required=False):
    if not isinstance(value, str):
        raise ProfileError(f'{field}: expected string')
    text = value.strip()
    if required and (not text or text.lower() in ('n/a', 'unknown', 'tbd', 'your name')):
        raise ProfileError(f'{field}: missing required information')
    if len(text) > 8000:
        raise ProfileError(f'{field}: too long')
    return text


def _month(value):
    """Validate chronology only when a date is unambiguously expressed."""
    import datetime
    if not value or value.lower() in ('present', 'current', 'now', 'ongoing'):
        return None
    for fmt in ('%b %Y', '%B %Y', '%Y-%m', '%Y', '%d %b %Y', '%d %B %Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def validate_profile(data):
    if not isinstance(data, dict):
        raise ProfileError('Profile must be a JSON object')
    accepted = {'full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github',
                'summary', 'skills', 'experience', 'education', 'certifications', 'projects', 'portfolio', 'current_company', 'achievements', 'coding_platforms', 'competencies',
                'skill_groups'}
    extra = set(data) - accepted
    if extra:
        raise ProfileError('Unknown fields: ' + ', '.join(sorted(extra)))
    out = {key: _string(data.get(key, ''), key, key == 'full_name')
           for key in ('full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github', 'summary')}
    if out['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', out['email']):
        raise ProfileError('Invalid email address')
    phone_digits = re.sub(r'\D', '', out['phone'])
    if not 7 <= len(phone_digits) <= 15:
        raise ProfileError('A valid phone number with 7 to 15 digits is required')
    for k in ('skills', 'experience', 'education', 'certifications'):
        if not isinstance(data.get(k, []), list):
            raise ProfileError(f'{k}: expected array')
    out['skills'] = [_string(x, 'skills[]', True) for x in data.get('skills', [])]
    if len(out['skills']) != len(set(s.casefold() for s in out['skills'])):
        raise ProfileError('Duplicate skills')
    out['experience'] = []
    for i, item in enumerate(data.get('experience', [])):
        if not isinstance(item, dict):
            raise ProfileError(f'experience[{i}]: expected object')
        if set(item) - {'company','position','start','end','location','bullets'}:
            raise ProfileError(f'experience[{i}]: unknown fields')
        entry = {k: _string(item.get(k,''), f'experience[{i}].{k}', k in ('company','position','start'))
                 for k in ('company', 'position','start','end','location')}
        if not isinstance(item.get('bullets',[]), list):
            raise ProfileError(f'experience[{i}].bullets: expected array')
        entry['bullets'] = [_string(v, f'experience[{i}].bullets[]', True) for v in item.get('bullets', [])]
        s, e = _month(entry['start']), _month(entry['end'])
        if s and e and e < s:
            raise ProfileError(f'experience[{i}]: end before start')
        out['experience'].append(entry)
    out['education'] = []
    for i, item in enumerate(data.get('education', [])):
        if not isinstance(item, dict):
            raise ProfileError(f'education[{i}]: expected object')
        if set(item) - {'institution','degree','start','end','details'}:
            raise ProfileError(f'education[{i}]: unknown fields')
        entry = {k: _string(item.get(k,''), f'education[{i}].{k}', k in ('institution','degree'))
                 for k in ('institution', 'degree','start','end','details')}
        out['education'].append(entry)
    out['certifications'] = [_string(v, 'certifications[]',True) for v in data.get('certifications', [])]
    for field in ('portfolio', 'current_company'):
        if field in data:
            value = _string(data[field], field)
            if value:
                out[field] = value
    if 'projects' in data:
        projects = data['projects']
        if not isinstance(projects, list) or len(projects) > 30:
            raise ProfileError('Projects: expected up to 30 entries')
        if projects:
            out['projects'] = [_string(item, 'projects[]', True) for item in projects]
    for field in ('achievements', 'coding_platforms', 'competencies'):
        if field in data:
            items = data[field]
            if not isinstance(items, list) or len(items) > 40:
                raise ProfileError(f'{field}: expected up to 40 entries')
            if items:
                out[field] = [_string(item, field, True) for item in items]
    if 'skill_groups' in data:
        # Presentation only: the candidate's own skills, each in exactly one named group.
        groups = data['skill_groups']
        if not isinstance(groups, list) or len(groups) > 12 or any(
                not isinstance(g, dict) or set(g) != {'name', 'items'} or not isinstance(g['items'], list) for g in groups):
            raise ProfileError('skill_groups: expected up to 12 {name, items} groups')
        out['skill_groups'] = [{'name': _string(g['name'], 'skill_groups[].name', True),
                                'items': [_string(item, 'skill_groups[].items', True) for item in g['items']]}
                               for g in groups]
        grouped = [item for g in out['skill_groups'] for item in g['items']]
        if sorted(grouped) != sorted(out['skills']):
            raise ProfileError('skill_groups must contain every listed skill exactly once')
    if not (out['experience'] or out['education']):
        raise ProfileError('At least one experience or education record is required')
    return out

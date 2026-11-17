"""Role-targeted AI tailoring that can only rephrase verified candidate facts.

Ollama rewrites the summary and every experience bullet for the selected role. Each rewrite is
accepted only if a deterministic grounding check proves it adds nothing the candidate did not
state: no new numbers, no new technologies or names, no new claims of leadership or outcomes,
and no lost hard facts. Anything that fails keeps the candidate's original text.
"""
import copy
import json
import re

# Neutral wording the model may add. Deliberately excludes leadership, ownership, seniority and
# outcome verbs (led, managed, owned, architected, improved, increased, reduced, optimised, ...),
# which would change what the candidate claims.
NEUTRAL = set('''
a an the and or of to for in on at by with within from into onto across through via as using used use
including include includes while where which that this these those their its it is are was were be been
also both each every other such per than then so closely
built build building developed develop developing created create creating implemented implement
implementing wrote write writing produced prepared applied apply performed handled completed delivered
provided supported support supporting contributed contribute collaborated collaborate collaborating
worked work working partnered assisted maintained maintain maintaining updated documented tested
reviewed conducted experience experienced professional skilled background focus focused hands-on
year years
'''.split())

# Verbs whose introduction would inflate the claim; they are rejected even if they appear elsewhere
# in the profile, unless the same item already used them.
INFLATING = set('''
led lead leading managed manage managing owned own owning spearheaded headed directed architected
mentored supervised oversaw drove driven increased increase reduced reduce improved improve
optimized optimised optimize accelerated boosted grew saved transformed pioneered launched
expert senior principal lead
'''.split())

IRREGULAR = {'built': 'build', 'wrote': 'write', 'written': 'write', 'led': 'lead', 'ran': 'run',
             'made': 'make', 'drove': 'drive', 'driven': 'drive', 'grew': 'grow', 'grown': 'grow',
             'taught': 'teach', 'began': 'begin', 'begun': 'begin', 'did': 'do', 'done': 'do'}
SUFFIXES = ('ations', 'ation', 'ments', 'ment', 'ings', 'ing', 'ions', 'ion', 'ies', 'ers', 'er',
            'ed', 'es', 'ly', 's')
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:\.[A-Za-z0-9]+)*")
NUMBER = re.compile(r'\d+(?:[.,]\d+)?%?')


def stem(word):
    word = word.lower().strip('.')
    if word in IRREGULAR:
        return IRREGULAR[word]
    for suffix in SUFFIXES:
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[:-len(suffix)].rstrip('e') or word
    return word.rstrip('e') or word


def words(text):
    return [match.group(0) for match in WORD.finditer(re.sub(r"[\'\u2019]s\b", '', text or ''))]


def numbers(text):
    return {value.rstrip('%').replace(',', '') for value in NUMBER.findall(text or '')}


def is_term(word, initial=False):
    """Technology, product or proper-noun-like token: mixed case, digits or symbols (C++, Node.js,
    S3, AWS, APIs), or a capitalised word that does not start a sentence (React, Kafka)."""
    if re.search(r'[A-Z]', word[1:]) or re.search(r'[0-9+#.]', word):
        return True
    return word[:1].isupper() and not initial


def tokens(text):
    """(word, is_term) for every word; sentence-initial capitals are ordinary words."""
    out, initial = [], True
    text = re.sub(r"[\'\u2019]s\b", '', text or '')
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+#]*(?:\.[A-Za-z0-9]+)*|[.!?;:]", text or ''):
        piece = match.group(0)
        if piece in '.!?;:':
            initial = True
            continue
        out.append((piece, is_term(piece, initial)))
        initial = False
    return out


def safe_edit(original, suggestion, allowed_technologies=None):
    """Whitespace / final-period edits only (kept for formatting-only callers)."""
    if not isinstance(suggestion, str):
        return False
    before = ' '.join(original.split())
    after = ' '.join(suggestion.split())
    return before == after or (bool(before) and before[-1].isalnum() and after == before + '.')


def profile_text(profile):
    parts = [profile.get('full_name', ''), profile.get('headline', ''), profile.get('summary', ''),
             profile.get('current_company', ''), *profile.get('skills', []),
             *profile.get('certifications', []), *profile.get('projects', [])]
    for entry in profile.get('experience', []):
        parts += [entry['company'], entry['position'], entry.get('location', ''), *entry['bullets']]
    for entry in profile.get('education', []):
        parts += [entry['institution'], entry['degree'], entry.get('details', '')]
    return '\n'.join(part for part in parts if part)


def grounded(original, rewrite, context, extra_numbers=()):
    """Return '' when ``rewrite`` only rephrases ``original`` using words from ``context``
    (the verified source text it may draw on) plus neutral wording; else the reason it fails."""
    if not isinstance(rewrite, str) or not rewrite.strip():
        return 'empty rewrite'
    rewrite = ' '.join(rewrite.split())
    if safe_edit(original, rewrite):
        return ''
    if len(rewrite) > max(int(len(original) * 1.8), len(original) + 80):
        return 'rewrite much longer than the original'
    new_numbers = numbers(rewrite) - numbers(original) - {str(n) for n in extra_numbers}
    if new_numbers:
        return 'adds numbers not in the source: ' + ', '.join(sorted(new_numbers))
    lost = numbers(original) - numbers(rewrite)
    if lost:
        return 'drops numbers from the source: ' + ', '.join(sorted(lost))
    own = {stem(w) for w in words(original)}
    allowed = own | {stem(w) for w in words(context)}
    neutral = {stem(w) for w in NEUTRAL}
    for word, term in tokens(rewrite):
        key = stem(word)
        if word.lower() in INFLATING and key not in own:
            return f'adds a stronger claim than the source: "{word}"'
        if term and key not in allowed:
            return f'adds a term not in the candidate profile: "{word}"'
        if key not in allowed and key not in neutral:
            return f'adds wording not supported by the source: "{word}"'
    source_terms = {stem(w) for w, term in tokens(original) if term}
    missing = source_terms - {stem(w) for w in words(rewrite)}
    if missing:
        return 'drops source facts: ' + ', '.join(sorted(missing))
    return ''


def _items(profile):
    """(path, original, context) for every rewritable string."""
    full = profile_text(profile)
    items = [(('summary',), profile['summary'], full)]
    for i, entry in enumerate(profile['experience']):
        own_entry = '\n'.join([entry['company'], entry['position'], *entry['bullets']])
        for j, bullet in enumerate(entry['bullets']):
            items.append((('experience', i, j), bullet, own_entry))
    return items


def role_terms(profile, keywords):
    """Role keywords the candidate's own profile already contains (case-insensitive)."""
    text = ' ' + ' '.join(profile_text(profile).split()).casefold() + ' '
    return [term for term in (keywords or []) if re.search(
        r'(?<!\w)' + re.escape(' '.join(term.split()).casefold()) + r'(?!\w)', text)]


SUMMARY_RULE = ('- String 1 is the professional summary: 1-3 sentences aimed at the target role, built only from the\n'
                "  candidate's own words in the array.")


def build_prompt(source, role=None, keywords=None, years=None, has_summary=True):
    lines = ['You are an expert resume editor. Rewrite every string in the input array so it reads '
             'stronger and clearer for the target role, while saying exactly the same facts.']
    if role:
        lines.append(f'Target role: {role}.')
    if keywords:
        lines.append('Role keywords the candidate already has: ' + ', '.join(keywords) +
                     '. Use one only in a string that already mentions it, or in the summary.')
    if years is not None and has_summary:
        lines.append(f'Verified total experience: {years} years. You may state "{years} years" in the '
                     'first string (the summary) only.')
    lines.append(f'''How to rewrite:
{SUMMARY_RULE if has_summary else '- There is no summary in this array.'}
- {'Every other' if has_summary else 'Every'} string is a work bullet: start with a strong past-tense verb that matches what was done
  (Built, Developed, Implemented, Integrated, Delivered, Fixed, Collaborated, Maintained, Documented,
  Tested, Reviewed), put the technology and purpose first, and remove filler such as "worked on",
  "was responsible for", "helped with".
- Never add tools, technologies, numbers, results, team size, leadership or responsibilities that the
  same string does not state. Keep every technology, number and name from the string.
Examples:
"Worked on fixing login page bugs using Angular." -> "Fixed login page bugs in Angular."
"Was responsible for writing unit tests for the payments service." -> "Wrote unit tests for the payments service."
"Helped the team with migrating reports to SQL Server." -> "Collaborated on migrating reports to SQL Server."
Return exactly {len(source)} string{'s' if len(source) != 1 else ''}: one rewrite per input string, in the same order. Never add or remove strings.
Return one JSON object with exactly one key, "edits", containing the array of rewritten strings.
INPUT ARRAY:''')
    return '\n'.join(lines) + '\n' + json.dumps(source, ensure_ascii=False)


def polish(profile, ai, on_progress=None, *, role=None, keywords=None, years=None):
    """Tailor ``profile`` with ``ai``. ``on_progress(fraction, metrics)`` receives received output
    characters over the input size (a measured ratio; capped below 1 until the stream ends).

    Never fails the resume because of what the model returns: only non-empty lines are sent, and
    a reply with the wrong shape (for example an extra, invented line) is discarded as a whole,
    keeping every original line. The candidate's verified text is always a valid result."""
    items = [item for item in _items(profile) if item[1].strip()]
    base = {'role': role, 'method': 'grounded-rewrite-v2', 'changes': [], 'rejections': [],
            'rejected_indices': [], 'changed': 0, 'rejected': 0, 'accepted': len(items)}
    if not items:
        if on_progress is not None:
            on_progress(1.0, {'phase': 'done', 'chars': 0, 'chunks': 0, 'elapsed': 0.0})
        return copy.deepcopy(profile), {**base, 'skipped': 'No summary or work bullets to tailor; '
                                                           'the resume uses the verified facts as entered.'}
    source = [original for _, original, _ in items]
    has_summary = items[0][0] == ('summary',)
    prompt = build_prompt(source, role, role_terms(profile, keywords), years, has_summary)
    expected = len(json.dumps({'edits': source}, ensure_ascii=False))
    callback = None
    if on_progress is not None:
        def callback(metrics):
            if metrics.get('phase') == 'done':
                on_progress(1.0, metrics)
            else:
                on_progress(min(0.95, metrics['chars'] / expected) if expected else 0.0, metrics)
    from .ollama import generate
    result = generate(ai, prompt, on_progress=callback)
    suggestions = result.get('edits') if isinstance(result, dict) else None
    if not isinstance(suggestions, list) or len(suggestions) != len(source):
        got = len(suggestions) if isinstance(suggestions, list) else 'no list'
        reason = f'model returned {got} lines for {len(source)}; reply discarded, originals kept'
        return copy.deepcopy(profile), {**base, 'accepted': 0, 'rejected': len(source),
                                        'rejected_indices': list(range(len(source))),
                                        'rejections': [{'index': None, 'reason': reason}],
                                        'model_output_rejected': reason}
    out = copy.deepcopy(profile)
    rejected, changes, reasons = [], [], []
    for index, ((path, before, context), after) in enumerate(zip(items, suggestions)):
        extra = (years,) if path == ('summary',) and years is not None else ()
        reason = grounded(before, after, context, extra)
        if reason:
            value = before
            rejected.append(index)
            reasons.append({'index': index, 'reason': reason})
        else:
            value = ' '.join(after.split())
            if value != ' '.join(before.split()):
                changes.append({'index': index, 'before': before, 'after': value})
        if path == ('summary',):
            out['summary'] = value
        else:
            out['experience'][path[1]]['bullets'][path[2]] = value
    return out, {**base, 'accepted': len(source) - len(rejected), 'rejected': len(rejected),
                 'rejected_indices': rejected, 'changed': len(changes), 'changes': changes,
                 'rejections': reasons}


def verify_tailored(original, tailored, years=None):
    """Re-check a saved tailored profile against the verified input. Raises ValueError."""
    restored = copy.deepcopy(tailored)
    for (path, before, context) in _items(original):
        after = tailored['summary'] if path == ('summary',) else \
            tailored['experience'][path[1]]['bullets'][path[2]] if path[1] < len(tailored['experience']) and \
            path[2] < len(tailored['experience'][path[1]]['bullets']) else None
        if after is None:
            raise ValueError('Tailored profile changed the number of bullets')
        extra = (years,) if path == ('summary',) and years is not None else ()
        if before and after != before and grounded(before, after, context, extra):
            raise ValueError('Tailored text is not grounded in the verified input')
        if path == ('summary',):
            restored['summary'] = before
        else:
            restored['experience'][path[1]]['bullets'][path[2]] = before
    if restored != original:
        raise ValueError('Tailored profile changed verified facts')

"""Allow only claim-preserving whitespace and terminal-period edits."""
import copy
import json


def safe_edit(original, suggestion, allowed_technologies=None):
    if not isinstance(suggestion, str):
        return False
    before = ' '.join(original.split())
    after = ' '.join(suggestion.split())
    return before == after or (
        bool(before) and before[-1].isalnum() and after == before + '.'
    )


def polish(profile, ai):
    source = [profile['summary']] + [b for e in profile['experience'] for b in e['bullets']]
    prompt = '''Return one JSON object with exactly one key, "edits", containing an array
of strings. Preserve the input array order. For each string, copy its text exactly,
except for normalizing whitespace or adding a final period. Keep empty strings empty.
INPUT ARRAY:
''' + json.dumps(source, ensure_ascii=False)
    result = ai.generate(prompt)
    suggestions = result.get('edits')
    if not isinstance(suggestions, list) or len(suggestions) != len(source):
        raise ValueError('Ollama did not return the same number of edit suggestions')
    out = copy.deepcopy(profile)
    rejected = []
    values = []
    for index, (before, after) in enumerate(zip(source, suggestions)):
        if safe_edit(before, after):
            values.append(' '.join(after.split()))
        else:
            values.append(before)
            rejected.append(index)
    values = iter(values)
    out['summary'] = next(values)
    for entry in out['experience']:
        entry['bullets'] = [next(values) for _ in entry['bullets']]
    return out, {'accepted': len(source) - len(rejected), 'rejected': len(rejected),
                 'rejected_indices': rejected}

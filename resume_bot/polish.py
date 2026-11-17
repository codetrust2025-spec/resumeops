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


def polish(profile, ai, on_progress=None):
    """``on_progress(fraction, metrics)`` receives received/expected output characters. The model
    must return the input strings unchanged (bar whitespace/periods), so the expected output size
    is known from the input and the fraction is a real measurement."""
    source = [profile['summary']] + [b for e in profile['experience'] for b in e['bullets']]
    prompt = '''Return one JSON object with exactly one key, "edits", containing an array
of strings. Preserve the input array order. For each string, copy its text exactly,
except for normalizing whitespace or adding a final period. Keep empty strings empty.
INPUT ARRAY:
''' + json.dumps(source, ensure_ascii=False)
    expected = len(json.dumps({'edits': source}, ensure_ascii=False))
    callback = None
    if on_progress is not None:
        def callback(metrics):
            on_progress(min(1.0, metrics['chars'] / expected) if expected else 1.0, metrics)
    from .ollama import generate
    result = generate(ai, prompt, on_progress=callback)
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

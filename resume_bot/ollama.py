"""Ollama's documented /api/generate endpoint, JSON-only responses."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


class OllamaError(RuntimeError):
    pass


class _TopLevelJsonFields:
    """Count complete top-level JSON members in streamed output.

    Only top-level member boundaries count: commas inside quoted strings, arrays and
    nested experience objects do not increment progress. This measures emitted JSON,
    not validation or remaining generation time.
    """
    def __init__(self):
        self.depth = 0
        self.quoted = False
        self.escape = False
        self.started = False
        self.saw_member = False
        self.count = 0

    def feed(self, text):
        for char in text:
            if self.quoted:
                if self.escape:
                    self.escape = False
                elif char == '\\\\':
                    self.escape = True
                elif char == '"':
                    self.quoted = False
                continue
            if not self.started:
                if char == '{':
                    self.started = True
                    self.depth = 1
                continue
            if char == '"':
                self.quoted = True
                if self.depth == 1:
                    self.saw_member = True
            elif char in '{[':
                self.depth += 1
            elif char in '}]':
                self.depth -= 1
                if self.depth == 0:
                    if self.saw_member:
                        self.count += 1
                    self.started = False
            elif char == ',' and self.depth == 1:
                self.count += 1
            elif self.depth == 1 and not char.isspace():
                self.saw_member = True
        return self.count


class Ollama:
    def __init__(self, model=None, url=None, timeout=240):
        self.model = model or os.environ.get('OLLAMA_MODEL','qwen2.5:7b')
        self.url = (url or os.environ.get('OLLAMA_URL','http://127.0.0.1:11434')).rstrip('/')
        self.timeout = timeout

    def generate(self, prompt, schema=None, on_progress=None):
        if on_progress is None:
            return self._generate(prompt, schema, None)
        try:
            return self._generate(prompt, schema, on_progress)
        except OllamaError as exc:
            if 'unreachable' in str(exc) and 'refused' in str(exc).lower():
                raise  # the service is down; a second request cannot help
            # The streamed request failed part-way. Run it once without streaming (the request
            # format used before live progress) and say so instead of inventing progress.
            on_progress({'phase': 'fallback', 'chars': 0, 'chunks': 0, 'elapsed': 0.0,
                         'reason': str(exc)[:200]})
            return self._generate(prompt, schema, None)

    def _generate(self, prompt, schema=None, on_progress=None):
        """Return the model's JSON object.

        With ``on_progress`` the request streams, and the callback receives measured metrics as
        output arrives: ``{'phase': 'waiting'|'generating'|'done', 'chars': int, 'chunks': int,
        'elapsed': float}`` plus Ollama's final counters (eval_count, eval_duration,
        prompt_eval_count, load_duration) when the stream ends. Nothing is estimated here.
        """
        streaming = on_progress is not None
        payload = {
            'model':self.model,
            'prompt':prompt,
            'format':schema if schema is not None else 'json',
            'stream':streaming,
            'options':{'temperature':0},
        }
        req = Request(self.url + '/api/generate',
                      data=json.dumps(payload).encode('utf8'),
                      headers={'Content-Type':'application/json'},method='POST')
        started = time.monotonic()
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                if not streaming:
                    text = json.load(resp)['response']
                else:
                    on_progress({'phase': 'waiting', 'chars': 0, 'chunks': 0, 'elapsed': 0.0})
                    parts, chunks, final = [], 0, {}
                    emitted, fields = 0, _TopLevelJsonFields()
                    for line in resp:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if item.get('error'):
                            raise OllamaError('Ollama reported an error during generation')
                        piece = item.get('response', '')
                        if piece:
                            parts.append(piece)
                            chunks += 1
                            emitted += len(piece)
                            fields.feed(piece)
                        if item.get('done'):
                            final = {key: item[key] for key in (
                                'eval_count', 'eval_duration', 'prompt_eval_count', 'load_duration',
                                'total_duration') if key in item}
                            break
                        on_progress({'phase': 'generating', 'chars': emitted,
                                     'chunks': chunks, 'fields_emitted': fields.count,
                                     'elapsed': time.monotonic() - started})
                    else:
                        raise OllamaError('Ollama stream ended before completion')
                    text = ''.join(parts)
                    on_progress({'phase': 'done', 'chars': len(text), 'chunks': chunks,
                                 'fields_emitted': fields.count,
                                 'elapsed': time.monotonic() - started, **final})
        except OllamaError:
            raise
        except (URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as exc:
            raise OllamaError(f'Ollama unreachable or invalid response: {exc}') from exc
        try:
            result = json.loads(text)
        except (TypeError,ValueError) as exc:
            raise OllamaError('Model did not return valid JSON') from exc
        if not isinstance(result, dict):
            raise OllamaError('Model JSON must be an object')
        return result

    def runtime(self):
        """What Ollama reports for this model right now (/api/ps): bytes in VRAM vs total.
        Returns None when the model is not loaded or Ollama cannot be reached."""
        try:
            with urlopen(self.url + '/api/ps', timeout=3) as resp:
                models = json.load(resp).get('models', [])
        except (URLError, TimeoutError, OSError, ValueError):
            return None
        for item in models:
            if item.get('name') in (self.model, self.model + ':latest') or item.get('model') == self.model:
                size, vram = int(item.get('size') or 0), int(item.get('size_vram') or 0)
                return {'model': item.get('name'), 'size': size, 'size_vram': vram,
                        'gpu_percent': round(100 * vram / size) if size else 0,
                        'context_length': item.get('context_length')}
        return None


def generate(ai, prompt, schema=None, on_progress=None):
    """Call ``ai.generate`` with streaming metrics when the model client supports them."""
    import inspect
    try:
        accepts = 'on_progress' in inspect.signature(ai.generate).parameters
    except (TypeError, ValueError):
        accepts = False
    if on_progress is not None and accepts:
        return ai.generate(prompt, schema=schema, on_progress=on_progress) if schema is not None else \
            ai.generate(prompt, on_progress=on_progress)
    return ai.generate(prompt, schema=schema) if schema is not None else ai.generate(prompt)

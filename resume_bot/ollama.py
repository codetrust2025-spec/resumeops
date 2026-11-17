"""Ollama's documented /api/generate endpoint, JSON-only responses."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, model=None, url=None, timeout=240):
        self.model = model or os.environ.get('OLLAMA_MODEL','qwen2.5:7b')
        self.url = (url or os.environ.get('OLLAMA_URL','http://127.0.0.1:11434')).rstrip('/')
        self.timeout = timeout

    def generate(self, prompt, schema=None, on_progress=None):
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
                        if item.get('done'):
                            final = {key: item[key] for key in (
                                'eval_count', 'eval_duration', 'prompt_eval_count', 'load_duration',
                                'total_duration') if key in item}
                            break
                        on_progress({'phase': 'generating', 'chars': sum(map(len, parts)),
                                     'chunks': chunks, 'elapsed': time.monotonic() - started})
                    else:
                        raise OllamaError('Ollama stream ended before completion')
                    text = ''.join(parts)
                    on_progress({'phase': 'done', 'chars': len(text), 'chunks': chunks,
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

"""Ollama's documented /api/generate endpoint, JSON-only responses."""
import json
import os
from urllib.request import Request, urlopen
from urllib.error import URLError


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, model=None, url=None, timeout=240):
        self.model = model or os.environ.get('OLLAMA_MODEL','qwen2.5:7b')
        self.url = (url or os.environ.get('OLLAMA_URL','http://127.0.0.1:11434')).rstrip('/')
        self.timeout = timeout

    def generate(self, prompt, schema=None):
        payload = {
            'model':self.model,
            'prompt':prompt,
            'format':schema if schema is not None else 'json',
            'stream':False,
            'options':{'temperature':0},
        }
        req = Request(self.url + '/api/generate',
                      data=json.dumps(payload).encode('utf8'),
                      headers={'Content-Type':'application/json'},method='POST')
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except (URLError,TimeoutError,OSError,ValueError) as exc:
            raise OllamaError(f'Ollama unreachable or invalid response: {exc}') from exc
        try:
            result = json.loads(body['response'])
        except (KeyError,TypeError,ValueError) as exc:
            raise OllamaError('Model did not return valid JSON') from exc
        if not isinstance(result, dict):
            raise OllamaError('Model JSON must be an object')
        return result

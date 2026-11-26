"""Workspace-wide Ollama preference shared by the dashboard and watcher."""
import json
import os
from pathlib import Path
from urllib.request import urlopen

from .ollama import OllamaError
from .storage import write_json


class ModelSettings:
    def __init__(self, root, default, url=None):
        self.path = Path(root) / '.resumeops-ollama.json'
        self.default = default
        self.url = (url or os.getenv('OLLAMA_URL') or 'http://127.0.0.1:11434').rstrip('/')

    def selected(self):
        try:
            stored = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        model = stored.get('model') if isinstance(stored, dict) else None
        return model if isinstance(model, str) and model else None

    def current(self):
        return self.selected() or self.default

    def installed(self):
        try:
            with urlopen(self.url + '/api/tags', timeout=3) as response:
                models = json.load(response)['models']
            if not isinstance(models, list):
                raise ValueError('Expected a model list.')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OllamaError('Could not read installed models from Ollama.') from exc
        names = set()
        for item in models:
            if not isinstance(item, dict) or item.get('remote_host') or item.get('remote_model'):
                continue
            name = item.get('name') or item.get('model')
            if isinstance(name, str) and name.strip() == name and 0 < len(name) <= 512 and not name.endswith(':cloud'):
                names.add(name)
        return sorted(names, key=lambda name: (name.casefold(), name))

    def update(self, model):
        if model is not None and (not isinstance(model, str) or not model or len(model) > 512):
            raise ValueError('Choose an installed Ollama model.')
        if model is not None and model not in self.installed():
            raise ValueError('That model is not installed on this Ollama server.')
        write_json(self.path, {'model': model})

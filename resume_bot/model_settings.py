"""Workspace-wide Ollama preference shared by the dashboard and watcher."""
import json
import os
from pathlib import Path
from urllib.request import urlopen

from .ollama import Ollama, OllamaUnavailableError
from .storage import write_json


class ModelSettings:
    def __init__(self, root, default=None, url=None):
        self.path = Path(root) / '.resumeops-ollama.json'
        self.default = default or os.getenv('OLLAMA_MODEL') or 'qwen2.5:7b'
        self.url = (url or os.getenv('OLLAMA_URL') or 'http://127.0.0.1:11434').rstrip('/')

    def selected(self):
        try:
            stored = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise OllamaUnavailableError('Cannot read the saved model preference. Re-select a model or restore the service default.') from exc
        if isinstance(stored, dict) and 'model' in stored:
            model = stored['model']
            if model is None or isinstance(model, str) and 0 < len(model) <= 512 and model.strip() == model:
                return model
        raise OllamaUnavailableError('Saved model preference is invalid. Re-select a model or restore the service default.')

    def current(self):
        return self.selected() or self.default

    def client(self, model=None):
        return Ollama(model if model is not None else self.current(), self.url)

    def installed(self):
        return self.inventory('/api/tags')

    def loaded(self):
        return self.inventory('/api/ps')

    def inventory(self, endpoint):
        try:
            with urlopen(self.url + endpoint, timeout=3) as response:
                models = json.load(response)['models']
            if not isinstance(models, list):
                raise ValueError('Expected a model list.')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OllamaUnavailableError('Could not read the Ollama model inventory.') from exc
        records = {}
        for item in models:
            if not isinstance(item, dict) or item.get('remote_host') or item.get('remote_model'):
                continue
            name = item.get('name') or item.get('model')
            if isinstance(name, str) and name.strip() == name and 0 < len(name) <= 512 and not name.endswith(':cloud'):
                digest = item.get('digest')
                record = {'name': name, 'digest': digest if isinstance(digest, str) else None}
                for field in ('size', 'size_vram') if endpoint == '/api/ps' else ('size',):
                    value = item.get(field)
                    record[field] = value if type(value) is int and value >= 0 else None
                records.setdefault(name, record)
        return sorted(records.values(), key=lambda record: (record['name'].casefold(), record['name']))

    def update(self, model):
        if model is not None and (not isinstance(model, str) or not model or len(model) > 512):
            raise ValueError('Choose an installed Ollama model.')
        if model is not None and not any(item['name'] == model for item in self.installed()):
            raise ValueError('That model is not installed on this Ollama server.')
        write_json(self.path, {'model': model})

"""Probe two installed models on the RTX host, using a disposable model preference."""
import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resume_bot.model_settings import ModelSettings  # noqa: E402


def verify_switching(root, models, url=None):
    if len(set(models)) != 2:
        raise ValueError('Choose two distinct installed model tags.')
    settings = ModelSettings(root, models[0], url)
    inventory = {item['name']: item for item in settings.installed()}
    if any(name not in inventory or not inventory[name]['digest'] for name in models):
        raise ValueError('Both models must be installed locally and report a digest.')

    evidence = []
    for name in models:
        settings.update(name)
        reopened = ModelSettings(root, settings.default, settings.url)
        client = reopened.client()
        if client.model != name:
            raise RuntimeError(f'Model selection did not persist for {name}.')
        if client.generate('Return the JSON object {"resumeops_model_probe": true}.') != {'resumeops_model_probe': True}:
            raise RuntimeError(f'{name} did not return the expected synthetic response.')
        loaded = next((item for item in reopened.loaded() if item['name'] == name), None)
        digest = inventory[name]['digest']
        if (not loaded or not loaded['size_vram'] or not loaded['digest'] or
                loaded['digest'].removeprefix('sha256:') != digest.removeprefix('sha256:')):
            raise RuntimeError(f'Ollama did not report matching GPU-backed execution for {name}.')
        evidence.append({'model': name, 'digest': digest, 'installed_bytes': inventory[name]['size'],
                         'vram_bytes': loaded['size_vram'], 'persisted': True})
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('models', nargs=2, metavar='INSTALLED_TAG')
    parser.add_argument('--ollama-url')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='resumeops-model-probe-') as root:
        print(json.dumps(verify_switching(root, args.models, args.ollama_url), indent=2))


if __name__ == '__main__':
    main()

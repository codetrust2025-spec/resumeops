"""Run on the RTX host with a verified candidate JSON and an authorized browser."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from resume_bot.ingest import load_profile
from resume_bot.polish import polish
from resume_bot.ollama import Ollama
from resume_bot.overleaf import _editor_url
from resume_bot.pipeline import process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--model', default='qwen2.5:7b')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--browser-profile', default='.overleaf-browser-profile')
    parser.add_argument('--output', default='output')
    args = parser.parse_args()
    if args.input.suffix.lower() != '.json' or args.input.resolve() == (
        Path(__file__).resolve().parent / 'examples/sample_candidate.json'
    ):
        parser.error('Provide an owner-verified candidate JSON, not the included sample')
    source_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
    sample = Path(__file__).resolve().parent / 'examples/sample_candidate.json'
    if sample.exists() and source_hash == hashlib.sha256(sample.read_bytes()).hexdigest():
        parser.error('The bundled sample is unsuitable for live candidate acceptance')
    ai = Ollama(args.model, args.ollama_url)
    try:
        with urlopen(ai.url + '/api/tags', timeout=5) as response:
            installed = json.load(response).get('models', [])
    except (URLError, OSError, ValueError) as exc:
        raise SystemExit(f'Live Ollama model inventory unavailable: {exc}') from exc
    if not any(args.model in (item.get('name'), item.get('model')) for item in installed):
        raise SystemExit('Requested Ollama model is unavailable')
    if ai.generate('Return exactly this JSON object: {"resume_builder_ready": true}').get(
        'resume_builder_ready'
    ) is not True:
        raise SystemExit('Live Ollama inference probe returned unexpected JSON')

    _, edit_audit = polish(load_profile(args.input), ai)
    result = process(args.input, args.output, ai=ai, overleaf=True,
                     browser_profile=args.browser_profile, refresh_overleaf=True)
    overleaf = result.get('overleaf', {})
    pdf = Path(result['output']) / 'overleaf.pdf'
    if (result.get('model') != ai.model or result.get('source_sha256') != source_hash or
            not _editor_url(overleaf.get('url', '')) or not pdf.is_file() or
            hashlib.sha256(pdf.read_bytes()).hexdigest() != overleaf.get('pdf_sha256') or
            not overleaf.get('pdf_checks', {}).get('required_facts_present')):
        raise SystemExit('Live acceptance lacked valid source, model, editor, or downloaded PDF evidence')
    print(json.dumps({'status': 'LIVE ACCEPTANCE PASSED', 'model': args.model,
                      'ollama_inference_probe': True, 'candidate_edit_audit': edit_audit, 'source_sha256': source_hash,
                      'local_pdf_checks': result['pdf_checks'], 'overleaf': overleaf}, indent=2))


if __name__ == '__main__':
    main()

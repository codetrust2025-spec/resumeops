"""Read candidate evidence, including scans, into bounded text for fact extraction."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .ingest import ExtractionError, read_resume_text

MAX_PAGES = 20
MAX_TEXT = 75000


def image_text(path):
    tesseract = shutil.which('tesseract')
    if tesseract:
        command = [tesseract, str(path), 'stdout', '-l', 'eng']
    elif os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned', '-File',
                   str(Path(__file__).with_name('ocr_windows.ps1')), '-ImagePath', str(path)]
    else:
        raise ExtractionError('Scan reading is unavailable. Upload a text-based document or enter the facts manually.')
    try:
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExtractionError('Scan reading failed or timed out. Try a clearer scan or enter the facts manually.') from error
    if result.returncode:
        raise ExtractionError('Cannot read this scan. Try a clearer JPG or PNG, or enter the facts manually.')
    return result.stdout.strip()


def scan_pdf(path):
    try:
        info = subprocess.run(['pdfinfo', str(path)], capture_output=True, text=True, timeout=15)
        pages = next(int(line.split(':', 1)[1]) for line in info.stdout.splitlines() if line.startswith('Pages:'))
        if info.returncode or not 1 <= pages <= MAX_PAGES:
            raise ValueError('page limit')
        with tempfile.TemporaryDirectory(prefix='resumeops-ocr-') as folder:
            result = subprocess.run(['pdftoppm', '-r', '150', '-scale-to', '2400', '-png',
                                     str(path), str(Path(folder) / 'page')], capture_output=True, timeout=120)
            if result.returncode:
                raise ValueError('rendering failed')
            images = sorted(Path(folder).glob('page-*.png'))
            if len(images) != pages:
                raise ValueError('incomplete rendering')
            texts = []
            for image in images:
                texts.append(image_text(image))
                if sum(map(len, texts)) > MAX_TEXT:
                    raise ExtractionError('Document text exceeds 75000 characters. Submit fewer pages.')
            return '\n'.join(texts)
    except (OSError, subprocess.TimeoutExpired, ValueError, StopIteration) as error:
        if isinstance(error, ExtractionError):
            raise
        raise ExtractionError('Cannot read this scanned PDF. Use up to 20 pages or upload clearer JPG/PNG scans.') from error


def read_evidence(path):
    path = Path(path)
    extension = path.suffix.lower()
    if extension in ('.jpg', '.jpeg', '.png'):
        text = image_text(path)
    elif extension == '.pdf':
        try:
            text = read_resume_text(path, minimum_characters=0)
            # Poppler separates pages with form feeds. Empty pages can contain image-only evidence.
            pages = text.split('\f')
            if any(not page.strip() for page in pages[:-1]):
                text = scan_pdf(path)
        except ExtractionError as error:
            if 'No readable text' not in str(error):
                raise
            text = scan_pdf(path)
    else:
        text = read_resume_text(path, minimum_characters=1)
    if not text.strip():
        raise ExtractionError('No readable text found. Upload a clearer document or enter the facts manually.')
    if len(text) > MAX_TEXT:
        raise ExtractionError('Document text exceeds 75000 characters. Submit fewer pages.')
    return text

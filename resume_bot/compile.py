"""Compile and verify PDF without shell escape or automatic model-generated code execution."""
import re
import subprocess
from pathlib import Path


class BuildError(RuntimeError):
    pass


def compile_latex(folder, timeout=90):
    folder=Path(folder)
    try:
        cmd=subprocess.run(['pdflatex','-no-shell-escape','-halt-on-error','-interaction=nonstopmode',
                            '-output-directory',str(folder),str(folder/'main.tex')],
                           stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout)
    except FileNotFoundError as exc:
        raise BuildError('pdflatex not installed; install TeX Live with pdfLaTeX and lmodern') from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError('LaTeX compilation timed out') from exc
    pdf=folder/'main.pdf'
    if cmd.returncode != 0 or not pdf.exists():
        raise BuildError('LaTeX compile failed: ' + cmd.stdout[-1800:])
    return pdf


def verify_pdf(pdf, profile, max_pages=2):
    """Verify PDF is searchable and includes key facts; enforce two-page length."""
    pdf=Path(pdf)
    try:
        info=subprocess.run(['pdfinfo',str(pdf)],capture_output=True,text=True,timeout=15,check=True)
        text=subprocess.run(['pdftotext',str(pdf),'-'],capture_output=True,encoding='utf-8',timeout=15,check=True)
    except (FileNotFoundError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
        raise BuildError('PDF text/page verification tooling unavailable or failed') from exc
    match=re.search(r'^Pages:\s*(\d+)',info.stdout,re.M)
    pages=int(match.group(1)) if match else 0
    if not 1 <= pages <= max_pages:
        raise BuildError(f'Generated resume has {pages} pages (allowed 1-{max_pages})')
    observed=' '.join(text.stdout.split()).casefold()
    facts=[profile['full_name'],profile['email']]
    facts += [e['company'] for e in profile['experience']]
    facts += [e['position'] for e in profile['experience']]
    facts += [e['institution'] for e in profile['education']]
    missing=[s for s in facts if ' '.join(s.split()).casefold() not in observed]
    if missing:
        raise BuildError('PDF missing verified text: ' + ', '.join(missing))
    return {'pages':pages,'searchable':True,'required_facts_present':True,'pdf_bytes':pdf.stat().st_size}


def page_lines(pdf, page):
    """The non-empty text lines on one page of a PDF (how much a resume spills onto its last page)."""
    try:
        text = subprocess.run(['pdftotext', '-f', str(page), '-l', str(page), str(pdf), '-'], capture_output=True,
                              encoding='utf-8', timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise BuildError('Cannot read the PDF page text') from exc
    return sum(1 for line in text.splitlines() if line.strip())


def verify_remote_pdf(remote, local, profile, max_pages):
    checks = verify_pdf(remote, profile, max_pages)
    local_checks = verify_pdf(local, profile, max_pages)
    if checks['pages'] != local_checks['pages']:
        raise BuildError('Overleaf PDF page count differs from the locally verified resume')
    try:
        texts = [subprocess.run(['pdftotext', str(pdf), '-'], capture_output=True,
                                encoding='utf-8', timeout=15, check=True).stdout
                 for pdf in (remote, local)]
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise BuildError('Cannot compare local and Overleaf PDF text') from exc
    if texts[0].split() != texts[1].split():
        raise BuildError('Overleaf PDF text differs from the locally verified resume')
    return checks

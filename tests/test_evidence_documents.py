"""Candidate evidence can be a short document with incomplete resume facts."""
import sys
import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
from zipfile import ZipFile

from resume_bot.ingest import ExtractionError, extract_profile, read_resume_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from resume_dashboard import Workspace
from automation_jobs import AutomationJobs, submission
from resume_bot.automation import CONSENT_VERSION, NeedsInput, source_profile
from resume_bot.document_evidence import read_evidence


class EvidenceDocumentTests(unittest.TestCase):
    def bundle(self):
        return {'consent': CONSENT_VERSION, 'role': 'Python Developer', 'documents': [
            {'extension': 'txt', 'document': base64.b64encode(text.encode()).decode()}
            for text in ['Full name: Sample Candidate\nPhone: +91 9000000000',
                         'Example College awarded a Diploma in Computing in 2024.']]}

    def test_bundle_validation_rejects_mixed_modes_and_preserves_legacy_jobs(self):
        payload = self.bundle()
        self.assertEqual(len(submission(payload)['documents']), 2)
        for fields in ({'candidate': {}}, {'document': 'eA==', 'extension': 'txt'},
                       {'documents': []}, {'documents': payload['documents'] * 6}):
            with self.assertRaises(ValueError):
                submission({**payload, **fields})
        legacy = {key: value for key, value in payload.items() if key != 'documents'}
        legacy.update(payload['documents'][0])
        self.assertEqual(submission(legacy)['extension'], 'txt')

    def test_combined_byte_limit_and_file_types_are_checked_on_server(self):
        payload = self.bundle()
        small = {'extension': 'txt', 'document': base64.b64encode(b'123456').decode()}
        with patch('automation_jobs.DOCUMENT_LIMIT', 10):
            with self.assertRaisesRegex(ValueError, 'total'):
                submission({**payload, 'documents': [small, small]})
        for item in ({**small, 'extension': 'exe'}, {**small, 'document': 'invalid!'},
                     {**small, 'document': ''}):
            with self.assertRaises(ValueError):
                submission({**payload, 'documents': [item]})

    def test_bundle_checkpoint_is_owner_scoped_and_retries_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = AutomationJobs(Workspace(Path(folder)))
            with patch.object(queue, 'start'):
                identifier = queue.enqueue('a' * 64, self.bundle())
                self.assertEqual(queue.enqueue('a' * 64, self.bundle()), identifier)
                queue.enqueue('b' * 64, self.bundle())
            with closing(queue.connection()) as db:
                row = db.execute('SELECT * FROM automation WHERE owner=? AND id=?', ('a' * 64, identifier)).fetchone()
            gap = NeedsInput({}, [{'id': 'source_text', 'field': 'source_text', 'label': 'Additional candidate facts'}])
            with patch('resume_bot.automation.source_profile', side_effect=gap) as extract:
                queue._prepare(row)
            self.assertIn('Sample Candidate', extract.call_args.args[0])
            self.assertIn('Diploma in Computing', extract.call_args.args[0])
            self.assertTrue(extract.call_args.kwargs['evidence_documents'])
            with closing(queue.connection()) as db:
                rows = db.execute('SELECT owner,status,checkpoints FROM automation ORDER BY owner').fetchall()
            self.assertEqual([row['status'] for row in rows], ['needs_input', 'queued'])
            self.assertIn('Candidate evidence document 2', json.loads(rows[0]['checkpoints'])['source'])
            with patch.object(queue, 'start'):
                queue.answer('a' * 64, identifier, {'source_text': 'Completed a Diploma in Computing at Example College in 2024.'})
            with closing(queue.connection()) as db:
                saved = db.execute('SELECT status,checkpoints FROM automation WHERE owner=?', ('a' * 64,)).fetchone()
            self.assertEqual(saved['status'], 'queued')
            self.assertIn('Candidate evidence document 2', json.loads(saved['checkpoints'])['source'])
            self.assertIn('Candidate-provided facts', json.loads(saved['checkpoints'])['source'])

    def test_unreadable_image_returns_a_clear_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.png'
            path.write_bytes(b'not an image')
            with self.assertRaises(ExtractionError):
                read_evidence(path)

    @unittest.skipUnless(os.name == 'nt', 'Windows OCR integration')
    def test_real_windows_ocr_reads_png_and_jpg_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            # Generate invented evidence using Windows drawing tools, then use the real OCR engine.
            script = '''Add-Type -AssemblyName System.Drawing
$b=[Drawing.Bitmap]::new(1400,200)
$g=[Drawing.Graphics]::FromImage($b)
$g.Clear([Drawing.Color]::White)
$f=[Drawing.Font]::new('Arial',32)
$g.DrawString('Sample Candidate Python Certificate',$f,[Drawing.Brushes]::Black,20,50)
$b.Save((Join-Path $env:RESUMEOPS_OCR_TEST_DIR 'scan.png'),[Drawing.Imaging.ImageFormat]::Png)
$b.Save((Join-Path $env:RESUMEOPS_OCR_TEST_DIR 'scan.jpg'),[Drawing.Imaging.ImageFormat]::Jpeg)
$g.Dispose();$f.Dispose();$b.Dispose()'''
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], check=True,
                           capture_output=True, env={**os.environ, 'RESUMEOPS_OCR_TEST_DIR': folder})
            for extension in ('png', 'jpg'):
                self.assertIn('Sample Candidate', read_evidence(Path(folder) / ('scan.' + extension)))
            latex = r'''\documentclass{article}
\usepackage{graphicx}
\begin{document}\thispagestyle{empty}
\includegraphics[width=\textwidth]{scan.png}
\end{document}'''
            (Path(folder) / 'scan.tex').write_text(latex)
            subprocess.run(['pdflatex', '-no-shell-escape', '-halt-on-error', '-interaction=nonstopmode',
                            'scan.tex'], cwd=folder, capture_output=True, check=True, timeout=60)
            self.assertIn('Sample Candidate', read_evidence(Path(folder) / 'scan.pdf'))

    def test_short_certificate_is_read_by_automation(self):
        text = 'Certificate awarded to Sample Candidate for completing Python Basics.'
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(Path(folder))
            self.assertEqual(workspace.read_automation_source('txt', text.encode()), text)
            path = Path(folder) / 'short.txt'
            path.write_text(text)
            with self.assertRaises(ExtractionError):
                read_resume_text(path)

    def test_short_education_docx_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'education.docx'
            with ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml',
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>Sample Candidate, Diploma in Computing, Example College, 2024'
                    '</w:t></w:r></w:p></w:body></w:document>')
            text = Workspace(Path(folder)).read_automation_source('docx', path.read_bytes())
            self.assertIn('Diploma in Computing', text)

    def test_empty_documents_still_require_readable_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ExtractionError, 'No readable text'):
                Workspace(Path(folder)).read_automation_source('txt', b'  \n')

    def test_extraction_requests_document_facts_and_preserves_incomplete_profile(self):
        def generate(model, prompt, **kwargs):
            self.assertIn('experience letter', prompt)
            self.assertIn('A certificate issuer is not an employer', prompt)
            return {'full_name': {'value': 'Sample Candidate', 'evidence': 'Sample Candidate'},
                    'certifications': [{'value': 'Python Basics', 'evidence': 'Python Basics'}]}
        with patch('resume_bot.ingest.generate', side_effect=generate):
            result = extract_profile('Sample Candidate\nCertificate: Python Basics', object(), for_review=True)
        self.assertEqual(result['full_name'], 'Sample Candidate')
        self.assertEqual(result['experience'], [])
        self.assertEqual(result['education'], [])
        self.assertEqual(result['certifications'], ['Python Basics'])

    def test_certificate_without_resume_sections_is_preserved_while_facts_are_requested(self):
        draft = {'full_name': 'Sample Candidate', 'phone': '', 'email': '', 'experience': [],
                 'education': [], 'skills': [], 'certifications': ['Python Basics'], 'projects': []}
        with patch('resume_bot.automation.extract_profile', return_value=draft):
            with self.assertRaises(NeedsInput) as caught:
                source_profile('Sample Candidate\nCertificate awarded for Python Basics', object(),
                               evidence_documents=True)
        self.assertEqual(caught.exception.draft['certifications'], ['Python Basics'])
        self.assertEqual(caught.exception.draft['experience'], [])
        self.assertEqual(caught.exception.questions[0]['field'], 'source_text')

    def test_planned_credential_is_not_promoted_to_an_earned_certificate(self):
        draft = {'full_name': 'Sample Candidate', 'phone': '', 'email': '', 'experience': [],
                 'education': [], 'skills': [], 'certifications': ['Cloud Basics'], 'projects': []}
        with patch('resume_bot.automation.extract_profile', return_value=draft):
            with self.assertRaises(NeedsInput) as caught:
                source_profile('Sample Candidate\nPlanned certification: Cloud Basics', object(), evidence_documents=True)
        self.assertEqual(caught.exception.draft['certifications'], [])

    def test_missing_scanned_credential_becomes_a_confirmation_before_publication(self):
        from resume_bot.automation import apply_answers
        draft = {'full_name': 'Sample Candidate', 'phone': '+91 9000000000', 'email': '',
                 'experience': [], 'education': [{'institution': 'Example College', 'degree': 'Diploma',
                                                  'start': '', 'end': '2024', 'details': ''}],
                 'skills': ['Python'], 'certifications': [], 'projects': []}
        source = ('Sample Candidate\nEducation\nDiploma, Example College, 2024\nSkills\nPython\n'
                  'Certificate of Completion\nSample Candidate\nPython Fundamentals')
        with patch('resume_bot.automation.extract_profile', return_value=draft):
            with self.assertRaises(NeedsInput) as caught:
                source_profile(source, object(), evidence_documents=True)
        self.assertEqual([q['field'] for q in caught.exception.questions], ['certifications'])
        confirmed = apply_answers(caught.exception.draft, caught.exception.questions,
                                  {'certifications': 'Python Fundamentals'})
        self.assertEqual(confirmed['certifications'], ['Python Fundamentals'])

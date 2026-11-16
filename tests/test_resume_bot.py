import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot.profile import ProfileError, validate_profile
from resume_bot.latex import escape, render
from resume_bot.ingest import extract_profile, ExtractionError, read_resume_text
from resume_bot.polish import safe_edit, polish
from resume_bot.pipeline import process, scan
from resume_bot.compile import verify_pdf, BuildError
from resume_bot.ollama import Ollama, OllamaError

ROOT=Path(__file__).parents[1]
SAMPLE=json.loads((ROOT/'examples/sample_candidate.json').read_text())


class FakeEditor:
    model='mock-ollama-test-only'
    def generate(self, prompt, schema=None):
        import re
        match=re.search(r'INPUT ARRAY:\s*(\[.*\])',prompt,re.S)
        self.asserted = match is not None
        return {'edits':json.loads(match.group(1))}


class BadEditor(FakeEditor):
    def generate(self, prompt, schema=None):
        values=super().generate(prompt)['edits']
        values[1]=values[1] + ' Increased revenue by 99%. Deployed Kubernetes.'
        return {'edits':values}


class FakeExtractor:
    def generate(self, prompt, schema=None):
        def f(value): return {'value':value,'evidence':value}
        return {
            'full_name':f('Asha Example'),'headline':f('Frontend Engineer'),
            'email':f('asha.example@example.com'),'phone':f(''),
            'location':f('Hyderabad'),'linkedin':f(''),'github':f(''),
            'summary':f(''),
            'skills':[f('React'),f('JavaScript')],
            'experience':[{'company':f('Demo Labs'),'position':f('Software Engineer'),
                'start':f('Jun 2023'),'end':f('Present'),'location':f('Hyderabad'),
                'bullets':[f('Built React components for the website.')]}],
            'education':[{'institution':f('Example Institute'),'degree':f('B.Tech'),
                'start':f('2018'),'end':f('2022'),'details':f('')}],
            'certifications':[]
        }


SOURCE='''Asha Example
Frontend Engineer
asha.example@example.com
Hyderabad
Skills: React, JavaScript
Experience
Software Engineer at Demo Labs
Jun 2023 - Present
Built React components for the website.
Education
B.Tech from Example Institute
2018 - 2022
'''


class ProfileTests(unittest.TestCase):
    def test_sample_valid(self):
        self.assertEqual(validate_profile(SAMPLE)['full_name'],'Asha Example')
    def test_invented_extra_fields_rejected(self):
        p=dict(SAMPLE);p['made_up_experience']='10 years'
        with self.assertRaises(ProfileError):validate_profile(p)
    def test_experience_bad_dates(self):
        p=json.loads(json.dumps(SAMPLE));p['experience'][0]['end']='May 2022'
        with self.assertRaises(ProfileError):validate_profile(p)
    def test_duplicate_skills(self):
        p=json.loads(json.dumps(SAMPLE));p['skills'].append('react')
        with self.assertRaises(ProfileError):validate_profile(p)
    def test_latex_escape_injection(self):
        self.assertEqual(escape(r'100% & $x #_{} \\'),r'100\% \& \$x \#\_\{\} \textbackslash{}\textbackslash{}')
        p=json.loads(json.dumps(SAMPLE));p['summary']=r'\input{evil.tex} & 100%'
        tex=render(p)
        self.assertNotIn(r'\input{evil.tex}',tex)
        self.assertIn(r'\textbackslash{}input\{evil.tex\}',tex)


class ExtractionTests(unittest.TestCase):
    def test_evidence_extraction(self):
        result=extract_profile(SOURCE,FakeExtractor())
        self.assertEqual(result['experience'][0]['company'],'Demo Labs')
    def test_unverifiable_claim_rejected(self):
        class Liar(FakeExtractor):
            def generate(self, prompt, schema=None):
                p=super().generate(prompt,schema)
                p['experience'][0]['company']={'value':'Fictional BigTech','evidence':'Fictional BigTech'}
                return p
        with self.assertRaises(ExtractionError):extract_profile(SOURCE,Liar())
    def test_no_text_scanned_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'blank.txt';p.write_text('hello')
            with self.assertRaises(ExtractionError):read_resume_text(p)
    def test_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'resume.png';p.write_bytes(b'png')
            with self.assertRaises(ExtractionError):read_resume_text(p)


class PolishTests(unittest.TestCase):
    def test_no_added_metrics(self):
        self.assertFalse(safe_edit('Built a web application.','Built a web application and increased sales by 90%.',set()))
    def test_no_added_unverified_tech(self):
        self.assertFalse(safe_edit('Built reusable UI components.','Built reusable UI components with Kubernetes.',set()))
    def test_rejects_changed_claim_without_numbers_or_known_technologies(self):
        self.assertFalse(safe_edit('Supported daily releases.', 'Led daily releases.'))
        self.assertTrue(safe_edit('Supported daily releases', 'Supported daily releases.'))

    def test_unsafe_ai_text_falls_back(self):
        result,audit=polish(validate_profile(SAMPLE),BadEditor())
        self.assertEqual(audit['rejected'],1)
        self.assertEqual(result['experience'][0]['bullets'][0],SAMPLE['experience'][0]['bullets'][0])
    def test_rewrite_no_changes_accepted(self):
        result,audit=polish(validate_profile(SAMPLE),FakeEditor())
        self.assertEqual(audit['rejected'],0)
        self.assertEqual(result['summary'],SAMPLE['summary'])


@unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdfinfo') and shutil.which('pdftotext'), 'TeX/PDF tools missing')
class IntegrationTests(unittest.TestCase):
    def test_equivalent_json_reuses_project_title_and_changed_facts_create_revision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'candidate.json'
            source.write_text(json.dumps(SAMPLE), encoding='utf-8')
            first = process(source, root / 'output', polish_enabled=False)
            source.write_text(json.dumps(SAMPLE, indent=2), encoding='utf-8')
            equivalent = process(source, root / 'output', polish_enabled=False)
            self.assertEqual(first['project_name'], equivalent['project_name'])
            edited = dict(SAMPLE, headline='Frontend Engineer and Team Lead')
            source.write_text(json.dumps(edited), encoding='utf-8')
            revised = process(source, root / 'output', polish_enabled=False)
            self.assertNotEqual(first['project_name'], revised['project_name'])
            self.assertTrue((Path(first['output']) / 'resume.pdf').is_file())
            self.assertTrue((Path(revised['output']) / 'resume.pdf').is_file())

    def test_watcher_retries_transient_failure_after_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / 'inbox'
            inbox.mkdir()
            source = inbox / 'candidate.json'
            shutil.copyfile(ROOT / 'examples/sample_candidate.json', source)
            args = dict(once=True, ai=FakeEditor())
            with patch('resume_bot.pipeline.time.time', return_value=1000), patch(
                'resume_bot.pipeline.process', side_effect=OllamaError('model offline')
            ) as failed:
                state = scan(inbox, root / 'output', **args)
                self.assertEqual(state[source.name]['failures'], 1)
                scan(inbox, root / 'output', **args)
                failed.assert_called_once()
            with patch('resume_bot.pipeline.time.time', return_value=1121):
                state = scan(inbox, root / 'output', **args)
            self.assertEqual(state[source.name]['status'], 'ok')
            self.assertEqual(json.loads((inbox / '.resume-bot-health.json').read_text())['failures'], 0)

    def test_entire_ai_path_compiles_searchable_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            result=process(ROOT/'examples/sample_candidate.json',td,ai=FakeEditor(),polish_enabled=True)
            self.assertEqual(result['status'],'built')
            self.assertEqual(result['pdf_checks']['pages'],1)
            path=Path(result['output'])
            self.assertTrue((path/'resume.pdf').exists())
            self.assertTrue((path/'main.tex').exists())
            self.assertTrue((path/'overleaf-project.zip').exists())
            self.assertEqual(result,process(ROOT/'examples/sample_candidate.json',td,ai=FakeEditor(),polish_enabled=True))
            import zipfile
            with zipfile.ZipFile(path/'overleaf-project.zip') as z:
                self.assertEqual(z.namelist(),['main.tex'])
    def test_auto_watch_once_processes_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            inbox=Path(td)/'inbox';inbox.mkdir()
            (inbox/'new-candidate.json').write_text(json.dumps(SAMPLE))
            state=scan(inbox,Path(td)/'output',ai=FakeEditor(),once=True)
            self.assertEqual(state['new-candidate.json']['status'],'ok')
            self.assertEqual(scan(inbox,Path(td)/'output',ai=FakeEditor(),once=True),state)
    def test_auto_watch_stops_on_bad_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            inbox=Path(td)/'inbox';inbox.mkdir()
            (inbox/'bad.json').write_text('{}')
            state=scan(inbox,Path(td)/'output',ai=FakeEditor(),once=True)
            self.assertEqual(state['bad.json']['status'],'failed')


if __name__=='__main__': unittest.main()

"""Facts v4: form-style (Word table) resumes keep every stated fact, answers never dead-end, summaries name no
employer and claim no unsupported quality, and jobs stranded by an older extraction are read again.

Every candidate here is fictional."""
import base64
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from resume_bot.automation import (CONSENT_VERSION, EXTRACTION_VERSION, AnswerError, NeedsInput, apply_answers,
                                   source_profile)
from resume_bot.generate import composed_summary, same_track, summary_problems, write_summary
from resume_bot.ingest import form_fields, read_resume_text

ROOT = Path(__file__).resolve().parents[1]
INTRO = ['Candidate Profile Summary for Recruiters', 'Prepared from the candidate form submission',
         'Personal details are listed below']
ROWS = [('1. Full Name', 'Kiran Example'),
        ('2. Phone Number', '+91 90000 00000 (Mobile)'),
        ('3. Email ID', 'kiran.example@example.com (personal address)'),
        ('4. Total Years of Experience', '3 years 4 months (as of November 2026)'),
        ('5. Company Name', 'Example Works Solutions Pvt Ltd'),
        ('6. Job Designation', 'Frontend Developer'),
        ('7. Employment Period', 'July 2023 – Present'),
        ('8. Highest Qualification', 'B.Tech in Information Technology'),
        ('9. College Name', 'Example Institute of Technology, Pune'),
        ('10. Education Passed Out Year', '2022'),
        ('11. Primary Technical Skills', 'HTML, CSS, JavaScript (ES6+), TypeScript, React.js, Redux, REST APIs, Git'),
        ('12. Target Job Role and Technology', 'React Frontend Developer')]
OUTRO = ['Project information',
         'No named projects are listed in this profile; the work is described by the role above.',
         'The candidate is applying for frontend roles that use React.js.']
SKILLS = ['HTML', 'CSS', 'JavaScript (ES6+)', 'TypeScript', 'React.js', 'Redux', 'REST APIs', 'Git']


def form_docx(path, rows=ROWS):
    """A two-column Word table between paragraphs, the layout that lost facts in production."""
    paragraph = lambda text: f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    cell = lambda text: f'<w:tc>{paragraph(text)}</w:tc>'
    table = '<w:tbl>' + ''.join(f'<w:tr>{cell(label)}{cell(value)}</w:tr>' for label, value in rows) + '</w:tbl>'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml',
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                         + ''.join(map(paragraph, INTRO)) + table + ''.join(map(paragraph, OUTRO))
                         + '</w:body></w:document>')
    return path


def quote(value, evidence=None):
    return {'value': value, 'evidence': value if evidence is None else evidence}


class JoinedCellModel:
    """Answers as qwen2.5 did on the production DOCX: correct verbatim values whose quoted evidence joins
    the label and value cells ("Company Name: ..."), which is not verbatim text. `blank` empties fields."""
    model = 'fake'

    def __init__(self, blank=(), email='kiran.example@example.com (personal address)', end='Present'):
        self.blank, self.email, self.end = set(blank), email, end

    def field(self, key, value, label):
        return quote('', '') if key in self.blank else quote(value, f'{label}: {value}')

    def generate(self, prompt, schema=None, on_progress=None):
        empty = quote('')
        return {'full_name': quote('Kiran Example'), 'headline': empty, 'email': quote(self.email),
                'phone': quote('+91 90000 00000'), 'location': empty, 'linkedin': empty, 'github': empty,
                'portfolio': empty, 'current_company': empty, 'summary': empty,
                'skills': [] if 'skills' in self.blank else [quote(s) for s in SKILLS],
                'certifications': [], 'projects': [],
                'experience': [{'company': self.field('company', 'Example Works Solutions Pvt Ltd', 'Company Name'),
                                'position': quote('Frontend Developer'), 'start': quote('July 2023'),
                                'end': quote(self.end), 'location': empty, 'bullets': []}],
                'education': [{'institution': self.field('institution', 'Example Institute of Technology, Pune',
                                                         'College Name'),
                               'degree': self.field('degree', 'B.Tech in Information Technology', 'Highest Qualification'),
                               'start': quote('2018', 'Education start 2018'), 'end': quote('2022'),
                               'details': empty}]}


class FormResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = read_resume_text(form_docx(Path(self.temp.name) / 'resume.docx'))

    def test_docx_table_text_keeps_every_cell(self):
        for label, value in ROWS:
            self.assertIn(label, self.source)
            self.assertIn(value, self.source)
        fields = form_fields(self.source)
        self.assertEqual(fields['company'][0], 'Example Works Solutions Pvt Ltd')
        self.assertEqual(fields['institution'][0], 'Example Institute of Technology, Pune')
        self.assertEqual(fields['degree'][0], 'B.Tech in Information Technology')
        self.assertEqual(fields['position'][0], 'Frontend Developer', 'a target role is never read as a past job title')

    def assert_complete(self, profile):
        job, school = profile['experience'][0], profile['education'][0]
        self.assertEqual((job['company'], job['position'], job['start'], job['end']),
                         ('Example Works Solutions Pvt Ltd', 'Frontend Developer', 'July 2023', 'Present'))
        self.assertEqual((school['institution'], school['degree'], school['end']),
                         ('Example Institute of Technology, Pune', 'B.Tech in Information Technology', '2022'))
        self.assertEqual(profile['email'], 'kiran.example@example.com')
        self.assertEqual(profile['full_name'], 'Kiran Example')
        self.assertEqual(profile['skills'], SKILLS)
        self.assertEqual(school['start'], '', 'a value the resume does not state is never kept')

    def test_joined_cell_evidence_no_longer_loses_company_institution_or_degree(self):
        self.assert_complete(source_profile(self.source, JoinedCellModel()))

    def test_dates_and_locations_still_need_verbatim_evidence(self):
        class Loose(JoinedCellModel):
            """Puts the header address on the job and the passing year on both ends, as qwen2.5 did."""
            def generate(self, prompt, schema=None, on_progress=None):
                data = super().generate(prompt, schema, on_progress)
                data['experience'][0]['location'] = quote('Pune', 'Location: Pune')
                data['education'][0]['start'] = quote('2022', 'Passed out: 2022')
                return data
        profile = source_profile(self.source, Loose())
        self.assertEqual(profile['experience'][0]['location'], '', 'a header address is not a job location')
        self.assertEqual((profile['education'][0]['start'], profile['education'][0]['end']), ('', '2022'))

    def test_one_stated_year_is_not_both_ends_of_a_span(self):
        class Doubled(JoinedCellModel):
            def generate(self, prompt, schema=None, on_progress=None):
                data = super().generate(prompt, schema, on_progress)
                data['education'][0]['start'] = quote('2022')  # verbatim, but stated once as the passing year
                return data
        profile = source_profile(self.source, Doubled())
        self.assertEqual((profile['education'][0]['start'], profile['education'][0]['end']), ('', '2022'))

    def test_labelled_rows_fill_what_the_model_left_empty(self):
        self.assert_complete(source_profile(self.source, JoinedCellModel(blank=('company', 'institution', 'degree', 'skills'))))

    def test_email_with_a_note_is_reduced_to_the_address(self):
        profile = source_profile(self.source, JoinedCellModel())
        self.assertEqual(profile['email'], 'kiran.example@example.com')

    def test_unreadable_email_is_an_editable_question_not_a_dead_end(self):
        source = self.source.replace('kiran.example@example.com (personal address)', 'kiran.example at example dot com')
        model = JoinedCellModel(email='kiran.example at example dot com')
        with self.assertRaises(NeedsInput) as raised:
            source_profile(source, model)
        questions = raised.exception.questions
        self.assertEqual([q['field'] for q in questions], ['email'])
        profile = apply_answers(raised.exception.draft, questions, {'email': 'kiran.example@example.com'})
        self.assertEqual(profile['email'], 'kiran.example@example.com')
        self.assertEqual(profile['experience'][0]['company'], 'Example Works Solutions Pvt Ltd')

    def test_dates_out_of_order_are_asked_not_raised_after_answering(self):
        source = self.source.replace('July 2023 – Present', 'July 2023 – March 2021')
        with self.assertRaises(NeedsInput) as raised:
            source_profile(source, JoinedCellModel(blank=('company',), end='March 2021'))
        fields = sorted(q['field'] for q in raised.exception.questions)
        self.assertEqual(fields, ['end', 'start'])
        self.assertTrue(all(q['value'] for q in raised.exception.questions), 'the read values are offered for editing')
        profile = apply_answers(raised.exception.draft, raised.exception.questions,
                                {'experience.0.start': 'July 2020', 'experience.0.end': 'March 2021'})
        self.assertEqual((profile['experience'][0]['start'], profile['experience'][0]['end']), ('July 2020', 'March 2021'))

    def test_genuinely_missing_facts_are_still_asked(self):
        rows = [row for row in ROWS if not row[0].endswith(('Company Name', 'College Name'))]
        source = read_resume_text(form_docx(Path(self.temp.name) / 'partial.docx', rows))
        model = JoinedCellModel(blank=('company', 'institution'))
        with self.assertRaises(NeedsInput) as raised:
            source_profile(source, model)
        self.assertEqual(sorted(q['id'] for q in raised.exception.questions),
                         ['education.0.institution', 'experience.0.company'])


    def test_a_year_typed_as_the_degree_is_rejected_next_to_that_field(self):
        rows = [row for row in ROWS if not row[0].endswith(('Highest Qualification', 'College Name'))]
        source = read_resume_text(form_docx(Path(self.temp.name) / 'nodegree.docx', rows))
        with self.assertRaises(NeedsInput) as raised:
            source_profile(source, JoinedCellModel(blank=('institution', 'degree')))
        questions = {q['id']: q for q in raised.exception.questions}
        degree = questions['education.0.degree']
        self.assertIn('(completed 2022)', degree['label'], 'the year is shown as context, not asked for')
        self.assertIn('for example B.Tech', degree['explanation'])
        with self.assertRaises(AnswerError) as wrong:
            apply_answers(raised.exception.draft, raised.exception.questions,
                          {'education.0.degree': '2022', 'education.0.institution': 'Example Institute of Technology'})
        self.assertEqual(wrong.exception.field, 'education.0.degree')
        self.assertIn('not the year', str(wrong.exception))
        profile = apply_answers(raised.exception.draft, raised.exception.questions,
                                {'education.0.degree': 'B.Tech', 'education.0.institution': 'Example Institute of Technology'})
        self.assertEqual(profile['education'][0]['degree'], 'B.Tech')


class JobLocationTests(unittest.TestCase):
    """Production (acceptance of c4c7bfe): the model put the header address ("Pune, India") on every job and
    quoted the header line, which is verbatim, as its evidence."""

    def test_the_header_address_is_not_a_job_location(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_extraction_recovery import RESUME, QuotingModel

        class HeaderLocation(QuotingModel):
            def generate(self, prompt, schema=None, on_progress=None):
                data = super().generate(prompt, schema, on_progress)
                for job in data['experience']:
                    if job['position']['value']:
                        job['location'] = quote('Pune, India', 'asha.example@example.com, Pune, India')
                return data
        with self.assertRaises(NeedsInput) as raised:  # the freelance role states no employer
            source_profile(RESUME, HeaderLocation())
        self.assertEqual([job['location'] for job in raised.exception.draft['experience']], ['', '', '', ''])

    def test_a_location_written_with_the_role_is_kept(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_extraction_recovery import RESUME, QuotingModel
        source = RESUME.replace('Example Cloud Pvt Ltd, Senior Backend Engineer, Jan 2022 - Present',
                                'Example Cloud Pvt Ltd, Senior Backend Engineer, Bengaluru, Jan 2022 - Present')

        class StatedLocation(QuotingModel):
            def generate(self, prompt, schema=None, on_progress=None):
                data = super().generate(prompt, schema, on_progress)
                data['experience'][0]['location'] = quote('Bengaluru')
                return data
        with self.assertRaises(NeedsInput) as raised:
            source_profile(source, StatedLocation())
        self.assertEqual(raised.exception.draft['experience'][0]['location'], 'Bengaluru')


class DateSpanTests(unittest.TestCase):
    """Production (acceptance of d5f11f3): "..., 2019" printed as "2019 - 2019" because 2019 also appeared in
    job dates elsewhere in the resume."""
    SOURCE = ('Asha Example\nExample Cloud Pvt Ltd, Senior Backend Engineer, Jan 2022 - Present\n'
              'Sample Analytics LLP, Backend Engineer, Jul 2019 - Dec 2021\n'
              'Demo Retail Inc, Software Intern, Jan 2019 - Jun 2019\n'
              'Education\nB.E. Computer Engineering, Example Institute of Technology, 2019\n')

    def repaired(self, source, start, end):
        from resume_bot.ingest import repair
        out = {key: '' for key in ('full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github',
                                   'portfolio', 'current_company', 'summary')}
        out.update(full_name='Asha Example', skills=[], certifications=[], projects=[], experience=[],
                   education=[{'institution': 'Example Institute of Technology', 'degree': 'B.E. Computer Engineering',
                               'start': start, 'end': end, 'details': ''}])
        return repair(out, source)['education'][0]

    def test_a_year_stated_once_for_the_degree_is_the_completion_year(self):
        school = self.repaired(self.SOURCE, '2019', '2019')
        self.assertEqual((school['start'], school['end']), ('', '2019'))

    def test_a_range_the_resume_writes_is_kept(self):
        source = self.SOURCE.replace('Technology, 2019', 'Technology, 2019 - 2019')
        school = self.repaired(source, '2019', '2019')
        self.assertEqual((school['start'], school['end']), ('2019', '2019'))
        school = self.repaired(self.SOURCE.replace('Technology, 2019', 'Technology, 2015 - 2019'), '2015', '2019')
        self.assertEqual((school['start'], school['end']), ('2015', '2019'))


@unittest.skipUnless(all(shutil.which(t) for t in ('pdflatex', 'pdftotext')), 'needs pdfLaTeX and pdftotext')
class EmDashPdfTests(unittest.TestCase):
    def test_a_fact_with_an_em_dash_builds_and_passes_the_strict_pdf_check(self):
        # Production: the summary copied "—" from the facts and the builder refused the resume on every retry.
        from resume_bot.claim_audit import matches_pdf_text
        from resume_bot.compile import compile_latex, verify_pdf
        from resume_bot.latex import render
        from resume_bot.profile import validate_profile
        facts = json.loads(json.dumps(SummaryAttributionTests.PROFILE))
        facts['skills'] = ['Python', 'Django — REST Framework', 'Kubernetes']
        facts['education'][0]['degree'] = 'B.E. — Computer Engineering'
        summary = composed_summary(facts, 'Python Backend Developer', 7, ['Python', 'Django'])
        self.assertIn('—', summary)
        self.assertEqual(summary_problems(summary, facts, 'Python Backend Developer', 7), [])
        profile = validate_profile({**facts, 'headline': 'Python Backend Developer', 'summary': summary})
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'main.tex').write_text(render(profile, years=7), encoding='utf-8')
            pdf = compile_latex(Path(temp))
            verify_pdf(pdf, profile, 2)
            self.assertTrue(matches_pdf_text(read_resume_text(pdf), profile, 7))


class SummaryAttributionTests(unittest.TestCase):
    PROFILE = {'full_name': 'Asha Example', 'email': 'asha@example.com', 'skills': ['Python', 'Django', 'Kubernetes'],
               'experience': [{'company': 'Example Cloud Pvt Ltd', 'position': 'Senior Backend Engineer', 'start': 'Jan 2022',
                               'end': 'Present', 'location': '',
                               'bullets': ['Built payment APIs in Python and PostgreSQL.',
                                           'Led migration of batch jobs to Kubernetes.']},
                              {'company': 'Sample Analytics LLP', 'position': 'Backend Engineer', 'start': 'Jul 2019',
                               'end': 'Dec 2021', 'location': '', 'bullets': ['Maintained REST services in Django.']}],
               'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.E. Computer Engineering',
                              'start': '', 'end': '2019', 'details': ''}]}

    def test_the_production_summary_is_rejected_for_both_defects(self):
        text = ('Python Backend Developer with 7 years of experience building scalable backend systems using Django and '
                'Python. Led migration of batch jobs to Kubernetes and developed payment APIs in Python and PostgreSQL '
                'at Example Cloud Pvt Ltd and Sample Analytics LLP.')
        problems = ' '.join(summary_problems(text, self.PROFILE, 'Python Backend Developer', 7))
        self.assertIn('do not name employers', problems)
        self.assertIn('Example Cloud Pvt Ltd', problems)
        self.assertIn('scalable', problems)

    def test_a_career_change_never_claims_years_in_the_target_role(self):
        text = ('DevOps Engineer with 7 years of experience building payment APIs in Python and PostgreSQL and '
                'leading the migration of batch jobs to Kubernetes.')
        self.assertTrue(any('career change' in p for p in summary_problems(text, self.PROFILE, 'DevOps Engineer', 7)))
        honest = ('Senior Backend Engineer with 7 years of experience building payment APIs in Python and PostgreSQL, '
                  'now moving into DevOps work after leading the migration of batch jobs to Kubernetes.')
        self.assertEqual(summary_problems(honest, self.PROFILE, 'DevOps Engineer', 7), [])
        composed = composed_summary(self.PROFILE, 'DevOps Engineer', 7, ['Kubernetes', 'Docker'])
        self.assertIn('moving into a DevOps Engineer role', composed)
        self.assertEqual(summary_problems(composed, self.PROFILE, 'DevOps Engineer', 7), [])

    def test_tools_are_tied_only_to_work_that_used_them(self):
        profile = {'skills': ['AWS', 'Terraform', 'Docker', 'Kubernetes'], 'certifications': [], 'projects': [],
                   'experience': [{'company': 'Example Labs', 'position': 'Cloud Engineer', 'start': 'Jan 2021',
                                   'end': 'Present', 'location': '',
                                   'bullets': ['Provisioned AWS EC2 and S3 resources with Terraform.']}],
                   'education': []}
        invented = ('AWS Cloud Engineer with 5 years of experience provisioning AWS EC2 and S3 resources with Terraform '
                    'and managing infrastructure with Docker and Kubernetes.')
        problems = ' '.join(summary_problems(invented, profile, 'AWS Cloud Engineer', 5))
        self.assertIn('ties Docker, Kubernetes to work', problems)
        listed = ('AWS Cloud Engineer with 5 years of experience provisioning AWS EC2 and S3 resources with Terraform. '
                  'Works with Docker and Kubernetes.')
        self.assertEqual(summary_problems(listed, profile, 'AWS Cloud Engineer', 5), [])
        inflated = ('AWS Cloud Engineer with 5 years of experience building and managing cloud infrastructure on AWS EC2 '
                    'and S3 with Terraform.')
        self.assertIn('claims responsibilities the resume does not state: managed',
                      ' '.join(summary_problems(inflated, profile, 'AWS Cloud Engineer', 5)))

    def test_a_career_change_summary_is_composed_from_facts(self):
        class Writer:
            model = 'fake'

            def generate(self, prompt, schema=None, on_progress=None):
                return {'summary': 'Senior Backend Engineer with 7 years of experience. Transitioned into a DevOps '
                                   'Engineer role focusing on automation.'}
        with patch('resume_bot.generate.generate', side_effect=lambda ai, prompt, **k: ai.generate(prompt)):
            text, how = write_summary(self.PROFILE, 'DevOps Engineer', 7, ['Kubernetes'], Writer())
        self.assertEqual(how, 'composed')
        self.assertNotIn('Transitioned', text)
        self.assertIn('moving into a DevOps Engineer role', text)

    def test_a_character_from_the_facts_is_allowed_in_a_composed_summary(self):
        # Production: a skill with an em dash was copied into the composed summary, and the builder's
        # plain-character rule rejected the resume on every retry.
        profile = json.loads(json.dumps(self.PROFILE))
        profile['skills'] = ['Python', 'Django — REST Framework', 'Kubernetes']
        composed = composed_summary(profile, 'Python Backend Developer', 7, ['Python', 'Django'])
        self.assertIn('—', composed)
        self.assertEqual(summary_problems(composed, profile, 'Python Backend Developer', 7), [])
        written = ('Python Backend Developer with 7 years of experience building payment APIs in Python and '
                   'PostgreSQL — and maintaining REST services in Django.')
        self.assertIn('use plain characters only', summary_problems(written, self.PROFILE, 'Python Backend Developer', 7),
                      'a character the facts do not contain is still rejected')

    def test_job_families_recognise_same_track_titles(self):
        def profile(title):
            return {'experience': [{'position': title}]}
        for past, target, expected in (('Frontend Developer', 'React.js Developer', True),
                                       ('Frontend Developer', 'UI Developer', True),
                                       ('Build Engineer', 'DevOps Engineer', True),
                                       ('Software Engineer', 'Python Backend Developer', True),
                                       ('Reporting Analyst', 'Data Analyst', True),
                                       ('Mobile Developer', 'Android Developer', True),
                                       ('QA Engineer', 'Java Backend Developer', False),
                                       ('Senior Backend Engineer', 'DevOps Engineer', False),
                                       ('Data Analyst', 'Data Scientist', False)):
            with self.subTest(past=past, target=target):
                self.assertEqual(same_track(profile(past), target), expected)

    def test_a_quality_the_resume_states_is_allowed(self):
        profile = json.loads(json.dumps(self.PROFILE))
        profile['experience'][0]['bullets'].append('Designed scalable queue consumers in Python.')
        text = ('Python Backend Developer with 7 years of experience building scalable payment APIs and Django REST '
                'services in Python, and moving batch jobs to Kubernetes.')
        self.assertEqual(summary_problems(text, profile, 'Python Backend Developer', 7), [])
        self.assertTrue(summary_problems(text.replace('scalable', 'high-performance'), profile,
                                         'Python Backend Developer', 7))


class StrandedJobTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        document = base64.b64encode(form_docx(Path(self.temp.name) / 'resume.docx').read_bytes()).decode()
        with patch.object(self.jobs, 'start'):
            self.id = self.jobs.enqueue('b' * 64, {'role': 'React.js Developer', 'consent': CONSENT_VERSION,
                                                   'extension': 'docx', 'document': document})

    def row(self):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', ('b' * 64, self.id)).fetchone()

    def prepare(self, model):
        with patch('resume_bot.ingest.generate', side_effect=lambda ai, prompt, **k: model.generate(prompt)), \
                patch('resume_bot.automation.generate', return_value={}), \
                patch('resume_bot.generate.generate', side_effect=lambda ai, prompt, **k: {'projects': []}
                      if 'portfolio projects' in prompt else (_ for _ in ()).throw(RuntimeError('no model'))), \
                patch.object(self.workspace, 'enqueue', return_value={'name': 'private-' + 'b' * 64 + '-1.json'}):
            self.jobs._prepare(self.row())

    def test_form_docx_reaches_the_builder_without_questions(self):
        self.prepare(JoinedCellModel())
        row = self.row()
        self.assertEqual(row['status'], 'handed_off', row['error'])
        facts = json.loads(row['facts'])
        self.assertEqual(facts['experience'][0]['company'], 'Example Works Solutions Pvt Ltd')
        self.assertEqual(facts['education'][0]['degree'], 'B.Tech in Information Technology')
        self.assertEqual(row['candidate'], 'Kiran Example')

    def test_questions_from_an_older_extraction_are_read_again(self):
        stale = {'source': read_resume_text(form_docx(Path(self.temp.name) / 'again.docx')),
                 'draft': {'experience': []}, 'questions': [{'id': 'experience.0.company'}]}
        current = {**stale, 'extraction': EXTRACTION_VERSION}
        projects = {'source': stale['source'], 'proposals': [{'name': 'Example'}]}
        with closing(self.jobs.connection()) as db, db:
            db.execute("UPDATE automation SET checkpoints=?,status='needs_input',stage='Needs your input',percent=25 "
                       'WHERE owner=? AND id=?', (json.dumps(stale), 'b' * 64, self.id))
            for owner, checkpoints in (('c' * 64, current), ('d' * 64, projects)):
                db.execute("INSERT INTO automation(owner,id,payload,checkpoints,status,stage,percent,created,updated) "
                           "VALUES (?,?,'{}',?,'needs_input','Needs your input',25,1,1)",
                           (owner, owner[:8], json.dumps(checkpoints)))
        self.jobs._recheck_stale_questions()
        with closing(self.jobs.connection()) as db:
            rows = {r['owner'][0]: r for r in db.execute('SELECT owner, status, checkpoints FROM automation')}
        self.assertEqual(rows['b']['status'], 'queued')
        self.assertNotIn('questions', json.loads(rows['b']['checkpoints']))
        self.assertIn('source', json.loads(rows['b']['checkpoints']), 'the original upload is kept')
        self.assertEqual(rows['c']['status'], 'needs_input', 'questions from the current extraction stay')
        self.assertEqual(rows['d']['status'], 'needs_input', 'project choices are never discarded')
        self.prepare(JoinedCellModel())
        self.assertEqual(self.row()['status'], 'handed_off')


if __name__ == '__main__':
    unittest.main()

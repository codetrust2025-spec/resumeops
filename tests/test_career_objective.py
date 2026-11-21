"""Career Objectives are role-led, evidence-safe, grounded in the candidate's own work, different between candidates,
and typeset only after approval."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from resume_bot.automation import _verified_role_resume
from resume_bot.claim_audit import matches_pdf_text
from resume_bot.compile import compile_latex
from resume_bot.generate import (OBJECTIVE_FALLBACKS, OBJECTIVE_WORK, objective_area, objective_problems,
                                 summary_problems, write_summary)
from resume_bot.ingest import read_resume_text
from resume_bot.latex import render
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles


def profile(role, experienced, bullets=('Built a dashboard.',)):
    return validate_profile({
        'full_name': 'Example Applicant', 'phone': '+1 202 555 0147',
        'email': 'candidate@example.test', 'skills': ['Java', 'Angular', 'React.js', 'MongoDB'],
        'experience': [{'position': role, 'company': 'Example Employer',
                        'start': '2022', 'end': '2025', 'bullets': list(bullets)}] if experienced else [],
        'education': [{'degree': 'B.Tech', 'institution': 'Example University', 'end': '2021'}],
        'projects': ['Sample Tracker - Built a dashboard.'],
    })


CLINIC = ('Built Spring Boot REST APIs for appointment booking.', 'Wrote JUnit tests for the booking service.')


class CareerObjectiveTests(unittest.TestCase):
    def test_requested_roles_generate_and_typeset_one_verified_paragraph(self):
        groups = [
            ('MERN Stack Developer', 'web applications', 'mern-stack-developer', True),
            ('Angular Developer', 'user interfaces', 'angular-developer', True),
            ('React.js Developer', 'user interfaces', 'react-developer', True),
            ('Java Backend Developer', 'software services', 'java-backend-developer', True),
            ('Azure Data Engineer', 'data solutions', 'azure-data-engineer', True),
            ('Software Engineer', 'software solutions', 'custom', False),
        ]
        load_roles()
        for title, focus, role, experienced in groups:
            with self.subTest(role=title):
                evidence = profile(title, experienced)
                identity = title if experienced else title + ' candidate'
                with patch('resume_bot.generate.generate', return_value={'area': 'dashboard', 'strengths': []}):
                    result, method = write_summary(evidence, title, 3 if experienced else 0, [], object())
                self.assertEqual(method, 'ai')
                self.assertTrue(result.startswith(f'{identity} focused on dependable {focus} and structured '
                                                  'problem-solving. '), result)
                self.assertIn('dashboard work', result)
                self.assertTrue(40 <= len(result.split()) <= 55, result)

                approved = {**copy.deepcopy(evidence), 'headline': title, 'summary': result}
                target = {'role': role, 'role_title': title, 'experience_years': 3 if experienced else 0}
                self.assertTrue(_verified_role_resume(approved, target, evidence, 4))
                self.assertFalse(_verified_role_resume({**approved, 'summary': 'Too short.'}, target, evidence, 4))
                self.assertEqual(approved['skills'], evidence['skills'])
                self.assertEqual(approved['experience'], evidence['experience'])
                tex = render(approved, complete_sections=True, career_objective=True)
                objective = tex.split(r'\section*{Career Objective}', 1)[1].split(r'\section*', 1)[0]
                self.assertIn(r'\textbf{' + title + '}', objective)
                self.assertIn(r'\textbf{structured problem-solving}', objective)
                with TemporaryDirectory() as directory:
                    folder = Path(directory)
                    (folder / 'main.tex').write_text(tex, encoding='utf-8')
                    pdf_text = read_resume_text(compile_latex(folder))
                    self.assertTrue(matches_pdf_text(pdf_text, approved, None, complete_sections=True))

    def test_prohibited_content_is_rejected_even_when_documented(self):
        evidence = profile('Angular Developer', True)
        opening = 'Angular Developer focused on dependable user interfaces and structured problem-solving.'
        safe = f"{opening} {OBJECTIVE_FALLBACKS[0].format(work=OBJECTIVE_WORK['frontend'])}"
        self.assertEqual(objective_problems(safe, evidence, 'Angular Developer'), [])
        for extra in ('Java', 'B.Tech', 'Example Employer', 'Sample Tracker', 'three years',
                      'built dashboards', 'improved performance', r'\textbf{business needs}', 'business\nneeds'):
            with self.subTest(extra=extra):
                self.assertTrue(objective_problems(safe.replace('business needs', extra),
                                                   evidence, 'Angular Developer'))

    def test_every_objective_passes_the_summary_fact_check(self):
        # Release 4911f58 was rolled back: the fallback objective said "solutions that teams can maintain", and a
        # resume whose work never mentions maintaining failed the fact check every summary passes.
        catalog = load_roles()
        self.assertGreater(len(catalog), 10)
        for experienced in (True, False):
            for role in catalog.values():
                title = role['name']
                for draft in ({}, {'area': 'dashboard', 'strengths': []}):
                    with self.subTest(role=title, experienced=experienced, draft=draft):
                        evidence = profile('Frontend Developer', experienced)
                        with patch('resume_bot.generate.generate', return_value=draft):
                            text, mode = write_summary(evidence, title, 3 if experienced else 0, [], object())
                        self.assertEqual(mode, 'ai' if draft else 'composed')
                        self.assertEqual(objective_problems(text, evidence, title), [])
                        self.assertEqual(summary_problems(text, evidence, title, None), [])
                        self.assertTrue(40 <= len(text.split()) <= 55, text)
                        self.assertNotRegex(text, r'(?i)\bmaintain\b')

    def test_role_families_get_their_own_wording(self):
        evidence = profile('Frontend Developer', True)
        expected = {'React.js Developer': 'accessible, well-tested interface features',
                    'Java Backend Developer': 'practical, well-tested services',
                    'Azure Data Engineer': 'accurate, well-documented data work',
                    'QA Engineer': 'thorough, repeatable test cases'}
        for title, work in expected.items():
            with self.subTest(role=title), patch('resume_bot.generate.generate', return_value={}):
                self.assertIn(work, write_summary(evidence, title, 3, [], object())[0])

    def test_the_model_names_an_area_from_the_candidates_own_lines_and_picks_strengths(self):
        evidence = profile('Software Engineer', True, CLINIC)
        draft = {'area': 'Appointment Booking', 'strengths': ['careful testing', 'readable code']}
        with patch('resume_bot.generate.generate', return_value=draft) as model:
            text, mode = write_summary(evidence, 'Java Backend Developer', 3, [], object())
        self.assertEqual(mode, 'ai')
        self.assertIn('appointment booking work', text)
        self.assertIn('careful testing', text)
        self.assertEqual(model.call_count, 1)
        schema = model.call_args.kwargs['schema']
        self.assertEqual(schema['required'], ['area', 'strengths'])
        self.assertIn('careful testing', schema['properties']['strengths']['items']['enum'])
        self.assertIn('Built Spring Boot REST APIs for appointment booking.', model.call_args.args[1])
        self.assertNotIn('Example Employer', model.call_args.args[1])  # employers are never offered as an area
        self.assertEqual(summary_problems(text, evidence, 'Java Backend Developer', None), [])

    def test_an_area_the_facts_do_not_state_is_never_printed(self):
        evidence = profile('Software Engineer', True, CLINIC)
        for area in ('healthcare platforms', 'spring boot apis', 'example employer', 'online orders',
                     'scalable booking', 'managed booking'):
            with self.subTest(area=area):
                self.assertEqual(objective_area(area, evidence), '')
        self.assertEqual(objective_area('booking service', evidence), 'booking service')
        # Seen live: "payment apis work". Acronyms keep the candidate's spelling; ordinary words stay lower case.
        payments = profile('Software Engineer', True, ('Built payment APIs for UPI refunds.',
                                                       'Payment reports were checked weekly.'))
        self.assertEqual(objective_area('Payment APIS', payments), 'payment APIs')
        self.assertEqual(objective_area('upi refunds', payments), 'UPI refunds')
        with patch('resume_bot.generate.generate', side_effect=[{'area': 'healthcare platforms', 'strengths': []},
                                                                {'area': 'hospital software', 'strengths': []}]) as model:
            text, mode = write_summary(evidence, 'Java Backend Developer', 3, [], object())
        self.assertEqual(mode, 'composed')
        self.assertEqual(model.call_count, 2)
        self.assertIn("'healthcare platforms' was rejected", model.call_args_list[1].args[1])
        self.assertNotRegex(text, r'(?i)healthcare|hospital')
        self.assertEqual(objective_problems(text, evidence, 'Java Backend Developer'), [])

    def test_objectives_differ_between_candidates_and_stay_stable_for_one(self):
        def objective(bullets, area):
            evidence = profile('Software Engineer', True, bullets)
            with patch('resume_bot.generate.generate', return_value={'area': area, 'strengths': []}):
                return write_summary(evidence, 'Java Backend Developer', 3, [], object())[0]
        clinic = objective(CLINIC, 'appointment booking')
        loans = objective(('Built Spring Boot services for loan repayment schedules.',), 'loan repayment')
        self.assertNotEqual(clinic.split('.', 1)[1], loans.split('.', 1)[1])
        self.assertEqual(clinic, objective(CLINIC, 'appointment booking'))  # the same facts always read the same

    def test_without_work_lines_or_a_model_the_objective_is_still_complete(self):
        fresher = validate_profile({'full_name': 'Example Applicant', 'phone': '+1 202 555 0147',
                                    'email': 'candidate@example.test', 'skills': ['Java'], 'experience': [],
                                    'education': [{'degree': 'B.Tech', 'institution': 'Example University',
                                                   'end': '2021'}], 'projects': []})
        with patch('resume_bot.generate.generate', side_effect=RuntimeError('model offline')) as model:
            text, mode = write_summary(fresher, 'Java Backend Developer', 0, [], object())
        self.assertEqual((mode, model.call_count), ('composed', 0))  # nothing to read: the model is not asked
        self.assertEqual(objective_problems(text, fresher, 'Java Backend Developer'), [])
        evidence = profile('Software Engineer', True, CLINIC)
        with patch('resume_bot.generate.generate', side_effect=RuntimeError('model offline')):
            text, mode = write_summary(evidence, 'Java Backend Developer', 3, [], object())
        self.assertEqual(mode, 'composed')
        self.assertEqual(objective_problems(text, evidence, 'Java Backend Developer'), [])

"""Career Objectives are role-led, evidence-safe and typeset only after approval."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from resume_bot.automation import _verified_role_resume
from resume_bot.claim_audit import matches_pdf_text
from resume_bot.compile import compile_latex
from resume_bot.generate import (broad_objective_problems, objective_problems,
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
                with patch('resume_bot.generate.generate', return_value={'strengths': ['clear technical judgment', 'clear communication']}):
                    result, method = write_summary(evidence, title, 3 if experienced else 0, [], object())
                self.assertEqual(method, 'ai')
                self.assertTrue(result.startswith(f'{identity} focused on dependable {focus} and structured '
                                                  'problem-solving. '), result)
                self.assertNotIn('dashboard', result)
                self.assertTrue(40 <= len(result.split()) <= 55, result)

                approved = {**copy.deepcopy(evidence), 'headline': title, 'summary': result}
                target = {'role': role, 'role_title': title, 'experience_years': 3 if experienced else 0}
                self.assertTrue(_verified_role_resume(approved, target, evidence, 5))
                self.assertFalse(_verified_role_resume({**approved, 'summary': 'Too short.'}, target, evidence, 5))
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
        with patch('resume_bot.generate.generate', return_value={}):
            safe, _ = write_summary(evidence, 'Angular Developer', 3, [], object())
        self.assertEqual(objective_problems(safe, evidence, 'Angular Developer'), [])
        for extra in ('Java', 'B.Tech', 'Example Employer', 'Sample Tracker', 'three years',
                      'built dashboards', 'improved performance', r'\textbf{business needs}', 'business\nneeds'):
            with self.subTest(extra=extra):
                self.assertTrue(objective_problems(safe.replace('business priorities', extra),
                                                   evidence, 'Angular Developer'))

    def test_every_objective_passes_the_summary_fact_check(self):
        # Release 4911f58 was rolled back: the fallback objective said "solutions that teams can maintain", and a
        # resume whose work never mentions maintaining failed the fact check every summary passes.
        catalog = load_roles()
        self.assertGreater(len(catalog), 10)
        for experienced in (True, False):
            for role in catalog.values():
                title = role['name']
                for draft in ({}, {'strengths': ['clear technical judgment', 'clear communication']}):
                    with self.subTest(role=title, experienced=experienced, draft=draft):
                        evidence = profile('Frontend Developer', experienced)
                        with patch('resume_bot.generate.generate', return_value=draft):
                            text, mode = write_summary(evidence, title, 3 if experienced else 0, [], object())
                        self.assertEqual(mode, 'ai' if draft else 'composed')
                        self.assertEqual(objective_problems(text, evidence, title), [])
                        self.assertEqual(summary_problems(text, evidence, title, None), [])
                        self.assertTrue(40 <= len(text.split()) <= 55, text)
                        self.assertNotRegex(text, r'(?i)\bmaintain\b')

    def test_hrms_and_test_case_details_are_excluded_even_when_verified(self):
        title = 'QA Automation Engineer'
        evidence = profile(title, True, ('Wrote repeatable test cases for HRMS work.',))
        old = ('QA Automation Engineer focused on dependable software quality and structured problem-solving. '
               'Aims to bring careful testing and clear technical judgment to HRMS work, turning agreed requirements '
               'into thorough, repeatable test cases. Values honest estimates, careful reviews and steady learning '
               'within a collaborative team.')
        self.assertEqual(objective_problems(old, evidence, title), [])
        self.assertTrue(broad_objective_problems(old, evidence, title))
        with patch('resume_bot.generate.generate', side_effect=[
                {'area': 'HRMS', 'strengths': ['software quality', 'attention to detail']},
                {'strengths': ['software quality', 'attention to detail']}]) as model:
            text, mode = write_summary(evidence, title, 3, [], object())
        self.assertEqual(mode, 'ai')
        self.assertEqual(model.call_count, 2)
        self.assertNotIn('HRMS', model.call_args.args[1])
        self.assertNotIn('test cases', text)
        self.assertEqual(broad_objective_problems(text, evidence, title), [])

    def test_unavailable_model_uses_broad_wording(self):
        evidence = profile('Software Engineer', False)
        with patch('resume_bot.generate.generate', side_effect=RuntimeError('model offline')):
            text, mode = write_summary(evidence, 'Software Engineer', 0, [], object())
        self.assertEqual(mode, 'composed')
        self.assertEqual(broad_objective_problems(text, evidence, 'Software Engineer'), [])

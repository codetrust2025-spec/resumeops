"""Career Objectives are role-led, evidence-safe, and typeset only after approval."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from resume_bot.automation import _verified_role_resume
from resume_bot.claim_audit import matches_pdf_text
from resume_bot.compile import compile_latex
from resume_bot.generate import objective_problems, summary_problems, write_summary
from resume_bot.ingest import read_resume_text
from resume_bot.latex import render
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles


def profile(role, experienced):
    return validate_profile({
        'full_name': 'Example Applicant', 'phone': '+1 202 555 0147',
        'email': 'candidate@example.test', 'skills': ['Java', 'Angular', 'React.js', 'MongoDB'],
        'experience': [{'position': role, 'company': 'Example Employer',
                        'start': '2022', 'end': '2025', 'bullets': ['Built a dashboard.']}] if experienced else [],
        'education': [{'degree': 'B.Tech', 'institution': 'Example University', 'end': '2021'}],
        'projects': ['Sample Tracker - Built a dashboard.'],
    })


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
                valid = (f'{identity} focused on dependable {focus} and structured problem-solving. '
                         'Aims to turn business needs into clear technical decisions and contribute to maintainable '
                         'solutions that help teams work with greater confidence and sustain quality as business requirements and operating conditions evolve.')
                with patch('resume_bot.generate.generate', side_effect=[
                        {'summary': 'Too short.'}, {'summary': valid.replace('business needs', 'MongoDB and React')},
                        {'summary': valid}]) as model:
                    result, method = write_summary(evidence, title, 3 if experienced else 0, [], object())
                self.assertEqual((result, method), (valid, 'ai'))
                self.assertEqual(model.call_count, 3)
                self.assertIn('40-55 words', model.call_args_list[1].args[1])
                self.assertIn('keep named technologies', model.call_args_list[2].args[1])

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
        with patch('resume_bot.generate.generate', return_value={}):
            safe, mode = write_summary(evidence, 'Angular Developer', 3, [], object())
        self.assertEqual(mode, 'composed')
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
                with self.subTest(role=title, experienced=experienced):
                    evidence = profile('Frontend Developer', experienced)
                    with patch('resume_bot.generate.generate', return_value={}):
                        text, mode = write_summary(evidence, title, 3 if experienced else 0, [], object())
                    self.assertEqual(mode, 'composed')
                    self.assertEqual(objective_problems(text, evidence, title), [])
                    self.assertEqual(summary_problems(text, evidence, title, None), [])
                    self.assertTrue(40 <= len(text.split()) <= 55, text)
                    self.assertNotRegex(text, r'(?i)\bmaintain\b|services\.$')

    def test_role_families_get_their_own_fallback_wording(self):
        evidence = profile('Frontend Developer', True)
        expected = {'React.js Developer': 'accessible, well-tested interface features',
                    'Java Backend Developer': 'practical, well-tested services',
                    'Azure Data Engineer': 'accurate, well-documented data work',
                    'QA Engineer': 'thorough, repeatable test cases'}
        for title, work in expected.items():
            with self.subTest(role=title), patch('resume_bot.generate.generate', return_value={}):
                self.assertIn(work, write_summary(evidence, title, 3, [], object())[0])

    def test_model_writes_three_sentence_endings_after_the_fixed_opening(self):
        evidence = profile('Frontend Developer', True)
        draft = {'aims': 'Aims to understand what users need and turn agreed requirements into clear, accessible '
                         'screens.',
                 'seeks': 'contribute careful reviews and steady improvements to the shared codebase',
                 'values': 'Plain communication, honest estimates and steady learning.'}
        with patch('resume_bot.generate.generate', return_value=draft) as model:
            text, mode = write_summary(evidence, 'React.js Developer', 3, [], object())
        self.assertEqual(mode, 'ai')
        self.assertEqual(model.call_args.kwargs['schema']['required'], ['aims', 'seeks', 'values'])
        self.assertEqual(text, 'React.js Developer candidate focused on dependable user interfaces and structured '
                               'problem-solving. Aims to understand what users need and turn agreed requirements into '
                               'clear, accessible screens. Seeks to contribute careful reviews and steady improvements '
                               'to the shared codebase. Values plain communication, honest estimates and steady '
                               'learning.')
        self.assertEqual(summary_problems(text, evidence, 'React.js Developer', None), [])

    def test_unstated_responsibilities_and_banned_words_are_named_for_the_retry(self):
        evidence = profile('Frontend Developer', True)
        drafts = [{'aims': 'maintain the interface code and keep every screen consistent for the people using it',
                   'seeks': 'contribute careful reviews and steady improvements to the shared codebase',
                   'values': 'plain communication, honest estimates and steady learning'},
                  {'aims': 'apply experience with every screen and keep the interface consistent for its users',
                   'seeks': 'contribute careful reviews and steady improvements to the shared codebase',
                   'values': 'plain communication, honest estimates and steady learning'},
                  {}]
        with patch('resume_bot.generate.generate', side_effect=drafts) as model:
            text, mode = write_summary(evidence, 'React.js Developer', 3, [], object())
        self.assertEqual(mode, 'composed')
        self.assertIn('claims responsibilities the resume does not state: maintained', model.call_args_list[1].args[1])
        self.assertIn('such as "experience"', model.call_args_list[2].args[1])
        # "user experience" is a priority the objective may name, not a claim of tenure.
        self.assertNotIn('"experience"', '; '.join(objective_problems(
            text.replace('steady learning', 'user experience'), evidence, 'React.js Developer')))

"""The AI-first form never drops what the candidate typed.

Before this fix, a facts note made every optional field disappear when no work or education entry was typed
(skills, location, links, certifications, achievements, coding platforms, projects, years), and a typed entry
made the note disappear. Now the note is read by the extraction and every typed field travels beside it as
attested facts, joined by merge_attested. A form with typed entries and no note is unchanged. Every
candidate here is fictional."""
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import resume_bot.automation as automation
from resume_bot.automation import CONSENT_VERSION, merge_attested, source_profile
from resume_bot.ingest import ExtractionError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs, submission  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'f' * 64
NOTE = 'Frontend Developer at Example Works since July 2023. B.Tech IT, Example Institute, 2022.'
EXTRAS = {'email': '', 'years_of_experience': 3, 'location': 'Pune', 'current_company': 'Example Works',
          'linkedin': 'https://linkedin.com/in/kiran-example', 'github': 'https://github.com/kiran-example',
          'portfolio': '', 'certifications': 'AWS Cloud Practitioner', 'achievements': 'Hackathon finalist',
          'skills': 'React.js, Redux', 'coding_platforms': [{'platform': 'LeetCode', 'url': 'https://leetcode.com/kiran-example'}],
          'projects': [{'name': 'Inventory Dashboard', 'stack': 'React.js', 'description': 'Charts for stock levels'}]}
JOB = {'company': 'Example Works', 'position': 'Frontend Developer', 'start': 'July 2023', 'end': 'Present',
       'location': '', 'responsibilities': ''}


def form(**candidate):
    return {'consent': CONSENT_VERSION, 'role': 'React.js Developer',
            'candidate': {'full_name': 'Kiran Example', 'phone': '+91 90000 00000', 'background': '',
                          'work_experience': [], 'education': [], **candidate}}


def from_note(**changes):
    """What the verbatim extraction reads from NOTE (the model's part)."""
    facts = {'full_name': 'Kiran Example', 'email': '', 'phone': '+91 90000 00000', 'skills': [],
             'experience': [{'company': 'Example Works', 'position': 'Frontend Developer', 'start': 'July 2023',
                             'end': 'Present', 'location': '', 'bullets': []}],
             'education': [{'institution': 'Example Institute', 'degree': 'B.Tech IT', 'start': '', 'end': '2022',
                            'details': ''}], 'certifications': [], 'projects': []}
    facts.update(changes)
    return automation.validate_profile(facts) if facts['experience'] or facts['education'] else facts


class SubmissionKeepsEveryField(unittest.TestCase):
    def test_a_note_keeps_every_typed_field(self):
        request = submission(form(background=NOTE, **EXTRAS))
        self.assertEqual(request['background'], NOTE)
        typed = request['attested']
        self.assertEqual(typed['skills'], ['React.js', 'Redux'])
        self.assertEqual((typed['location'], typed['current_company']), ('Pune', 'Example Works'))
        self.assertEqual((typed['linkedin'], typed['github']), (EXTRAS['linkedin'], EXTRAS['github']))
        self.assertEqual((typed['certifications'], typed['achievements']), (['AWS Cloud Practitioner'], ['Hackathon finalist']))
        self.assertEqual(typed['coding_platforms'], ['LeetCode https://leetcode.com/kiran-example'])
        self.assertTrue(typed['projects'][0].startswith('Inventory Dashboard'))
        self.assertEqual(request['years'], 3)

    def test_a_typed_entry_keeps_the_note(self):
        request = submission(form(background=NOTE, work_experience=[JOB], skills='React.js'))
        self.assertEqual(request['background'], NOTE)
        self.assertEqual([(e['company'], e['position']) for e in request['attested']['experience']],
                         [('Example Works', 'Frontend Developer')])
        self.assertNotIn('profile', request)

    def test_typed_entries_without_a_note_are_unchanged(self):
        request = submission(form(work_experience=[{**JOB, 'responsibilities': 'Built React.js screens.'}],
                                  skills='React.js', years_of_experience=3))
        self.assertEqual(sorted(request), ['consent', 'profile', 'role', 'years'])
        self.assertEqual(request['profile']['experience'][0]['bullets'], ['Built React.js screens.'])

    def test_name_and_phone_alone_make_the_same_request_as_before(self):
        self.assertEqual(submission(form()), {
            'consent': CONSENT_VERSION, 'role': 'React.js Developer', 'background': '',
            'identity': {'full_name': 'Kiran Example', 'phone': '+91 90000 00000', 'email': ''}})

    def test_a_mistake_in_a_typed_field_is_still_reported_on_submit(self):
        with self.assertRaises(ValueError):
            submission(form(background=NOTE, skills='React.js, react.js'))  # duplicate skill


class PreparationMergesNoteAndForm(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = AutomationJobs(Workspace(Path(self.temp.name), code_root=ROOT))

    def prepare(self, payload, extracted):
        """Run preparation with the extraction's answer, and stop at the role catalog: return the merged facts
        and how the extraction was called."""
        with patch.object(self.jobs, 'start'):
            identifier = self.jobs.enqueue(OWNER, payload)
        with closing(self.jobs.connection()) as db:
            row = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
        merged, calls = [], []
        real = automation.merge_attested

        def extraction(source, *args, **kwargs):
            calls.append((source, kwargs))
            return extracted
        with patch('resume_bot.automation.source_profile', side_effect=extraction), \
                patch('resume_bot.automation.merge_attested', side_effect=lambda f, a: merged.append(real(f, a)) or merged[-1]), \
                patch('resume_bot.automation.role_intelligence', side_effect=RuntimeError('stop after the merge')):
            self.jobs._prepare(row)
        return merged, calls

    def test_the_note_and_every_typed_field_reach_the_resume_facts(self):
        merged, calls = self.prepare(form(background=NOTE, **EXTRAS), from_note())
        self.assertIn(NOTE, calls[0][0], 'the note is read by the extraction')
        self.assertEqual(calls[0][1]['attested']['skills'], ['React.js', 'Redux'], 'typed fields reach the extraction')
        facts = merged[-1]
        self.assertEqual([(e['company'], e['position']) for e in facts['experience']], [('Example Works', 'Frontend Developer')])
        self.assertEqual(facts['skills'], ['React.js', 'Redux'])
        self.assertEqual((facts['location'], facts['linkedin'], facts['github']),
                         ('Pune', EXTRAS['linkedin'], EXTRAS['github']))
        self.assertEqual((facts['certifications'], facts['achievements']), (['AWS Cloud Practitioner'], ['Hackathon finalist']))
        self.assertEqual(facts['coding_platforms'], ['LeetCode https://leetcode.com/kiran-example'])
        self.assertTrue(facts['projects'][0].startswith('Inventory Dashboard'))

    def test_a_typed_entry_and_a_note_with_other_facts_are_both_kept(self):
        note_only_skills = from_note(experience=[], education=[], skills=['TypeScript'], projects=['Admin Portal'])
        merged, calls = self.prepare(form(background='Skills: TypeScript. Built the Admin Portal.',
                                          work_experience=[JOB], skills='React.js'),
                                     note_only_skills)
        self.assertEqual(len(calls[0][1]['attested']['experience']), 1, 'the typed job reaches the extraction')
        facts = merged[-1]
        self.assertEqual([(e['company'], e['position']) for e in facts['experience']], [('Example Works', 'Frontend Developer')])
        self.assertEqual(facts['skills'], ['TypeScript', 'React.js'])
        self.assertEqual(facts['projects'], ['Admin Portal'])


class MergeRules(unittest.TestCase):
    def test_typed_details_join_the_same_entry_and_merging_twice_changes_nothing(self):
        note = from_note(experience=[{'company': 'Example Works', 'position': 'Frontend Developer', 'start': 'July 2023',
                                      'end': '', 'location': '', 'bullets': ['Built React.js screens.']}],
                         skills=['react.js'], location='Mumbai')
        typed = {'experience': [{'company': 'example works', 'position': 'Frontend developer', 'start': 'July 2023',
                                 'end': 'Present', 'location': 'Pune', 'bullets': ['Built React.js screens.', 'Wrote Jest tests.']}],
                 'skills': ['React.js', 'Redux'], 'location': 'Pune'}
        once = merge_attested(note, typed)
        self.assertEqual(len(once['experience']), 1, 'the same job, written differently, is one entry')
        self.assertEqual(once['experience'][0]['bullets'], ['Built React.js screens.', 'Wrote Jest tests.'])
        self.assertEqual((once['experience'][0]['end'], once['experience'][0]['location']), ('Present', 'Pune'))
        self.assertEqual(once['skills'], ['react.js', 'Redux'], 'no duplicate skill')
        self.assertEqual(once['location'], 'Pune', 'an explicitly typed field replaces a quote from the note')
        self.assertEqual(merge_attested(once, typed), once)

    def test_a_note_without_jobs_is_read_when_jobs_were_typed(self):
        note = 'Kiran Example\n+91 90000 00000\nSkills\nTypeScript\nEnjoys hackathons.'
        extracted = {**from_note(experience=[], education=[]), 'skills': ['TypeScript'], 'headline': '', 'location': '',
                     'linkedin': '', 'github': '', 'portfolio': '', 'current_company': '', 'summary': ''}
        typed = {'experience': [{'company': 'Example Works', 'position': 'Frontend Developer', 'start': 'July 2023',
                                 'end': 'Present', 'location': '', 'bullets': ['Built React.js screens.']}],
                 'skills': ['React.js']}
        with patch('resume_bot.automation.extract_profile', return_value=extracted):
            with self.assertRaises(ExtractionError):
                source_profile(note, object())  # alone, a note needs work or education
            profile = source_profile(note, object(), attested=typed)
        self.assertEqual([(e['company'], e['bullets']) for e in profile['experience']],
                         [('Example Works', ['Built React.js screens.'])])
        self.assertEqual(profile['skills'], ['TypeScript', 'React.js'], 'the note skill and the typed skill')


if __name__ == '__main__':
    unittest.main()

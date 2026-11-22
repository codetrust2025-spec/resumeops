"""AI Edit: corrections through chat and tailoring to a job description, approved before anything changes."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from resume_bot.ai_edit import EditError, apply_edits, corrections, find_candidates, tailor
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'a' * 64


def bhavana():
    return validate_profile({
        'full_name': 'Bhavana Example', 'phone': '+91 90000 00021', 'email': 'bhavana@example.test',
        'skills': ['Java', 'Spring Boot', 'MySQL', 'Git'],
        'experience': [{'position': 'Java Developer', 'company': 'Example Works', 'start': '2024', 'end': 'Present',
                        'bullets': ['Built REST APIs with Spring Boot for the order service.',
                                    'Wrote MySQL queries for the order reports.']}],
        'education': [{'degree': 'B.Tech in Computer Science', 'institution': 'Example University',
                       'start': '2020', 'end': '2024'}],
    })


JD = ('Java Backend Developer. We are hiring a Java Backend Developer to build microservices with Spring Boot, '
      'Kafka and Docker on AWS. You will write MySQL queries and REST APIs.')


class AiEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_roles()

    def test_the_graduation_year_is_changed_only_in_education(self):
        edits = corrections(bhavana(), "Change Bhavana's graduation year from 2024 to 2026.")
        self.assertEqual(edits, [{'path': 'education.0.end', 'label': 'End (Example University)', 'old': '2024',
                                  'new': '2026'}], 'the job that started in 2024 is untouched')
        facts, role, notes = apply_edits(bhavana(), edits)
        self.assertEqual(facts['education'][0]['end'], '2026')
        self.assertEqual(facts['experience'][0]['start'], '2024')
        self.assertIsNone(role)
        self.assertIn('2024 -> 2026', notes[0])

    def test_the_candidate_is_found_by_name(self):
        jobs = [{'id': '1', 'candidate': 'Bhavana Example'}, {'id': '2', 'candidate': 'Kiran Sample'},
                {'id': '3', 'candidate': 'Bhavana Example'}]
        self.assertEqual([j['id'] for j in find_candidates(jobs, "Change Bhavana's graduation year")], ['1'],
                         'the newest version of the named candidate')
        self.assertEqual(find_candidates(jobs, 'Change the graduation year'), [])
        both = [{'id': '1', 'candidate': 'Bhavana Rao'}, {'id': '2', 'candidate': 'Bhavana Iyer'}]
        self.assertEqual(len(find_candidates(both, 'Change Bhavana phone to +91 90000 00099')), 2, 'asked to choose')

    def test_skills_are_added_and_removed_only_as_asked(self):
        added = corrections(bhavana(), 'Add Docker to the skills')
        self.assertEqual([(e['path'], e['new']) for e in added], [('skills.+', 'Docker')])
        removed = corrections(bhavana(), 'Remove Git from skills')
        self.assertEqual([(e['path'], e['old']) for e in removed], [('skills.3', 'Git')])
        facts, _, _ = apply_edits(bhavana(), added + removed)
        self.assertEqual(facts['skills'], ['Java', 'Spring Boot', 'MySQL', 'Docker'])

    def test_a_stale_or_invalid_change_is_refused(self):
        with self.assertRaises(EditError):
            apply_edits(bhavana(), [{'path': 'education.0.end', 'old': '2023', 'new': '2026'}])
        with self.assertRaises(EditError):
            apply_edits(bhavana(), [{'path': 'full_name', 'old': 'Bhavana Example', 'new': ''}])
        with self.assertRaises(EditError):
            apply_edits(bhavana(), [{'path': 'summary', 'old': '', 'new': 'Invented'}])
        with self.assertRaises(EditError):
            corrections(bhavana(), 'Change the degree from M.Tech to PhD')

    def test_tailoring_uses_verified_facts_only(self):
        result = tailor(bhavana(), JD, 'Java Developer')
        self.assertEqual(result['role'], 'Java Backend Developer')
        for term in ('Spring Boot', 'MySQL'):
            self.assertIn(term, result['keywords'])
        for term in ('Kafka', 'Docker'):
            self.assertIn(term, result['gaps'])
            self.assertNotIn(term, result['keywords'])
        facts, role, _ = apply_edits(bhavana(), result['edits'])
        self.assertEqual(role, 'Java Backend Developer')
        self.assertEqual(sorted(facts['skills']), sorted(bhavana()['skills']), 'reordered, nothing added')
        self.assertNotIn('Kafka', json.dumps(facts))


    def test_a_title_variation_names_the_role_and_projects_lead_with_job_keywords(self):
        facts = validate_profile({**bhavana(), 'projects': ['Notes App - Flutter mobile notes app.',
                                                           'Library Manager - Spring Boot and MySQL application.']})
        result = tailor(facts, 'Full Stack Engineer wanted. Build Spring Boot services with MySQL and React screens.',
                        'Java Developer')
        self.assertEqual(result['role'], 'Full Stack Developer')
        order = next(e for e in result['edits'] if e['path'] == 'projects')
        self.assertTrue(order['new'][0].startswith('Library Manager'))


class AiEditServerTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = AutomationJobs(Workspace(Path(self.temp.name), code_root=ROOT))
        self.jobs.start = lambda: None
        with closing(self.jobs.connection()) as db, db:
            db.execute("INSERT INTO automation(owner,id,status,created,updated,role,candidate,facts) "
                       "VALUES (?,?,'handed_off',1,1,'Java Developer','Bhavana Example',?)",
                       (OWNER, 'e' * 64, json.dumps(bhavana())))

    def row(self, ident):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, ident)).fetchone()

    def test_preview_then_apply_builds_a_new_version_and_keeps_the_original(self):
        preview = self.jobs.edit_preview(OWNER, "Change Bhavana's graduation year from 2024 to 2026")
        self.assertEqual(preview['id'], 'e' * 64)
        self.assertEqual(json.loads(self.row('e' * 64)['facts'])['education'][0]['end'], '2024', 'preview changes nothing')
        new = self.jobs.edit_apply(OWNER, preview['id'], preview['edits'])
        self.assertNotEqual(new, 'e' * 64)
        built = self.row(new)
        self.assertEqual(json.loads(built['checkpoints'])['facts']['education'][0]['end'], '2026')
        self.assertEqual(json.loads(built['payload'])['regenerated_from'], 'e' * 64)
        self.assertIn('2024 -> 2026', built['notice'])
        self.assertEqual(json.loads(self.row('e' * 64)['facts'])['education'][0]['end'], '2024', 'original kept')

    def test_a_request_without_a_name_offers_the_resumes_to_choose(self):
        only = self.jobs.edit_preview(OWNER, 'Change the graduation year from 2024 to 2026')
        self.assertEqual(only['id'], 'e' * 64, 'one resume: it is the one, and the change still needs approval')
        with closing(self.jobs.connection()) as db, db:
            db.execute("INSERT INTO automation(owner,id,status,created,updated,role,candidate,facts) "
                       "VALUES (?,?,'handed_off',2,2,'QA Engineer','Kiran Sample',?)",
                       (OWNER, 'f' * 64, json.dumps({**bhavana(), 'full_name': 'Kiran Sample'})))
        result = self.jobs.edit_preview(OWNER, 'Change the graduation year from 2024 to 2026')
        self.assertEqual(sorted(item['candidate'] for item in result['choose']), ['Bhavana Example', 'Kiran Sample'])

    def add_resume(self, ident, candidate, role, updated):
        with closing(self.jobs.connection()) as db, db:
            db.execute("INSERT INTO automation(owner,id,status,created,updated,role,candidate,facts) "
                       "VALUES (?,?,'handed_off',?,?,?,?,?)",
                       (OWNER, ident, updated, updated, role, candidate,
                        json.dumps({**bhavana(), 'full_name': candidate})))

    def test_per_resume_ai_edit_targets_that_exact_resume(self):
        self.add_resume('f' * 64, 'Kiran Sample', 'QA Engineer', 5)
        result = self.jobs.edit_preview(OWNER, "Change Kiran's graduation year from 2024 to 2026", 'e' * 64)
        self.assertNotIn('choose', result)
        self.assertEqual((result['id'], result['candidate']), ('e' * 64, 'Bhavana Example'),
                         'the resume AI Edit was opened from, whatever name the request mentions')

    def test_global_ai_edit_finds_the_candidate_and_the_resume_by_role(self):
        self.add_resume('1' * 64, 'Bhavana Example', 'DevOps Engineer', 5)
        self.add_resume('2' * 64, 'Bhavana Example', 'DevOps Engineer', 9)  # a newer version of that resume
        both = self.jobs.edit_preview(OWNER, "Change Bhavana's graduation year from 2024 to 2026")
        self.assertEqual(sorted(item['role'] for item in both['choose']), ['DevOps Engineer', 'Java Developer'],
                         'two resumes of the candidate: asked which one')
        devops = self.jobs.edit_preview(OWNER, "Change Bhavana's DevOps Engineer resume: graduation year from 2024 to 2026")
        self.assertEqual(devops['id'], '2' * 64, 'the newest version of the named role')
        self.assertEqual(devops['versions'], 2)

    def test_regenerating_for_another_role_is_previewed_and_applied_as_a_new_version(self):
        preview = self.jobs.edit_preview(OWNER, 'Regenerate Bhavana as a DevOps Engineer')
        self.assertEqual(preview['edits'], [{'path': 'role', 'label': 'Target role (regenerated for it)',
                                             'old': 'Java Developer', 'new': 'DevOps Engineer'}])
        new = self.jobs.edit_apply(OWNER, preview['id'], preview['edits'])
        self.assertEqual(json.loads(self.row(new)['payload'])['role'], 'DevOps Engineer')
        self.assertEqual(self.row('e' * 64)['role'], 'Java Developer', 'the original stays')

    def test_another_owner_cannot_edit(self):
        with self.assertRaises(EditError):
            self.jobs.edit_preview('b' * 64, 'Change the graduation year from 2024 to 2026', 'e' * 64)
        with self.assertRaises(EditError):
            self.jobs.edit_apply('b' * 64, 'e' * 64, [{'path': 'education.0.end', 'old': '2024', 'new': '2026'}])

    def test_job_description_keywords_reach_the_new_version(self):
        preview = self.jobs.edit_preview(OWNER, '', 'e' * 64, JD)
        new = self.jobs.edit_apply(OWNER, 'e' * 64, preview['edits'], preview['keywords'])
        payload = json.loads(self.row(new)['payload'])
        self.assertEqual(payload['role'], 'Java Backend Developer')
        self.assertIn('Spring Boot', payload['jd_keywords'])
        self.assertNotIn('Kafka', payload['jd_keywords'])


if __name__ == '__main__':
    unittest.main()

"""Every job gets AI drafts, however many jobs the documents list: the model is asked about at most two jobs at a
time (asked for four jobs at once, the local model left the oldest one without a line) and sees the lines the other
jobs already have. Fictional candidate; the model is faked."""
import json
import unittest
from unittest.mock import patch

from resume_bot.ai_review import role_intelligence
from resume_bot.generate import BROKEN_WORDING, _same_task, draft_role_content, plain_words

# Twelve distinct lines (three per job), each naming a Java Backend technology.
LINES = ['Built Spring Boot REST endpoints for a booking form and validated request fields before saving to MySQL.',
         'Configured Hibernate entity mappings so parent records loaded child rows through lazy joins.',
         'Wrote JUnit tests for refund calculation rules while mocking repository calls with Mockito.',
         'Published status messages to Kafka topics and consumed each message in a separate listener application.',
         'Cached catalogue lookups in Redis with expiry settings chosen for read-heavy product screens.',
         'Packaged the pricing module as a Docker image and ran the module with Docker Compose during integration checks.',
         'Set up Jenkins pipeline stages that compiled, tested and archived the loyalty service artifacts.',
         'Designed PostgreSQL indexes for warehouse stock queries after reviewing slow statements in execution plans.',
         'Secured supplier administration APIs with Spring Security roles and token validation filters.',
         'Exposed booking search through Spring MVC controllers returning paged JSON responses to browser pages.',
         'Migrated ledger reports from handwritten JDBC queries to Spring Data JPA repositories with derived methods.',
         'Traced ticketing request failures with SLF4J logging in Spring Boot and fixed the faulty mapper classes.']


def target():
    intel = role_intelligence('Java Backend Developer')
    return {'role': intel['role'], 'keywords': intel['terms']}


def candidate(jobs):
    return {'full_name': 'Kiran Example (fictional)', 'skills': [], 'education': [],
            'experience': [{'company': f'Example Company {n} (fictional)', 'position': '', 'start': f'Jan {2020 - 2 * n}',
                            'end': f'Dec {2021 - 2 * n}', 'location': '', 'bullets': []} for n in range(jobs)]}


class Model:
    def __init__(self):
        self.asked = []

    def __call__(self, ai, prompt, **_):
        facts = json.loads(prompt.split('FACTS:\n', 1)[1].split('\nThe previous draft', 1)[0])
        self.asked.append(facts)
        roles = [{'index': job['index'], 'bullets': LINES[3 * job['index']:3 * job['index'] + 3]}
                 for job in facts['jobs_to_draft']]
        return {'roles': roles, 'skills': ['Java', 'Spring Boot', 'Hibernate', 'JUnit'] if 'Also list' in prompt else []}


class DraftBatchTests(unittest.TestCase):
    def test_four_jobs_are_drafted_two_at_a_time_and_every_job_gets_lines(self):
        model = Model()
        with patch('resume_bot.generate.generate', model):
            work, skills = draft_role_content(candidate(4), target(), 'Java Backend Developer', 9, [0, 1, 2, 3], True,
                                              object())
        self.assertTrue(all(len(facts['jobs_to_draft']) <= 2 for facts in model.asked), model.asked)
        self.assertEqual(sorted(work), [0, 1, 2, 3], 'the oldest job is drafted too')
        self.assertTrue(all(len(lines) >= 3 for lines in work.values()), work)
        self.assertIn('Java', skills)
        later = [facts for facts in model.asked if any(job['index'] >= 2 for job in facts['jobs_to_draft'])]
        self.assertTrue(later and not any('lines_in_other_jobs' in facts for facts in later),
                        'the local model copied the other jobs lines when shown them')
        focus = {job['index']: job['focus_technologies'] for facts in model.asked for job in facts['jobs_to_draft']}
        self.assertEqual(len({tuple(f) for f in focus.values()}), 4, 'each job centres on its own technologies')
        self.assertTrue(all(f[0] == 'Java' for f in focus.values()), 'the leading technology stays in every list')

    def test_two_jobs_are_still_drafted_together(self):
        model = Model()
        with patch('resume_bot.generate.generate', model):
            work, _ = draft_role_content(candidate(2), target(), 'Java Backend Developer', 3, [0, 1], False, object())
        self.assertTrue(all([job['index'] for job in facts['jobs_to_draft']] == [0, 1] for facts in model.asked))
        self.assertEqual(sorted(work), [0, 1])

    def test_a_line_differing_from_another_job_only_in_its_generic_ending_is_offered_once(self):
        base = 'Built Spring Boot REST endpoints for a booking form and validated request fields before saving to MySQL'

        class Echo(Model):
            def __call__(self, ai, prompt, **_):
                facts = json.loads(prompt.split('FACTS:\n', 1)[1].split('\nThe previous draft', 1)[0])
                return {'roles': [{'index': job['index'], 'bullets': (
                    [base + ', ensuring data integrity.'] if job['index'] == 1 else [base + '.'])
                    + LINES[3 * job['index']:3 * job['index'] + 3]} for job in facts['jobs_to_draft']], 'skills': []}

        with patch('resume_bot.generate.generate', Echo()):
            work, _ = draft_role_content(candidate(2), target(), 'Java Backend Developer', 3, [0, 1], False, object())
        printed = [line.rstrip('.') for lines in work.values() for line in lines]
        self.assertEqual(printed.count(base), 1, work)


class WordingTests(unittest.TestCase):
    def test_the_same_task_in_other_words_is_recognised(self):
        self.assertTrue(_same_task('Configured Redux Toolkit to manage complex state with slices.',
                                   ['Configured Redux Toolkit with slices for managing complex state.']))
        self.assertFalse(_same_task(LINES[0], [LINES[8]]))

    def test_filler_removal_that_breaks_a_sentence_is_caught(self):
        for text in ('Managed dependencies with npm, ensuring a robust and current project setup.',
                     'Optimized builds with Vite, leveraging its fast development server.'):
            self.assertTrue(BROKEN_WORDING.search(plain_words(text)), plain_words(text))
        for text in LINES:
            self.assertFalse(BROKEN_WORDING.search(plain_words(text)), text)


if __name__ == '__main__':
    unittest.main()

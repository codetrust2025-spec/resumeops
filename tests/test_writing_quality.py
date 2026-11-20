"""Writing quality of AI drafts: jobs get their full number of lines, generic endings are trimmed from drafts, and a
suggested project gets its five bullets.

Before: the local model sometimes left a job at four of its seven lines after the one retry; fill lines kept endings
such as ", ensuring data integrity."; and a proposal was accepted with three of its five requested bullets. Every
candidate here is fictional and the model is faked."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot.generate import draft_role_content, propose_projects, without_generic_tail

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also when run as tests.test_writing_quality
import test_experience_depth as depth  # noqa: E402  (a module import, so its test classes are not collected twice)

arun, role = depth.arun, depth.role

GCP_LINES = [
    'Built BigQuery views that join order and inventory tables for the operations reporting dashboards.',
    'Developed Dataflow pipelines with Apache Beam to clean event data before loading it into BigQuery.',
    'Configured Pub/Sub topics to stream frontend activity events into Cloud Storage for later analysis.',
    'Wrote Python jobs on Cloud Composer that refresh BigQuery tables behind the internal dashboards.',
    'Tested Dataflow transformations with Python unit tests before releasing changes to the data pipelines.',
    'Scheduled BigQuery exports to Cloud Storage with Cloud Composer for the finance team every night.',
    'Partitioned BigQuery tables by event date so the dashboard queries scan only the days they show.',
]

ASHA = {'full_name': 'Asha Example', 'skills': ['Python', 'SQL', 'Django', 'Kubernetes'],
        'experience': [{'company': 'Example Cloud Pvt Ltd', 'position': 'Senior Backend Engineer', 'start': 'Jan 2022',
                        'end': 'Present', 'location': '', 'bullets': ['Built payment APIs in Python and PostgreSQL.']}],
        'education': []}


class GenericEndings(unittest.TestCase):
    def test_a_generic_closing_clause_is_trimmed_when_a_full_sentence_remains(self):
        self.assertEqual(without_generic_tail('Built Dataflow pipelines with Apache Beam to load order events into '
                                              'BigQuery, ensuring data integrity.'),
                         'Built Dataflow pipelines with Apache Beam to load order events into BigQuery.')

    def test_a_short_sentence_keeps_its_clause_and_other_lines_are_unchanged(self):
        short = 'Configured Cloud Composer for ETL, reducing manual work.'
        self.assertEqual(without_generic_tail(short), short)
        self.assertEqual(without_generic_tail('Automated data ingestion processes using Dataflow, reducing manual data '
                                              'entry.'), 'Automated data ingestion processes using Dataflow.')
        self.assertEqual(without_generic_tail(GCP_LINES[0]), GCP_LINES[0])


class FullLineCount(unittest.TestCase):
    def test_a_job_still_short_after_the_retry_is_asked_once_more(self):
        replies = [GCP_LINES[:2], GCP_LINES[2:4], GCP_LINES[4:6]]
        prompts = []

        def model(ai, prompt, **_):
            prompts.append(prompt)
            return {'roles': [{'index': 0, 'bullets': replies.pop(0)}], 'skills': []}
        with patch('resume_bot.generate.generate', model):
            work, _ = draft_role_content(arun(), role('GCP Data Engineer'), 'GCP Data Engineer', 3, [0], False, object())
        self.assertEqual(len(prompts), 3)
        self.assertEqual(work[0], GCP_LINES[:6], 'the lines of all three attempts (two after one retry, before)')

    def test_a_fill_line_loses_its_generic_ending(self):
        weak = GCP_LINES[2].rstrip('.') + ', ensuring data quality.'
        with patch('resume_bot.generate.generate', lambda ai, prompt, **_: {
                'roles': [{'index': 0, 'bullets': [*GCP_LINES[:2], weak]}], 'skills': []}):
            work, _ = draft_role_content(arun(), role('GCP Data Engineer'), 'GCP Data Engineer', 3, [0], False, object())
        self.assertIn(GCP_LINES[2], work[0])
        self.assertFalse(any('ensuring' in line for line in work[0]))


class ProjectBullets(unittest.TestCase):
    MORE = ['Added Django REST endpoints that list the reservations of each account from PostgreSQL tables.',
            'Wrote a Kubernetes readiness probe for the Django services that checks the PostgreSQL connection first.',
            'Developed Django services with REST APIs for service communication.',  # repeats a bullet: dropped
            'Cached the reservation search results in Redis keyed by route and travel date for repeat lookups.']

    def propose(self, *responses):
        prompts, replies = [], list(responses)

        def model(ai, prompt, **_):
            prompts.append(prompt)
            reply = replies.pop(0)
            if prompt.startswith('Complete these portfolio project proposals'):
                return reply
            context = {'problem': 'Fictional practice.', 'architecture': 'Service.', 'features': ['Orders'],
                       'responsibilities': 'Backend.'}
            return {'projects': [{**context, **item} for item in reply]}
        with patch('resume_bot.generate.generate', model):
            kept = propose_projects(ASHA, role('Python Backend Developer'), 'Python Backend Developer', 7,
                                    object())
        return kept, prompts

    def test_a_short_proposal_is_completed_to_five_checked_bullets(self):
        kept, prompts = self.propose([depth.ProposalTests.SHORT, depth.ProposalTests.THIRD],
                                     {'projects': [{'index': 0, 'bullets': self.MORE}, {'index': 1, 'bullets': []}]})
        self.assertEqual(len(prompts), 2)
        self.assertIn('"existing_bullets"', prompts[1])
        microservices = kept[0]
        self.assertEqual(len(microservices['bullets']), 5)
        self.assertEqual(microservices['bullets'][3:], self.MORE[:2])
        self.assertNotIn(self.MORE[3], microservices['bullets'], 'five at most')
        self.assertEqual(len(kept[1]['bullets']), 3, 'nothing usable came back: the proposal keeps its bullets')

    def test_a_failed_top_up_never_loses_a_proposal(self):
        kept, prompts = self.propose([depth.ProposalTests.SHORT, depth.ProposalTests.THIRD])  # the top-up call fails
        self.assertEqual([p['name'] for p in kept], ['Microservices Ecosystem', 'Order Reporting Service'])
        self.assertEqual(len(prompts), 2)


if __name__ == '__main__':
    unittest.main()

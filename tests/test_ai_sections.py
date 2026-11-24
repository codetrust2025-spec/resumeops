"""Ollama writes every resume section; Python accepts an answer only when it adds no fact, loses none and repeats
nothing (resume_bot.ai_sections). A rejected answer is asked once more, then the verified text is kept."""
import unittest

from resume_bot.ai_sections import (additions, coverage, rewrite_problems, write_education, write_experience,
                                    write_objective, write_project, write_sections, write_skill_groups)

JOB = {'company': 'LTM', 'position': 'Senior Data Engineer', 'start': 'Mar 2022', 'end': 'Present',
       'location': 'Hyderabad', 'bullets': [
           'Configured Azure Data Factory pipelines to automate data ingestion from on-premises sources.',
           'Built automated data ingestion pipelines using Azure Data Factory to synchronize on-premises databases.',
           'Developed PySpark scripts to process large datasets in Azure Databricks clusters.']}
PROJECT = ('CAF - Tech stack: Azure Migrate | Handled end-to-end cloud infrastructure migration, Azure VM '
           'provisioning, network configuration, replication, cutover, and post-migration validation using '
           'Azure Migrate and ASR.')


class Scripted:
    """Answers each request with the next scripted reply and keeps the prompts."""
    model = 'scripted'

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class FactChecks(unittest.TestCase):
    def test_rephrasing_passes_and_new_facts_fail(self):
        source = JOB['bullets'][2]
        self.assertEqual(additions(source, 'Developed PySpark scripts that process large datasets on Azure '
                                           'Databricks clusters.', source), '')
        self.assertIn('Kubernetes', additions(source, 'Developed PySpark scripts on Kubernetes.', source))
        self.assertIn('40', additions(source, 'Developed 40 PySpark scripts in Azure Databricks.', source))
        self.assertIn('Led', additions(source, 'Led PySpark script development in Azure Databricks.', source))

    def test_lost_technology_is_reported(self):
        self.assertEqual(coverage([JOB['bullets'][2]], ['Developed scripts for large datasets.']),
                         ['azur', 'databrick', 'pyspark'])


class Experience(unittest.TestCase):
    def test_duplicates_merge_into_one_professional_line(self):
        ai = Scripted({'lines': [
            {'text': 'Configured Azure Data Factory pipelines to automate data ingestion from on-premises sources '
                     'and synchronize on-premises databases', 'from': [0, 1]},
            {'text': 'Developed PySpark scripts to process large datasets in Azure Databricks clusters', 'from': [2]}]})
        lines, problems = write_experience(JOB, [], 'Azure Data Engineer', ai)
        self.assertEqual(problems, [])
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith('.') for line in lines))

    def test_invented_tool_is_rejected_then_retried(self):
        bad = {'lines': [{'text': 'Configured Azure Data Factory and Snowflake pipelines.', 'from': [0, 1]},
                         {'text': JOB['bullets'][2], 'from': [2]}]}
        ai = Scripted(bad, bad)
        lines, problems = write_experience(JOB, [], 'Azure Data Engineer', ai)
        self.assertIsNone(lines)
        self.assertTrue(any('Snowflake' in p for p in problems))
        self.assertTrue(ai.prompts[1].startswith('IMPORTANT: YOUR PREVIOUS ANSWER WAS REJECTED'))

    def test_line_repeating_a_project_is_left_to_the_project(self):
        job = {**JOB, 'bullets': [*JOB['bullets'], 'Handled end-to-end cloud infrastructure migration, Azure VM '
                                  'provisioning, network configuration, replication, cutover, and post-migration '
                                  'validation using Azure Migrate and ASR.']}
        ai = Scripted({'lines': [{'text': JOB['bullets'][0], 'from': [0, 1]}, {'text': JOB['bullets'][2], 'from': [2]}]})
        lines, problems = write_experience(job, [PROJECT.split(' | ', 1)[1]], 'Azure Data Engineer', ai)
        self.assertEqual(problems, [])
        self.assertNotIn('ASR', ' '.join(lines))


class Projects(unittest.TestCase):
    def test_points_from_the_project_text_only(self):
        ai = Scripted({'points': [
            'Handled end-to-end cloud infrastructure migration for CAF using Azure Migrate',
            'Provisioned Azure VM and network configuration',
            'Performed replication and cutover using ASR',
            'Completed post-migration validation for CAF']})
        project, problems = write_project(PROJECT, [], 'Azure Data Engineer', ai)
        self.assertEqual(problems, [])
        self.assertEqual(project.count(' | '), 4)
        self.assertTrue(project.startswith('CAF - Tech stack: Azure Migrate | '))

    def test_invented_result_rejected(self):
        bad = {'points': ['Migrated 200 servers to Azure with zero downtime using Azure Migrate and ASR.']}
        _, problems = write_project(PROJECT, [], 'Azure Data Engineer', Scripted(bad, bad, bad))
        self.assertTrue(problems)


class Skills(unittest.TestCase):
    def test_every_skill_once_in_allowed_categories(self):
        ai = Scripted({'groups': [{'name': 'Azure Data Services', 'items': ['Azure Data Factory', 'azure databricks']},
                                  {'name': 'Programming Languages', 'items': ['PySpark']}]})
        groups, problems = write_skill_groups(['Azure Data Factory', 'Azure Databricks', 'PySpark'],
                                              ['Programming Languages', 'Azure Data Services'], 'Azure Data Engineer', ai)
        self.assertEqual(problems, [])
        self.assertEqual(groups[0]['items'], ['Azure Data Factory', 'Azure Databricks'])  # verified spelling

    def test_added_or_missing_skill_rejected(self):
        bad = {'groups': [{'name': 'Programming Languages', 'items': ['PySpark', 'Scala']}]}
        groups, problems = write_skill_groups(['PySpark', 'SQL'], ['Programming Languages'], 'X', Scripted(bad, bad))
        self.assertIsNone(groups)
        self.assertTrue(any('Scala' in p for p in problems) and any('SQL' in p for p in problems))


class Education(unittest.TestCase):
    def test_casing_cleaned_words_kept(self):
        entry = {'institution': 'rajamahendri institute of engineering and technology', 'degree': 'B.Tech',
                 'start': '', 'end': '2021', 'details': 'Computer science | CGPA 6.5'}
        ai = Scripted({'education': [{'institution': 'Rajamahendri Institute of Engineering and Technology',
                                      'degree': 'B.Tech', 'details': 'Computer Science | CGPA 6.5'}]})
        rows, problems = write_education([entry], ai)
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]['institution'], 'Rajamahendri Institute of Engineering and Technology')

    def test_expansion_rejected(self):
        entry = {'institution': 'RIET', 'degree': 'B.Tech', 'start': '', 'end': '2021', 'details': ''}
        bad = {'education': [{'institution': 'RIET', 'degree': 'Bachelor of Technology', 'details': ''}]}
        rows, problems = write_education([entry], Scripted(bad, bad))
        self.assertIsNone(rows)


class Objective(unittest.TestCase):
    def test_validator_decides_and_word_count_is_fed_back(self):
        ai = Scripted({'objective': 'Too short.'}, {'objective': 'Good text.'})
        check = lambda text: [] if text == 'Good text.' else ['use 40-55 words']
        text, problems = write_objective({}, 'Data Engineer', 'rules', check, ai)
        self.assertEqual((text, problems), ('Good text.', []))
        self.assertIn('yours has 2 words', ai.prompts[1])


class WholeResume(unittest.TestCase):
    def profile(self):
        return {'full_name': 'A B', 'email': '', 'phone': '9999999999', 'location': '', 'linkedin': '',
                'github': '', 'summary': 'old', 'skills': ['PySpark', 'Azure Migrate'], 'certifications': [],
                'experience': [dict(JOB, bullets=list(JOB['bullets']))], 'projects': [PROJECT],
                'education': [{'institution': 'X college', 'degree': 'B.Tech', 'start': '', 'end': '2021',
                               'details': ''}]}

    def test_failed_sections_keep_verified_text_and_watcher_check_passes(self):
        source = self.profile()
        broken = {'lines': [{'text': 'Led Snowflake work.', 'from': [0, 1, 2]}]}
        ai = Scripted(broken, broken,
                      {'points': ['Handled end-to-end cloud infrastructure migration using Azure Migrate',
                                  'Provisioned Azure VM and network configuration',
                                  'Performed replication and cutover using ASR',
                                  'Completed post-migration validation for CAF']},
                      {'groups': [{'name': 'Cloud', 'items': ['Azure Migrate']},
                                  {'name': 'Programming Languages', 'items': ['PySpark']}]},
                      {'education': [{'institution': 'X College', 'degree': 'B.Tech', 'details': ''}]})
        out, report = write_sections(source, {'groups': {'Programming Languages': [], 'Cloud': []}}, 'Data Engineer', ai)
        self.assertEqual(out['experience'][0]['bullets'], JOB['bullets'])
        self.assertEqual(report['sections']['experience_0']['by'], 'verified text kept')
        self.assertEqual(report['sections']['project_0']['by'], 'ollama')
        self.assertEqual(out['skills'], ['Azure Migrate', 'PySpark'])
        self.assertEqual(out['education'][0]['institution'], 'X College')
        self.assertEqual(rewrite_problems(source, out), [])

    def test_watcher_rejects_a_tampered_rewrite(self):
        source = self.profile()
        tampered = self.profile()
        tampered['experience'][0]['bullets'][0] = 'Led a team of 6 engineers on Snowflake.'
        self.assertTrue(rewrite_problems(source, tampered))
        tampered = self.profile()
        tampered['education'][0]['degree'] = 'M.Tech'
        self.assertIn('education degree changed', rewrite_problems(source, tampered))


if __name__ == '__main__':
    unittest.main()

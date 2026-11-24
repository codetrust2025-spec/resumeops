"""Regression coverage from a real audit (an Azure Data Engineer application, re-created here with a fictional name):
skills typed without commas printed as one lower-case "Programming Languages" skill, the same sentence printed as a
job line and as a project line, paraphrased drafts accepted as different work, lower-case names printed as typed, a
third-person Career Objective, and quality checks that scored all of it as passing. All candidates are fictional."""
import unittest
from unittest.mock import patch

from resume_bot.ats import score as ats_score  # noqa: F401  (imported for its module-level patterns)
from resume_bot.facts_quality import (canonical_case, is_placeholder, normalize_facts, smart_title, split_skills,
                                      weak_lines)
from resume_bot.generate import _objective_identity, _same_task, skill_groups, task_themes, write_summary
from resume_bot.standards import standards_report
from resume_bot.workflow import ROLES, analyze, load_roles

CAF = ('Worked on the CAF project as an Azure Cloud Engineer, handling end-to-end cloud infrastructure migration, '
       'Azure VM provisioning, network configuration, replication, cutover, and post-migration validation using '
       'Azure Migrate and ASR.')
DRAFTED = ['Configured Azure Data Factory pipelines to automate data ingestion from on-premises sources.',
           'Monitored pipeline executions and data flows using Azure Data Factory monitoring tools.',
           'Built automated data ingestion pipelines using Azure Data Factory to synchronize on-premises databases.',
           'Developed PySpark scripts to process large datasets in Azure Databricks clusters.']


def audited_facts():
    """The audited application's facts as the form produced them (fictional name)."""
    return {'full_name': 'Gopal Example (fictional)', 'email': 'gopal@example.com', 'phone': '+91 90000 00013',
            'location': 'Hyderabad', 'headline': '', 'summary': '',
            'skills': ['Azure data factory Azure Databricks azure migrate networking pyspark'],
            'experience': [{'company': 'LTM', 'position': 'Senior data engineer', 'start': 'Mar 2022',
                            'end': 'Present', 'location': 'Hyderabad', 'bullets': [CAF]}],
            'projects': ['CAF - Tech stack: Azure data factory, Azure Databricks, azure migrate, networking, pyspark | '
                         + CAF, 'NA - Tech stack: NA | NA'],
            'education': [{'institution': 'Rajamahendri institute of engineering and technology', 'degree': 'B.Tech',
                           'start': '', 'end': '2021', 'details': 'Computer science | CGPA 6.5'}],
            'certifications': []}


class SkillParsingTests(unittest.TestCase):
    def test_space_separated_skills_become_named_technologies(self):
        self.assertEqual(split_skills(['Azure data factory Azure Databricks azure migrate networking pyspark']),
                         ['Azure Data Factory', 'Azure Databricks', 'Azure Migrate', 'Networking', 'PySpark'])
        self.assertEqual(split_skills(['Azure data factory,  databricks, ', 'NA', '']),
                         ['Azure Data Factory', 'Databricks'])
        self.assertEqual(split_skills(['node js, express', 'power automate', 'Java 17']),
                         ['Node.js', 'Express', 'Power Automate', 'Java 17'])

    def test_the_split_skills_fall_into_their_own_categories(self):
        load_roles()
        skills = split_skills(['Azure data factory Azure Databricks azure migrate networking pyspark'])
        groups = {g['name']: g['items'] for g in skill_groups(skills, ROLES['azure-data-engineer'])}
        self.assertEqual(groups['Programming Languages'], ['PySpark'])
        self.assertEqual(groups['Azure Data Services'], ['Azure Data Factory', 'Azure Databricks'])
        self.assertEqual(sum(len(items) for items in groups.values()), 5)

    def test_every_role_reads_its_own_skills_typed_in_lower_case_without_commas(self):
        load_roles()
        for role_id, role in ROLES.items():
            with self.subTest(role=role_id):
                wanted = [items[0] for items in role['resumeGroups'].values()][:4]
                typed = ' '.join(item.casefold() for item in wanted)
                found = split_skills([typed])
                for item in wanted:
                    self.assertIn(item.casefold(), [s.casefold() for s in found], (typed, found))


class NormalizationTests(unittest.TestCase):
    def test_the_audited_facts_are_tidied_without_new_facts(self):
        facts = normalize_facts(audited_facts())
        self.assertEqual(facts['skills'], ['Azure Data Factory', 'Azure Databricks', 'Azure Migrate', 'Networking',
                                           'PySpark'])
        self.assertEqual(facts['experience'][0]['position'], 'Senior Data Engineer')
        self.assertEqual(facts['education'][0]['institution'], 'Rajamahendri Institute of Engineering and Technology')
        self.assertEqual(facts['education'][0]['details'], 'Computer Science | CGPA 6.5')
        # The sentence typed both as work and as the project's description is printed once, with the project.
        self.assertEqual(facts['experience'][0]['bullets'], [])
        self.assertEqual([p.split(' - ')[0] for p in facts['projects']], ['CAF'])
        self.assertIn(CAF, facts['projects'][0])
        self.assertEqual(normalize_facts(facts), facts, 'idempotent')
        self.assertEqual(facts['experience'][0]['company'], 'LTM')

    def test_only_case_changes_and_deliberate_spellings_stay(self):
        self.assertEqual(smart_title('senior data engineer'), 'Senior Data Engineer')
        self.assertEqual(smart_title('sde ii'), 'SDE II')
        self.assertEqual(smart_title('B.Tech in computer science'), 'B.Tech in Computer Science')
        self.assertEqual(smart_title('iOS developer'), 'iOS Developer')
        self.assertEqual(canonical_case('Migrated servers using azure migrate and pyspark for internal dashboards.'),
                         'Migrated servers using Azure Migrate and PySpark for internal dashboards.')
        self.assertEqual(canonical_case('Built streaming pipelines with ReactJS.'),
                         'Built streaming pipelines with ReactJS.')
        # Seen live: "Set up Networking configurations ..." (a practice recased mid-sentence).
        self.assertEqual(canonical_case('Set up networking configurations for secure data transfer.'),
                         'Set up networking configurations for secure data transfer.')
        for value in ('NA', 'n/a', ' - ', 'None', 'nil'):
            self.assertTrue(is_placeholder(value), value)
        self.assertFalse(is_placeholder('NAS backups'))


class RepetitionTests(unittest.TestCase):
    def test_paraphrased_work_is_one_task_and_different_work_is_not(self):
        self.assertTrue(_same_task(DRAFTED[2], [DRAFTED[0]]))
        self.assertFalse(_same_task(DRAFTED[1], [DRAFTED[0]]))  # monitoring is not ingestion
        self.assertFalse(_same_task(DRAFTED[3], [DRAFTED[0]]))
        self.assertFalse(_same_task('Built Angular forms with reactive validation and TypeScript models.',
                                    ['Built reusable React.js components with TypeScript for internal forms.']))
        self.assertEqual(task_themes(DRAFTED[0]), {'ingestion'})

    def test_ats_and_standards_no_longer_pass_repeated_and_weak_work(self):
        load_roles()
        facts = normalize_facts(audited_facts())
        facts['experience'][0]['bullets'] = [*DRAFTED, CAF]  # what the audited resume printed
        facts['skill_groups'] = skill_groups(facts['skills'], ROLES['azure-data-engineer'])
        target = {'role': 'azure-data-engineer', 'template': 'standard', 'experience_years': 4}
        report = analyze(facts, target)
        quality = next(c for c in report['categories'] if c['key'] == 'content_quality')
        self.assertLess(quality['points'], quality['max'], 'repeated work loses content-quality points')
        standards = standards_report(facts, target, 4, report, {'pages': 1, 'searchable': True})
        checks = {c['id']: c for c in standards['checks']}
        self.assertFalse(checks['experience']['ok'])
        self.assertIn('repeat other work', checks['experience']['detail'])
        self.assertIn('open weakly', checks['experience']['detail'])
        self.assertFalse(checks['projects']['ok'], checks['projects']['detail'])
        self.assertFalse(standards['verified'])
        self.assertEqual(weak_lines([CAF, DRAFTED[0]]), [CAF])


class FirstPersonObjectiveTests(unittest.TestCase):
    def test_the_audited_role_reads_in_the_first_person(self):
        facts = normalize_facts(audited_facts())
        with patch('resume_bot.generate.generate', side_effect=RuntimeError('model offline')):
            text, _ = write_summary(facts, 'Azure Data Engineer', 4, [], object())
        self.assertTrue(text.startswith('I am a professional seeking an Azure Data Engineer role focused on '), text)
        self.assertNotRegex(text, r'\b(?:candidate|Aims|Seeks|Values)\b')
        self.assertEqual(_objective_identity({'experience': []}, 'Data Analyst', 'first'), 'I am an aspiring Data Analyst')
        held = {'experience': [{'position': 'Azure Data Engineer'}]}
        self.assertEqual(_objective_identity(held, 'Azure Data Engineer', 'first'), 'I am an Azure Data Engineer')


if __name__ == '__main__':
    unittest.main()

"""AI-first intake tests: only fourteen categories, never silently publish AI claims."""
import unittest

from resume_bot.ai_first import INPUT_CATEGORIES, IntakeError, suggest_sections, validate_intake
from resume_bot.ai_review import approved_resume, claims_for_review, role_intelligence


class FakeAI:
    def generate(self, prompt, schema=None):
        self.prompt = prompt
        return {
            'professional_summary': 'Frontend developer with React expertise',
            'technical_skills': ['React', 'TypeScript', 'AWS'],
            'core_competencies': ['Responsive UI', 'API integration'],
            'experience_responsibilities': [
                {'experience_index': 0, 'bullets': ['Architected a platform']},
                {'experience_index': 8, 'bullets': ['Invented a position']},
            ],
            'project_writeups': [
                {'project_index': 3, 'bullets': ['Built cloud platform']}],
            'project_ideas': [
                {'title': 'Portfolio Dashboard', 'description': 'A suggested learning project'},
                {'title': 'Task Tracker', 'description': 'Another portfolio idea'}],
            'key_achievements': ['Reduced latency by 90%'],
            'certification_recommendations': ['AWS Certified Developer'],
        }


def candidate():
    return {
        'role': 'Frontend Developer', 'years_of_experience': 2,
        'full_name': 'Example Candidate', 'email': 'example@example.com',
        'phone': '', 'location': 'Hyderabad', 'current_company': '',
        'linkedin': '', 'github': '', 'portfolio': '',
        'work_experience': [
            {'company': 'Example Company', 'position': 'Developer', 'start': 'Jan 2024',
             'end': 'Present'}],
        'education': [
            {'degree': 'B.Tech', 'specialization': 'IT', 'university': 'Example University',
             'graduation_year': '2023', 'cgpa': '7.8'}],
        'projects': [], 'coding_platforms': [],
    }


class AIIntakeContract(unittest.TestCase):
    def test_only_fourteen_categories_are_accepted(self):
        actual = validate_intake(candidate())
        self.assertEqual(set(actual), set(INPUT_CATEGORIES))
        for prohibited in ('summary', 'skills', 'certifications', 'achievements', 'keywords'):
            bad = candidate()
            bad[prohibited] = ''
            with self.assertRaises(IntakeError):
                validate_intake(bad)

    def test_no_manual_achievement_bullets_required(self):
        basic = candidate()
        self.assertEqual(validate_intake(basic)['work_experience'][0]['company'], 'Example Company')
        bad = candidate()
        bad['work_experience'][0]['bullets'] = ['Candidate had to write this']
        with self.assertRaises(IntakeError):
            validate_intake(bad)

    def test_generated_claims_always_require_review(self):
        ai = FakeAI()
        result = suggest_sections(candidate(), ai, market_terms=['React', 'AWS'])
        self.assertFalse(result['ready_for_generation'])
        self.assertTrue(result['approval_required'])
        self.assertFalse(result['market_terms_verified_as_candidate_skills'])
        self.assertEqual(result['suggestions']['technical_skills']['status'],
                         'requires_candidate_confirmation')
        self.assertEqual(result['suggestions']['key_achievements']['status'],
                         'requires_evidence_and_confirmation')
        self.assertEqual(result['suggestions']['certification_recommendations']['status'],
                         'learning_recommendations_not_earned')
        self.assertEqual(result['suggestions']['project_ideas'][0]['status'],
                         'portfolio_idea_not_completed')
        self.assertEqual(len(result['suggestions']['experience_responsibilities']), 1)
        self.assertEqual(result['suggestions']['project_writeups'], [])
        self.assertIn('never evidence of candidate experience', ai.prompt.lower())

    def test_unverified_resume_keyword_is_never_claimed_as_skill(self):
        result = suggest_sections(candidate(), FakeAI(), market_terms=['AWS'])
        self.assertIn('AWS', result['suggestions']['technical_skills']['items'])
        self.assertFalse(result['market_terms_verified_as_candidate_skills'])
        self.assertFalse(result['ready_for_generation'])

    def test_curated_terms_have_no_external_provider_attribution(self):
        intelligence = role_intelligence('frontend-developer')
        self.assertEqual(intelligence['sources'], [
            {'label': 'ResumeOps role catalog (curated)', 'url': '', 'terms': intelligence['terms']},
        ])

    def test_provenance_requires_source_match_and_approval_excludes_unsupported_skill(self):
        intake = {**candidate(), 'role': 'frontend-developer'}
        draft = suggest_sections(intake, FakeAI(), market_terms=['AWS'])
        draft['source'] = 'Skills: React\nOther Company\nArchitected a platform'
        claims = {claim['text']: claim for claim in claims_for_review(draft)}
        self.assertEqual(claims['React']['provenance'], 'verified_from_source')
        self.assertEqual(claims['AWS']['provenance'], 'unsupported_role_keyword')
        self.assertEqual(claims['Architected a platform']['provenance'], 'unverified_suggestion')
        picked = {claims['React']['id']: 'React'}
        reviewed = approved_resume(draft, {'attested': True, 'claims': picked})
        self.assertEqual(reviewed['profile']['skills'], ['React'])
        self.assertEqual(reviewed['approved_claims'][0]['provenance'], 'verified_from_source')
        picked[claims['AWS']['id']] = 'AWS'
        reviewed = approved_resume(draft, {'attested': True, 'claims': picked})
        self.assertEqual(reviewed['profile']['skills'], ['React', 'AWS'])
        self.assertEqual(reviewed['approved_claims'][1]['provenance'], 'unsupported_role_keyword')
        self.assertTrue(reviewed['approved_claims'][1]['candidate_confirmed'])

    def test_invalid_intake_is_rejected(self):
        for patch in ({'email': 'not an email'}, {'years_of_experience': -1},
                      {'years_of_experience': '4'}, {'full_name': ''}):
            data = candidate()
            data.update(patch)
            with self.assertRaises(IntakeError):
                validate_intake(data)


class ScriptedAI:
    """Returns the given entries, as qwen2.5:7b did in production on 18 Nov 2026."""
    def __init__(self, responsibilities, writeups):
        self.raw = {'professional_summary': 'Engineer', 'technical_skills': [],
                    'core_competencies': [], 'experience_responsibilities': responsibilities,
                    'project_writeups': writeups, 'project_ideas': [], 'key_achievements': [],
                    'certification_recommendations': []}

    def generate(self, prompt, schema=None):
        self.prompt = prompt
        return self.raw


def two_jobs_one_project():
    data = candidate()
    data['years_of_experience'] = 10
    data['work_experience'] = [
        {'company': 'Current Corp', 'position': 'Lead Engineer', 'start': 'Jan 2021', 'end': 'Present'},
        {'company': 'Earlier Labs', 'position': 'Engineer', 'start': 'Jul 2016', 'end': 'Dec 2020'}]
    data['projects'] = [{'name': 'Demand forecasting platform'}]
    return data


class AIEntryIndexes(unittest.TestCase):
    def test_one_based_index_for_single_job_and_project_is_kept(self):
        ai = ScriptedAI([{'experience_index': 1, 'bullets': ['Maintained regression suites']}],
                        [{'project_index': 1, 'bullets': ['Wrote checkout tests']}])
        result = suggest_sections(candidate() | {'projects': [{'name': 'Checkout suite'}]}, ai)
        self.assertEqual(result['suggestions']['experience_responsibilities'][0]['experience_index'], 0)
        self.assertEqual(result['suggestions']['project_writeups'][0]['project_index'], 0)
        self.assertIn('ZERO-BASED', ai.prompt)
        self.assertIn('"0": "Checkout suite"', ai.prompt)

    def test_echoed_company_decides_the_employer(self):
        ai = ScriptedAI([{'experience_index': 1, 'company': 'Current Corp', 'bullets': ['Led design']},
                         {'experience_index': 2, 'company': ' earlier  labs ', 'bullets': ['Built models']}],
                        [{'project_index': 1, 'project_name': 'Demand forecasting platform',
                          'bullets': ['Forecasting service']}])
        suggestions = suggest_sections(two_jobs_one_project(), ai)['suggestions']
        self.assertEqual([(e['experience_index'], e['suggestions'])
                          for e in suggestions['experience_responsibilities']],
                         [(0, ['Led design']), (1, ['Built models'])])
        self.assertEqual(suggestions['project_writeups'][0]['project_index'], 0)

    def test_one_based_list_without_names_is_shifted(self):
        ai = ScriptedAI([{'experience_index': 1, 'bullets': ['Led design']},
                         {'experience_index': 2, 'bullets': ['Built models']}], [])
        entries = suggest_sections(two_jobs_one_project(), ai)['suggestions']['experience_responsibilities']
        self.assertEqual([e['experience_index'] for e in entries], [0, 1])

    def test_zero_based_list_is_unchanged_and_out_of_range_dropped(self):
        ai = ScriptedAI([{'experience_index': 0, 'bullets': ['Led design']},
                         {'experience_index': 1, 'bullets': ['Built models']},
                         {'experience_index': 5, 'company': 'Invented Inc', 'bullets': ['Invented']}], [])
        entries = suggest_sections(two_jobs_one_project(), ai)['suggestions']['experience_responsibilities']
        self.assertEqual([e['experience_index'] for e in entries], [0, 1])


if __name__ == '__main__':
    unittest.main()

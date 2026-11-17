"""AI-first intake tests: only fourteen categories, never silently publish AI claims."""
import unittest

from resume_bot.ai_first import INPUT_CATEGORIES, IntakeError, suggest_sections, validate_intake


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
        self.assertIn('not candidate facts', ai.prompt.lower())

    def test_unverified_resume_keyword_is_never_claimed_as_skill(self):
        result = suggest_sections(candidate(), FakeAI(), market_terms=['AWS'])
        self.assertIn('AWS', result['suggestions']['technical_skills']['items'])
        self.assertFalse(result['market_terms_verified_as_candidate_skills'])
        self.assertFalse(result['ready_for_generation'])

    def test_invalid_intake_is_rejected(self):
        for patch in ({'email': 'not an email'}, {'years_of_experience': -1},
                      {'years_of_experience': '4'}, {'full_name': ''}):
            data = candidate()
            data.update(patch)
            with self.assertRaises(IntakeError):
                validate_intake(data)


if __name__ == '__main__':
    unittest.main()

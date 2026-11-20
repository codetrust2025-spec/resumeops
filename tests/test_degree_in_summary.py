"""The candidate's degree in a summary states the education, not work.

Production (2026-11-21, a fictional live check): a Data Analyst with a B.Sc Statistics moving into Angular Developer
got the composed career-change summary "... Holds a B.Sc in Statistics." The watcher's final check read "Statistics"
as a tool tied to work the resume does not describe, so the resume was never published. Every candidate here is
fictional."""
import unittest

from resume_bot.generate import composed_summary, summary_problems
from resume_bot.profile import validate_profile


def analyst(degree):
    return validate_profile({
        'full_name': 'Rhea Example (fictional)', 'phone': '+91 90000 66666',
        'skills': ['TypeScript', 'JavaScript', 'HTML', 'Angular', 'RxJS', 'NgRx'],
        'experience': [{'company': 'Example Insights Pvt Ltd (fictional)', 'position': 'Data Analyst',
                        'start': 'January 2022', 'end': 'Present', 'location': '',
                        'bullets': ['Built Power BI dashboards that track weekly store sales for regional managers.']}],
        'education': [{'institution': 'Example College (fictional)', 'degree': degree, 'start': '', 'end': '2021',
                       'details': ''}]})


class DegreeInSummary(unittest.TestCase):
    def test_the_live_career_change_summary_passes_the_final_check(self):
        text = ('Data Analyst with 4 years of professional experience, moving into an Angular Developer role. '
                'Transferable, hands-on skills include TypeScript, JavaScript, HTML, Angular, RxJS and NgRx. '
                'Holds a B.Sc in Statistics.')
        self.assertEqual(summary_problems(text, analyst('B.Sc Statistics'), 'Angular Developer', 4), [])

    def test_composed_summaries_pass_for_degrees_that_name_a_subject(self):
        for degree in ('B.Sc Statistics', 'M.Sc Data Science', 'B.Tech - Information Technology', 'MCA'):
            with self.subTest(degree=degree):
                profile = analyst(degree)
                text = composed_summary(profile, 'Angular Developer', 4, ['Angular', 'TypeScript', 'RxJS'])
                self.assertEqual(summary_problems(text, profile, 'Angular Developer', 4), [], text)

    def test_a_subject_tied_to_work_outside_the_degree_is_still_caught(self):
        text = ('Data Analyst with 4 years of professional experience, moving into an Angular Developer role. '
                'Applied Statistics to build forecasting models for store sales. Holds a B.Sc in Statistics.')
        problems = summary_problems(text, analyst('B.Sc Statistics'), 'Angular Developer', 4)
        self.assertTrue(any('Statistics' in problem for problem in problems), problems)


if __name__ == '__main__':
    unittest.main()

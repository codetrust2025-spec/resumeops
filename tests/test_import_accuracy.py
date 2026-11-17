"""Import must recover names, headlines, contact links, skills, projects and the current company
from TXT, DOCX and PDF resumes, using only text that is really in the source."""
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from resume_bot.ingest import extract_profile, read_resume_text

RESUME = '''Priya Example Raman
Senior Data Analyst
priya.raman@example.com | +91 98765 43210 | Bengaluru
linkedin.com/in/priya-example | github.com/priya-example | https://priya-example.dev
Summary
Data analyst building SQL reporting and Python automation for retail teams.
Skills
Python, SQL, Power BI, Excel; Airflow
Experience
Data Analyst, Example Retail Insights, Bengaluru, March 2022 - Present
- Built Power BI dashboards for store operations.
- Automated weekly sales reports with Python and Airflow.
Junior Analyst, Sample Consulting, Remote, July 2020 - February 2022
- Prepared Excel models for client pricing reviews.
Projects
- Open-source SQL style checker for analysts
- Kaggle notebook on retail demand patterns
Education
B.Sc. Statistics, Example University, 2017 - 2020
'''


class SloppyModel:
    """Mimics the slips seen in production: the name is put in the headline, skills come back
    as one comma-separated string, projects / portfolio / current company are missed."""
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, schema=None, on_progress=None):
        self.prompts.append(prompt)

        def f(value):
            return {'value': value, 'evidence': value}
        blank = f('')
        return {
            'full_name': blank, 'headline': f('Priya Example Raman'), 'email': blank, 'phone': blank,
            'location': f('Bengaluru'), 'linkedin': blank, 'github': blank, 'portfolio': blank,
            'current_company': blank, 'summary': f('Data analyst building SQL reporting and Python automation for retail teams.'),
            'skills': [f('Python, SQL, Power BI, Excel; Airflow'), f('SQL')],
            'experience': [
                {'company': f('Example Retail Insights'), 'position': f('Data Analyst'), 'start': f('March 2022'),
                 'end': f('Present'), 'location': f('Bengaluru'),
                 'bullets': [f('Built Power BI dashboards for store operations.')]},
                {'company': f('Sample Consulting'), 'position': f('Junior Analyst'), 'start': f('July 2020'),
                 'end': f('February 2022'), 'location': f('Remote'),
                 'bullets': [f('Prepared Excel models for client pricing reviews.')]}],
            'education': [{'institution': f('Example University'), 'degree': f('B.Sc. Statistics'),
                           'start': f('2017'), 'end': f('2020'), 'details': blank}],
            'certifications': [], 'projects': []}


def write_docx(path, text):
    body = ''.join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in text.splitlines())
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                         'wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>')


def write_pdf(path, text):
    def tex(line):
        for a, b in (('\\', r'\textbackslash{}'), ('&', r'\&'), ('%', r'\%'), ('$', r'\$'), ('#', r'\#'),
                     ('_', r'\_'), ('{', r'\{'), ('}', r'\}'), ('~', r'\textasciitilde{}'), ('|', r'\textbar{}')):
            line = line.replace(a, b)
        return line
    body = '\n\n'.join(tex(line) for line in text.splitlines())
    source = path.with_suffix('.tex')
    source.write_text('\\documentclass{article}\\usepackage[T1]{fontenc}\\usepackage{lmodern}\\pagestyle{empty}'
                      '\\setlength{\\parindent}{0pt}\\begin{document}\n' + body + '\n\\end{document}\n')
    subprocess.run(['pdflatex', '-interaction=nonstopmode', '-output-directory', str(path.parent), str(source)],
                   capture_output=True, check=True, timeout=60)


class ImportAccuracyTests(unittest.TestCase):
    def check(self, text, for_review=True):
        model = SloppyModel()
        profile = extract_profile(text, model, for_review=for_review)
        self.assertEqual(profile['full_name'], 'Priya Example Raman')
        self.assertEqual(profile['headline'], 'Senior Data Analyst')
        self.assertEqual(profile['email'], 'priya.raman@example.com')
        self.assertEqual(profile['phone'], '+91 98765 43210')
        self.assertEqual(profile['linkedin'], 'linkedin.com/in/priya-example')
        self.assertEqual(profile['github'], 'github.com/priya-example')
        self.assertEqual(profile['portfolio'], 'https://priya-example.dev')
        self.assertEqual(profile['skills'], ['Python', 'SQL', 'Power BI', 'Excel', 'Airflow'])
        self.assertEqual(profile['projects'], ['Open-source SQL style checker for analysts',
                                               'Kaggle notebook on retail demand patterns'])
        self.assertEqual(profile['current_company'], 'Example Retail Insights')
        self.assertEqual([e['company'] for e in profile['experience']], ['Example Retail Insights', 'Sample Consulting'])
        self.assertEqual(profile['education'][0]['degree'], 'B.Sc. Statistics')
        prompt = model.prompts[0]
        for guide in ('ONE skill per item', 'Never put the name in headline', '"projects":[field]', '"portfolio":field'):
            self.assertIn(guide, prompt)
        if for_review:
            # Production showed a 4th bullet dropped by an old "at most three bullets" instruction.
            self.assertNotIn('at most three', prompt)
            self.assertIn('Include EVERY bullet', prompt)
        return profile

    def test_txt(self):
        self.check(RESUME)
        self.check(RESUME, for_review=False)   # the watcher's strict path validates the same result

    def test_docx(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.docx'
            write_docx(path, RESUME)
            self.check(read_resume_text(path))

    @unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdftotext'), 'TeX/PDF tools missing')
    def test_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.pdf'
            write_pdf(path, RESUME)
            self.check(read_resume_text(path))

    def test_nothing_is_invented_when_the_source_lacks_it(self):
        bare = '\n'.join(line for line in RESUME.splitlines()
                         if 'linkedin' not in line and not line.startswith(('Projects', '- Open', '- Kaggle')))
        bare = bare.replace('Present', 'June 2024')
        model = SloppyModel()
        profile = extract_profile(bare, model, for_review=True)
        self.assertEqual((profile['linkedin'], profile['github'], profile['portfolio']), ('', '', ''))
        self.assertEqual(profile['projects'], [])
        self.assertEqual(profile['current_company'], '')
        # Values the model returns that are not in the text are still rejected.
        model.generate = lambda prompt, schema=None, on_progress=None: {
            **SloppyModel().generate(prompt), 'skills': [{'value': 'Kubernetes', 'evidence': 'Kubernetes'}]}
        self.assertNotIn('Kubernetes', extract_profile(bare, model, for_review=True)['skills'])


if __name__ == '__main__':
    unittest.main()

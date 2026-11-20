"""Word resumes that keep the name, phone, email or links in the page header (or footer) are read in full.

Before this fix only word/document.xml was read, so a header-only name or phone never reached the extraction and the
candidate was asked for facts the document states. Every candidate here is fictional."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from resume_bot.ingest import read_resume_text, repair

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def paragraphs(*runs):
    """Each item is a paragraph: a string, or a list of run XML snippets."""
    out = []
    for item in runs:
        body = item if isinstance(item, list) else [f'<w:r><w:t xml:space="preserve">{item}</w:t></w:r>']
        out.append('<w:p>' + ''.join(body) + '</w:p>')
    return ''.join(out)


BODY = paragraphs('Professional Summary', 'Backend developer building Python services for retail teams.',
                  'Experience', 'Software Engineer, Example Retail Pvt Ltd (fictional), June 2021 - Present',
                  'Built Django REST APIs for order tracking used by store managers every day.',
                  'Education', 'B.Tech Computer Science, Example Institute of Technology (fictional), 2021')


def docx(folder, body=BODY, **parts):
    path = Path(folder) / 'resume.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document {W} {MC}><w:body>{body}</w:body></w:document>')
        for name, xml in parts.items():
            tag = 'hdr' if name.startswith('header') else 'ftr'
            archive.writestr(f'word/{name}.xml', f'<w:{tag} {W} {MC}>{xml}</w:{tag}>')
    return path


class HeadersAndFooters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_the_header_is_read_first_and_the_footer_last(self):
        path = docx(self.temp.name,
                    header1=paragraphs('Meera Example (fictional)', 'meera.example@example.com | +91 90000 33333'),
                    footer1=paragraphs('linkedin.com/in/meera-example-fictional'))
        lines = read_resume_text(path).splitlines()
        self.assertEqual(lines[:2], ['Meera Example (fictional)', 'meera.example@example.com | +91 90000 33333'])
        self.assertEqual(lines[2], 'Professional Summary')
        self.assertEqual(lines[-1], 'linkedin.com/in/meera-example-fictional')

    def test_header_contact_details_reach_the_profile(self):
        path = docx(self.temp.name, header1=paragraphs(
            'Meera Example', [f'<w:r><w:t>meera.example@example.com</w:t></w:r><w:r><w:tab/></w:r>'
                              f'<w:r><w:t>+91 90000 33333</w:t></w:r>']))
        source = read_resume_text(path)
        out = {'full_name': '', 'headline': '', 'email': '', 'phone': '', 'linkedin': '', 'github': '',
               'portfolio': '', 'location': '', 'current_company': '', 'summary': '', 'experience': [],
               'education': [], 'skills': [], 'projects': [], 'certifications': []}
        repair(out, source)
        self.assertEqual((out['full_name'], out['email'], out['phone']),
                         ('Meera Example', 'meera.example@example.com', '+91 90000 33333'))

    def test_a_line_the_body_already_has_is_not_repeated(self):
        path = docx(self.temp.name, header1=paragraphs('Experience', 'Meera Example'),
                    header2=paragraphs('Meera Example'), footer1=paragraphs('Education', 'Page 1'))
        lines = read_resume_text(path).splitlines()
        self.assertEqual(lines.count('Experience'), 1)
        self.assertEqual(lines.count('Education'), 1)
        self.assertEqual(lines.count('Meera Example'), 1)
        self.assertEqual(lines[0], 'Meera Example')

    def test_tabs_and_line_breaks_read_as_word_shows_them(self):
        run = ('<w:pPr><w:tabs><w:tab w:val="right" w:pos="9000"/></w:tabs></w:pPr>'
               '<w:r><w:t>Meera Example</w:t><w:tab/><w:t>Pune</w:t><w:br/><w:t>+91 90000 33333</w:t></w:r>')
        lines = read_resume_text(docx(self.temp.name, header1=f'<w:p>{run}</w:p>')).splitlines()
        self.assertEqual(lines[:2], ['Meera Example Pune', '+91 90000 33333'])

    def test_a_text_box_saved_for_old_word_is_read_once(self):
        box = ('<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
               + paragraphs('Meera Example', '+91 90000 33333') + '</w:txbxContent></w:drawing></mc:Choice>'
               '<mc:Fallback><w:pict><w:txbxContent>' + paragraphs('Meera Example', '+91 90000 33333')
               + '</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>')
        text = read_resume_text(docx(self.temp.name, header1=box))
        self.assertEqual(text.count('+91 90000 33333'), 1)
        self.assertTrue(text.startswith('Meera Example\n+91 90000 33333\n'))

    def test_a_document_without_header_or_footer_reads_as_before(self):
        text = read_resume_text(docx(self.temp.name))
        self.assertEqual(text.splitlines()[0], 'Professional Summary')
        self.assertEqual(len(text.splitlines()), 7)


if __name__ == '__main__':
    unittest.main()

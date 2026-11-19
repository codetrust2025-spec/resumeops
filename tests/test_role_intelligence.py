import json
from datetime import UTC,datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from resume_bot.ai_first import IntakeError, experience_strategy, suggest_sections
from resume_bot.ai_review import approved_resume, claims_for_review, role_intelligence
from resume_bot.claim_audit import claim_violations
from resume_bot.latex import render
from resume_bot.market import relevant_sources
from test_ai_first import FakeAI, candidate


class RoleIntelligenceTests(unittest.TestCase):
    def test_custom_title_is_preserved_without_assuming_other_catalog_terms(self):
        draft = suggest_sections({**candidate(), 'role':'Warehouse Planning Manager'}, FakeAI())
        reviewed = approved_resume(draft, {'attested': True, 'claims': {}})
        self.assertEqual(reviewed['target']['role_title'], 'Warehouse Planning Manager')
        self.assertEqual(reviewed['target']['keywords'], [])
        self.assertEqual(reviewed['ats']['score'], 0)

    def test_attributed_sources_require_title_relevance_recency_and_safe_links(self):
        now = datetime.now(UTC)
        post = {
            'title': 'Senior Warehouse Planning Manager',
            'description': 'Experience with inventory forecasting and workforce planning.<br>Maintain stock controls.',
            'url': 'https://example.com/warehouse',
            'retrieved_at': now.isoformat(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                'greenhouse_boards': ['example', '../outside'],
                'authorized_postings': [
                    {**post,'title':'Software Developer','url':'https://example.com/software'},
                    {**post,'url':'javascript:alert(1)'},
                    {**post,'url':'https://example.com/stale','retrieved_at':(now-timedelta(days=31)).isoformat()},
                ],
            }
            (root/'market-sources.json').write_text(json.dumps(config))
            with patch('resume_bot.market._board',return_value=[{**post,'provider':'Greenhouse public employer board'}]) as api:
                [source] = relevant_sources('Warehouse Planning Manager',root)
            api.assert_called_once()
            self.assertEqual(source['url'],post['url'])
            self.assertEqual(source['retrieved_at'],post['retrieved_at'])
            self.assertEqual(source['relevance'],100)
            self.assertIn('inventory forecasting',source['terms'])
            self.assertTrue(source['responsibilities'])

    def test_catalog_fallback_on_feed_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'market-sources.json').write_text('{"greenhouse_boards":["example"]}')
            with patch('resume_bot.market._board',return_value=[]):
                intel=role_intelligence('Full Stack Developer',root)
        self.assertEqual(intel['role'],'fullstack-developer')
        self.assertIn('React.js',intel['terms'])
        self.assertEqual(intel['sources'][0]['url'],'')

    def test_five_distinct_experience_strategies_and_intake_stability(self):
        messages=set()
        for years in (0,2,4,7,9):
            model=FakeAI()
            draft=suggest_sections({**candidate(),'years_of_experience':years},model)
            strategy=experience_strategy(years)
            self.assertIn(strategy,model.prompt)
            self.assertEqual(len(draft['allowed_input_categories']),14)
            messages.add(strategy)
        self.assertEqual(len(messages),5)

    def test_competencies_use_separate_section_and_audit(self):
        draft=suggest_sections({**candidate(),'role':'frontend-developer'},FakeAI())
        claim=next(row for row in claims_for_review(draft) if row['section']=='Competency')
        reviewed=approved_resume(draft,{'attested':True,'claims':{claim['id']:claim['text']}})
        self.assertEqual(reviewed['profile']['skills'],[])
        self.assertEqual(reviewed['profile']['competencies'],[claim['text']])
        audit={'attested':True,'claims':reviewed['approved_claims']}
        self.assertEqual(claim_violations(reviewed['profile'],audit),[])
        self.assertIn(r'\section*{Core Competencies}',render(reviewed['profile']))

    def test_work_and_project_content_cannot_repeat(self):
        draft=suggest_sections({**candidate(),'role':'frontend-developer','projects':[{'name':'Portfolio'}]},FakeAI())
        entry=draft['suggestions']['experience_responsibilities'][0]['suggestions'][0]
        draft['suggestions']['project_writeups']=[{'project_index':0,'suggestions':[entry]}]
        selected={row['id']:row['text'] for row in claims_for_review(draft)
                  if row['section'] in ('Responsibility','Project detail')}
        with self.assertRaisesRegex(IntakeError,'distinct'):
            approved_resume(draft,{'attested':True,'claims':selected})



    def test_existing_draft_keeps_original_approval_payload_shape(self):
        draft = suggest_sections({**candidate(),'role':'frontend-developer'},FakeAI())
        draft['intelligence'] = {'label':'Frontend Developer','terms':['React'],'sources':[]}
        claim = next(row for row in claims_for_review(draft) if row['section']=='Competency')
        reviewed = approved_resume(draft,{'attested':True,'claims':{claim['id']:claim['text']}})
        self.assertEqual(reviewed['target']['keywords'],[])
        self.assertEqual(reviewed['profile']['skills'],[claim['text']])
        self.assertNotIn('competencies',reviewed['profile'])

    def test_unsupported_technology_in_approved_bullet_is_rejected(self):
        draft = suggest_sections({**candidate(),'role':'frontend-developer'},FakeAI())
        responsibility = next(row for row in claims_for_review(draft) if row['section']=='Responsibility')
        approval = {'attested':True,'claims':{responsibility['id']:'Maintained AWS services.'}}
        with self.assertRaisesRegex(IntakeError,'technology not found'):
            approved_resume(draft,approval)
        aws = next(row for row in claims_for_review(draft) if row['text']=='AWS')
        approval['claims'][aws['id']] = 'AWS'
        self.assertEqual(approved_resume(draft,approval)['profile']['experience'][0]['bullets'],
                         ['Maintained AWS services.'])

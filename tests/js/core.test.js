// Component-logic tests for dashboard/core.js. Run with: node --test tests/js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Core = require('../../dashboard/core.js');

function filledState() {
  const state = Core.emptyState();
  Object.assign(state.profile, {full_name: 'Morgan Example', email: 'morgan@example.com', headline: 'Engineer'});
  state.experience.push({company: 'Example Labs', position: 'Engineer', start: 'Jan 2020', end: 'Dec 2021', location: '', bullets: 'Built X.\n\n Led Y. '});
  state.skills = 'React, Git\nJest';
  state.role = 'frontend-developer';
  return state;
}

test('buildProfile matches the server schema and omits empty Level 1 fields', () => {
  const profile = Core.buildProfile(filledState());
  assert.deepEqual(Object.keys(profile).sort(), ['certifications', 'education', 'email', 'experience', 'full_name',
    'github', 'headline', 'linkedin', 'location', 'phone', 'skills', 'summary'].sort());
  assert.deepEqual(profile.skills, ['React', 'Git', 'Jest']);
  assert.deepEqual(profile.experience[0].bullets, ['Built X.', 'Led Y.']);
});

test('projects and portfolio are included when present', () => {
  const state = filledState();
  state.profile.portfolio = 'https://m.example.com';
  state.projects.push({name: 'Kit', link: '', technologies: 'React, TypeScript', bullets: 'Built forms'});
  const profile = Core.buildProfile(state);
  assert.equal(profile.portfolio, 'https://m.example.com');
  assert.deepEqual(profile.projects, [{name: 'Kit', link: '', technologies: ['React', 'TypeScript'], bullets: ['Built forms']}]);
});

test('stateFromProfile round-trips an imported draft', () => {
  const profile = Core.buildProfile(filledState());
  const state = Core.stateFromProfile(profile, 'source text');
  assert.equal(state.sourceKind, 'import');
  assert.deepEqual(Core.buildProfile(state), profile);
});

test('validation mirrors profile.py rules step by step', () => {
  const empty = Core.emptyState();
  assert.deepEqual(Core.validate(empty, 1).map(e => e.field), ['full_name', 'email']);
  const state = filledState();
  assert.equal(Core.validate(state, 4).length, 0);
  state.experience[0].end = 'Jan 2019';
  assert.match(Core.validate(state, 2)[0].message, /end date is before/);
  state.experience[0].end = 'Dec 2021';
  state.skills = 'React\nreact';
  assert.match(Core.validate(state, 2)[0].message, /duplicate skill/);
  state.skills = 'React';
  state.role = '';
  assert.equal(Core.validate(state, 3)[0].field, 'role');
  state.role = 'frontend-developer';
  assert.equal(Core.validate(state, 5)[0].field, 'verified');
  state.verified = true;
  assert.equal(Core.validate(state, 5).length, 0);
  state.profile.full_name = 'TBD';
  assert.equal(Core.validate(state, 1)[0].field, 'full_name');
});

test('experience years count overlapping roles once and set the Level 1 level label', () => {
  const now = new Date(2026, 10, 17);
  assert.equal(Core.experienceYears([{start: 'Jan 2020', end: 'Dec 2021'}, {start: 'Jun 2021', end: 'Dec 2022'}], now), 3);
  assert.equal(Core.experienceYears([{start: '2024-11', end: 'Present'}], now), 2.1);
  assert.equal(Core.experienceYears([{start: 'sometime', end: ''}], now), 0);
  assert.equal(Core.experienceLevel(1), 'Fresher / Junior');
  assert.equal(Core.experienceLevel(3), 'Mid-level');
  assert.equal(Core.experienceLevel(7), 'Senior');
  assert.equal(Core.experienceLevel(12), 'Lead / Principal');
});

test('keywords deduplicate case-insensitively and cap at 50', () => {
  assert.deepEqual(Core.addKeywords(['React'], ['react', ' Jest ', '', 'Git']), ['React', 'Jest', 'Git']);
  assert.equal(Core.addKeywords([], Array.from({length: 80}, (_, i) => 'k' + i)).length, 50);
});

test('job progress reflects real watcher state', () => {
  assert.deepEqual(Core.jobProgress(null), {percent: 0, label: 'Not queued', state: 'idle'});
  assert.equal(Core.jobProgress({status: 'queued'}).state, 'queued');
  assert.equal(Core.jobProgress({status: 'processing', percent: 70, stage: 'LaTeX created'}).label, 'LaTeX created');
  const failed = Core.jobProgress({status: 'failed', percent: 40, blocked: true, error: 'Ollama down'});
  assert.equal(failed.percent, 40);
  assert.match(failed.label, /Blocked · retrying automatically · Ollama down/);
  assert.equal(Core.jobProgress({status: 'ok', percent: 100}).percent, 100);
  assert.equal(Core.stageIndex(0), -1);
  assert.equal(Core.stageIndex(75), 5);
  assert.equal(Core.stageIndex(100), Core.PIPELINE.length - 1);
});

test('active job prefers processing, then failed, then queued, then finished', () => {
  const jobs = [{status: 'ok', percent: 100, name: 'a'}, {status: 'queued', name: 'b'}, {status: 'failed', name: 'c'}];
  assert.equal(Core.activeJob(jobs).name, 'c');
  assert.equal(Core.activeJob([...jobs, {status: 'processing', name: 'd'}]).name, 'd');
  assert.equal(Core.activeJob([]), null);
});

test('score labels follow Level 1 thresholds and the sample is synthetic', () => {
  assert.deepEqual([95, 85, 75, 10].map(Core.scoreLabel), ['Excellent', 'Good', 'Fair', 'Needs improvement']);
  assert.match(Core.SAMPLE.email, /@example\.com$/);
  assert.equal(Core.STEPS.length, 5);
});

test('content signals count quantified and action-verb bullets', () => {
  const profile = Core.buildProfile(filledState());
  profile.experience[0].bullets = ['Built 3 tools.', 'helped team'];
  assert.deepEqual(Core.contentSignals(profile), {bullets: 2, quantified: 1, actionVerbs: 1});
});

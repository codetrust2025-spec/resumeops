// Display logic for live progress (dashboard/live.js). Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Live = require('../../dashboard/live.js');

const base = {name: 'job.json', status: 'processing', percent: 37, stage: 'Generating resume using Ollama',
  stage_key: 'ai', stage_label: 'Ollama AI resume generation', measured: true, started: 1000, last: 1000,
  stage_started: 1010, activity: 1040, heartbeat: 1041, expected_seconds: 240, attempt: 1,
  completed_stages: ['validate', 'job', 'source'], skipped_stages: [], units: null, detail: '512 characters'};

test('running job shows title, stage, elapsed and status from the server record', () => {
  const view = Live.describe(base, 1042, {});
  assert.equal(view.title, 'Processing Resume — 37%');
  assert.equal(view.stage, 'Ollama AI resume generation — Generating resume using Ollama');
  assert.equal(view.elapsed, '00:42');
  assert.equal(view.status, 'AI generation running');
  assert.equal(view.state, 'running');
  assert.deepEqual(view.stages.slice(0, 5).map(s => s.state), ['done', 'done', 'done', 'current', 'todo']);
});

test('percentage never decreases for the same attempt, and never shows 100 before verified completion', () => {
  const memory = {};
  assert.equal(Live.describe(base, 1042, memory).percent, 37);
  assert.equal(Live.describe({...base, percent: 30}, 1043, memory).percent, 37);   // stale poll
  assert.equal(Live.describe({...base, percent: 100}, 1044, memory).percent, 99);  // not yet 'ok'
  assert.equal(Live.describe({...base, status: 'ok', percent: 100}, 1045, memory).percent, 100);
  // A new attempt is a real restart and is reported as such.
  const retry = Live.describe({...base, attempt: 2, percent: 6}, 1100, memory);
  assert.equal(retry.percent, 6);
  assert.match(retry.status, /attempt 2/);
});

test('unmeasured work holds the percentage and says so', () => {
  const view = Live.describe({...base, measured: false, stage_key: 'pdf', stage_label: 'Compile and validate local PDF',
    stage: 'Compiling LaTeX with pdfLaTeX', percent: 71, activity: 1030}, 1050, {});
  assert.equal(view.state, 'processing');
  assert.match(view.status, /no measurable progress; percentage held · last activity 00:20 ago/);
});

test('stale heartbeat and slow stages raise warnings', () => {
  const view = Live.describe({...base, heartbeat: 1000, stage_started: 700}, 1042, {});
  assert.equal(view.state, 'stalled');
  assert.equal(view.warnings.length, 2);
  assert.match(view.warnings[0], /No watcher heartbeat for 00:42/);
  assert.match(view.warnings[1], /taking longer than expected/);
});

test('waiting, retrying, failed and completed are distinct states', () => {
  const waiting = Live.describe({name: 'q.json', status: 'queued', percent: 0, last: 1000, failures: 0, queue_position: 2}, 1010, {});
  assert.equal(waiting.state, 'waiting');
  assert.match(waiting.status, /position 2/);
  const failed = {...base, status: 'failed', percent: 84, stage_key: 'overleaf', stage_label: 'Upload and compile on Overleaf',
    error: 'Overleaf is unavailable', attempt: 1, next_retry: 1100, blocked: true};
  const retrying = Live.describe(failed, 1040, {});
  assert.equal(retrying.state, 'retrying');
  assert.ok(retrying.canRetry);
  assert.match(retrying.status, /Attempt 1 failed during Upload and compile on Overleaf · automatic retry in 01:00/);
  assert.equal(retrying.stages[7].state, 'failed');
  assert.equal(Live.describe({...failed, last: 1030}, 1090, {}).elapsed, '00:30 (stopped)');
  assert.equal(Live.describe(failed, 1200, {}).state, 'failed');
  const done = Live.describe({...base, status: 'ok', percent: 100, stage: 'Verified PDF ready to download'}, 1100, {});
  assert.equal(done.state, 'completed');
  assert.ok(done.complete);
  assert.ok(done.stages.every(s => s.state === 'done'));
  const odd = Live.describe({...base, status: 'ok', percent: 96}, 1100, {});
  assert.equal(odd.complete, false);
});

test('clock formats minutes and hours', () => {
  assert.equal(Live.clock(5), '00:05');
  assert.equal(Live.clock(3725), '1:02:05');
});

test('pipeline groups Job into Validate and counts finished steps out of eight', () => {
  const view = Live.describe(base, 1042, {});
  assert.deepEqual(view.pipeline.map(s => s.label),
    ['Validate', 'Source', 'AI', 'Checks', 'LaTeX', 'Local PDF', 'Overleaf', 'Verify']);
  assert.deepEqual(view.pipeline.slice(0, 4).map(s => s.state), ['done', 'done', 'current', 'todo']);
  assert.equal(view.completedCount, 2);
  assert.equal(view.heartbeat, '00:01 ago');
  const early = Live.describe({...base, stage_key: 'job', completed_stages: ['validate']}, 1042, {});
  assert.equal(early.pipeline[0].state, 'current');
  const done = Live.describe({...base, status: 'ok', percent: 100, skipped_stages: ['overleaf']}, 1042, {});
  assert.equal(done.completedCount, 8);
  assert.equal(done.pipeline[6].state, 'skipped');
});

test('GPU placement and the technical failure detail are reported, never invented', () => {
  assert.equal(Live.describe(base, 1042, {}).gpu, '');
  const runtime = {model: 'qwen2.5:7b', size: 4755397016, size_vram: 4755397016, gpu_percent: 100};
  assert.equal(Live.gpuText(runtime), 'qwen2.5:7b · 100% in GPU memory (4.4 GB of 4.4 GB)');
  const failed = Live.describe({...base, status: 'failed', error: 'Local Ollama is unavailable.',
    error_detail: 'OllamaError: stream closed', ollama_runtime: runtime, last: 1050}, 1060, {});
  assert.equal(failed.errorDetail, 'Technical detail: OllamaError: stream closed');
  assert.match(failed.gpu, /100% in GPU memory/);
  assert.equal(failed.heartbeat, '—');
});

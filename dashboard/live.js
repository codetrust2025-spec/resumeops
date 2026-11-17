/* Live job presentation. Pure functions over the watcher's authoritative /api/status record.
   Nothing here invents progress: the percentage is the watcher's measured value, held at the
   highest value already shown for the same attempt so a delayed poll can never move it back. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ResumeLive = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STAGES = [
    ['validate', 'Validate'], ['job', 'Job'], ['source', 'Source'], ['ai', 'Ollama AI'],
    ['checks', 'Checks'], ['latex', 'LaTeX'], ['pdf', 'Local PDF'], ['overleaf', 'Overleaf'],
    ['verify', 'Verify'],
  ];
  const HEARTBEAT_STALE = 20;  // seconds without a watcher heartbeat while processing

  function clock(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(s / 3600);
    const mm = String(Math.floor(s % 3600 / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return h ? h + ':' + mm + ':' + ss : mm + ':' + ss;
  }

  function attemptKey(job) {
    return job.name + '#' + (job.attempt || job.failures + 1 || 1);
  }

  /** Monotonic display per job attempt. `memory` is a plain object kept by the caller. */
  function displayPercent(job, memory) {
    const key = attemptKey(job);
    const value = Math.max(memory[key] || 0, Math.min(100, Number(job.percent) || 0));
    // 100 only when the watcher reports a finished, verified job.
    const shown = job.status === 'ok' && job.percent === 100 ? 100 : Math.min(value, 99);
    memory[key] = shown;
    return shown;
  }

  function stageStates(job) {
    const done = new Set(job.completed_stages || []);
    const skipped = new Set(job.skipped_stages || []);
    return STAGES.map(([key, label]) => {
      let state = 'todo';
      if (job.status === 'ok' && job.percent === 100) state = skipped.has(key) ? 'skipped' : 'done';
      else if (skipped.has(key)) state = 'skipped';
      else if (done.has(key)) state = 'done';
      else if (job.stage_key === key) state = job.status === 'failed' ? 'failed' : 'current';
      return {key, label, state};
    });
  }

  /**
   * Describe a job for display.
   * state: waiting | running | processing (unmeasured) | stalled | retrying | failed | completed
   */
  function describe(job, now, memory = {}) {
    const percent = displayPercent(job, memory);
    const view = {percent, state: 'running', title: '', stage: '', elapsed: '', status: '',
      detail: '', warnings: [], stages: stageStates(job), canRetry: false, complete: false};
    if (job.status === 'queued') {
      view.state = 'waiting';
      view.title = 'Waiting to start — ' + percent + '%';
      view.stage = 'Queued for the watcher';
      view.elapsed = clock(now - job.last);
      view.status = 'Waiting' + (job.queue_position ? ' · position ' + job.queue_position + ' in the queue' : '');
      return view;
    }
    view.elapsed = clock(now - (job.started || job.last));
    view.stage = job.stage_label ? job.stage_label + (job.stage && job.stage !== job.stage_label ? ' — ' + job.stage : '')
      : (job.stage || 'Processing');
    if (job.status === 'ok') {
      if (job.percent === 100) {
        view.state = 'completed';
        view.complete = true;
        view.title = 'Resume completed — 100%';
        view.stage = job.stage || 'Verified PDF ready to download';
        view.status = 'Completed successfully' + (job.attempt > 1 ? ' on attempt ' + job.attempt : '');
      } else {
        view.state = 'processing';
        view.title = 'Finalising — ' + percent + '%';
        view.status = 'Watcher reported success without final verification; waiting for it.';
      }
      return view;
    }
    if (job.status === 'failed') {
      view.elapsed = clock((job.last || now) - (job.started || job.last)) + ' (stopped)';
      const waitFor = job.next_retry ? job.next_retry - now : 0;
      view.canRetry = true;
      view.detail = job.error || '';
      view.state = waitFor > 0 ? 'retrying' : 'failed';
      view.title = (waitFor > 0 ? 'Retry scheduled — ' : 'Failed — ') + percent + '%';
      view.status = 'Attempt ' + (job.attempt || job.failures || 1) + ' failed' +
        (job.stage_label ? ' during ' + job.stage_label : '') +
        (waitFor > 0 ? ' · automatic retry in ' + clock(waitFor) : ' · waiting for the watcher to retry');
      return view;
    }
    // processing
    const units = job.units && job.units.total ? job.units.done + ' of ' + job.units.total + ' ' + job.units.unit : '';
    const lastActivity = job.activity ? now - job.activity : 0;
    if (job.measured === false) {
      view.state = 'processing';
      view.title = 'Processing Resume — ' + percent + '%';
      view.status = 'Processing — this step reports no measurable progress; percentage held · last activity ' +
        clock(lastActivity) + ' ago';
    } else {
      view.title = 'Processing Resume — ' + percent + '%';
      view.status = (job.stage_key === 'ai' ? 'AI generation running' : 'Running') + (units ? ' · ' + units : '');
    }
    if (job.attempt > 1) view.status += ' · attempt ' + job.attempt;
    view.detail = job.detail || '';
    const heartbeatAge = job.heartbeat ? now - job.heartbeat : null;
    if (heartbeatAge !== null && heartbeatAge > HEARTBEAT_STALE) {
      view.state = 'stalled';
      view.warnings.push('No watcher heartbeat for ' + clock(heartbeatAge) + '. The worker may have stopped.');
    }
    const inStage = job.stage_started ? now - job.stage_started : 0;
    if (job.expected_seconds && inStage > job.expected_seconds) {
      view.warnings.push('This stage is taking longer than expected (' + clock(inStage) + ', usually under ' +
        clock(job.expected_seconds) + ').');
    }
    return view;
  }

  return {STAGES, clock, describe, displayPercent, stageStates, attemptKey};
}));

/* Operator view of public-form applications (/candidates): every candidate, where their resume is, queue control,
   downloads and new private links. Reads /api/intake/candidates (operator accounts only). */
'use strict';
const $ = id => document.getElementById(id);
let data = {candidates: []};

const LABELS = {queued: 'Waiting in queue', retrying: 'Retrying', processing: 'Preparing', handed_off: 'Building',
  needs_input: 'Waiting for the candidate', complete: 'Complete', blocked: 'Stopped'};

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric',
    minute: '2-digit'}) : '';
}

function bucket(entry) {
  const status = entry.job?.status || 'queued';
  return status === 'retrying' ? 'queued' : status === 'handed_off' ? 'processing' : status;
}

function stats() {
  const counts = {};
  for (const entry of data.candidates) if (!entry.deleted) counts[bucket(entry)] = (counts[bucket(entry)] || 0) + 1;
  const items = [['Applications', data.candidates.filter(entry => !entry.deleted).length],
    ['Waiting in queue', counts.queued || 0], ['Preparing', counts.processing || 0],
    ['Waiting for the candidate', counts.needs_input || 0], ['Complete', counts.complete || 0],
    ['Stopped', counts.blocked || 0], ['GPU slots in use', `${data.gpu?.busy ?? 0} of ${data.gpu?.slots ?? 1}`]];
  $('cd-stats').replaceChildren(...items.map(([label, value]) => {
    const box = el('div', null, 'cd-stat');
    box.append(el('strong', String(value)), el('span', label));
    return box;
  }));
  const paused = data.queue?.paused;
  $('cd-queue-state').textContent = paused ? 'Processing is paused: applications are saved and wait.' :
    'Processing runs one AI job at a time on the GPU, account resumes first, then applications oldest first.';
  $('cd-pause').textContent = paused ? 'Resume processing' : 'Pause processing';
  $('cd-pause').dataset.paused = paused ? '1' : '';
}

function download(entry, name, text) {
  const link = el('a', text, 'cd-link');
  link.href = '/api/artifact?' + new URLSearchParams({key: entry.job.revision, name, download: '1'});
  link.addEventListener('click', async event => {
    event.preventDefault();
    try { await ResumeRecovery.download({url: link.getAttribute('href'), name: `${entry.candidate}-${name}`}); }
    catch (error) { $('cd-message').textContent = error.message; }
  });
  return link;
}

function row(entry) {
  const tr = el('tr');
  const who = el('td');
  who.append(el('strong', entry.deleted ? '(deleted by the candidate)' : entry.name), el('span', entry.candidate, 'cd-id'),
    el('span', when(entry.created), 'cd-muted'));
  const contact = el('td');
  if (!entry.deleted) contact.append(el('span', entry.email), el('span', entry.phone, 'cd-muted'));
  const role = el('td', entry.role || '');
  if (entry.versions > 1) role.append(el('span', `${entry.versions} versions`, 'cd-muted'));
  const state = el('td');
  const job = entry.job || {};
  const status = bucket(entry);
  state.append(el('span', LABELS[job.status] || LABELS[status] || status, `cd-badge cd-${status}`));
  let detail = job.stage || '';
  if (job.queue_ahead !== undefined) detail += ` · ${job.queue_ahead} ahead`;
  state.append(el('span', detail, 'cd-muted'));
  if (job.percent !== undefined && job.percent !== null && status !== 'complete') {
    const bar = el('progress');
    bar.max = 100;
    bar.value = job.percent || 0;
    bar.setAttribute('aria-label', `${job.percent}%`);
    state.append(bar);
  }
  if (job.error && status === 'blocked') state.append(el('span', job.error, 'cd-error'));
  const outputs = el('td', null, 'cd-outputs');
  if (status === 'complete' && job.revision) {
    if (entry.ats) outputs.append(el('span', `ATS ${entry.ats}`, 'cd-ats'));
    outputs.append(download(entry, 'resume.pdf', 'PDF'), download(entry, 'main.tex', 'LaTeX'),
      download(entry, 'overleaf.pdf', 'Overleaf PDF'));
    if (job.overleaf) {
      const open = el('a', 'Overleaf', 'cd-link');
      open.href = job.overleaf; open.target = '_blank'; open.rel = 'noopener';
      outputs.append(open);
    }
  }
  if (!entry.deleted) {
    const key = el('button', 'New private link', 'cd-small');
    key.type = 'button';
    key.addEventListener('click', () => newLink(entry));
    outputs.append(key);
  }
  tr.append(who, contact, role, state, outputs);
  return tr;
}

function render() {
  stats();
  const term = $('cd-search').value.trim().toLowerCase();
  const wanted = $('cd-status').value;
  const shown = data.candidates.filter(entry => (!wanted || bucket(entry) === wanted) && (!term ||
    [entry.name, entry.email, entry.phone, entry.candidate, entry.role].some(item => (item || '').toLowerCase().includes(term))));
  $('cd-rows').replaceChildren(...shown.map(row));
  $('cd-empty').hidden = data.candidates.length > 0;
}

async function newLink(entry) {
  if (!window.confirm(`Create a new private link for ${entry.name} (${entry.candidate})? Their current link stops working.`)) return;
  const response = await ResumeAuth.call('/api/intake/key', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({candidate: entry.candidate})});
  const result = await response.json();
  if (!response.ok) { $('cd-message').textContent = result.error || 'Could not create a link.'; return; }
  $('cd-link').value = result.link;
  $('cd-link-dialog').showModal();
}

async function refresh() {
  const response = await ResumeAuth.call('/api/intake/candidates');
  if (!response.ok) {
    $('cd-message').textContent = response.status === 404 || response.status === 403
      ? 'Only operator accounts can see candidates.' : 'Could not load candidates.';
    return;
  }
  data = await response.json();
  $('cd-message').textContent = '';
  if (!$('cd-form-url').value) $('cd-form-url').value = data.form_url;
  render();
}

async function init() {
  $('cd-search').addEventListener('input', render);
  $('cd-status').addEventListener('change', render);
  $('cd-copy-form').addEventListener('click', () => navigator.clipboard.writeText($('cd-form-url').value).catch(() => $('cd-form-url').select()));
  $('cd-link-copy').addEventListener('click', () => navigator.clipboard.writeText($('cd-link').value).catch(() => $('cd-link').select()));
  $('cd-link-close').addEventListener('click', () => $('cd-link-dialog').close());
  $('cd-pause').addEventListener('click', async () => {
    const paused = !$('cd-pause').dataset.paused;
    if (paused && !window.confirm('Pause processing? Applications are still saved; preparation waits until you resume.')) return;
    await ResumeAuth.call('/api/intake/queue', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paused})});
    await refresh();
  });
  await ResumeAuth.ready();
  for (;;) {
    await refresh().catch(error => { $('cd-message').textContent = error.message; });
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
}
init();

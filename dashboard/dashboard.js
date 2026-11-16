'use strict';
/* App shell, routing, status polling and the read-only views. The wizard lives in workflow.js. */
const $ = id => document.getElementById(id);
const Core = window.ResumeCore;
const text = (tag, value, className) => {
  const node = document.createElement(tag);
  if (value !== undefined && value !== null) node.textContent = value;
  if (className) node.className = className;
  return node;
};
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
const pill = (label, state) => {
  const node = text('span', label, 'pill');
  node.dataset.state = state;
  return node;
};
const link = (label, href, newTab = true) => {
  const a = text('a', label);
  a.href = href;
  if (newTab) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  return a;
};
const setBar = (bar, percent, state) => {
  bar.style.width = Math.max(0, Math.min(100, percent)) + '%';
  bar.parentElement.dataset.state = state || '';
};
const statusLabels = {queued: 'Waiting', ok: 'Completed', failed: 'Awaiting retry', processing: 'Processing'};
const artifactLabels = {'resume.pdf': 'Local PDF', 'overleaf.pdf': 'Overleaf PDF', 'main.tex': 'LaTeX',
  'overleaf-project.zip': 'Project ZIP', 'result.json': 'Result', 'audit.json': 'Audit', 'profile.json': 'Profile'};
const artifactUrl = (key, name) => '/api/artifact?' + new URLSearchParams({key, name});
const token = document.querySelector('meta[name=dashboard-token]').content;
let snapshot;
let catalog = {roles: [], templates: {}, keywordGroups: {}};
let refreshInFlight = false;

/* ---------- Routing ---------- */
const ROUTES = ['dashboard', 'create', 'candidates', 'outputs', 'ats', 'templates', 'logs', 'settings'];
function route() {
  const name = (location.hash.match(/^#\/([a-z]+)/) || [])[1];
  const current = ROUTES.includes(name) ? name : 'dashboard';
  for (const view of document.querySelectorAll('[data-view]')) view.hidden = view.dataset.view !== current;
  for (const a of document.querySelectorAll('[data-route]')) {
    if (a.dataset.route === current) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  $('sidebar').dataset.open = 'false';
  $('menu-toggle').setAttribute('aria-expanded', 'false');
  document.title = (document.querySelector(`[data-route="${current}"]`)?.textContent || 'Dashboard') + ' · ResumeOps';
  if (current === 'logs') loadLog();
  if (current === 'settings') loadSettings();
  if (current === 'templates') renderTemplates();
  window.dispatchEvent(new CustomEvent('route', {detail: current}));
}
window.addEventListener('hashchange', () => { route(); $('main').focus({preventScroll: true}); });
$('menu-toggle').addEventListener('click', () => {
  const open = $('sidebar').dataset.open !== 'true';
  $('sidebar').dataset.open = String(open);
  $('menu-toggle').setAttribute('aria-expanded', String(open));
});

/* ---------- API ---------- */
async function postJSON(path, body) {
  const response = await fetch(path, {method: 'POST', body: typeof body === 'string' ? body : JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-Dashboard-Token': token}});
  let result = {};
  try { result = await response.json(); } catch { /* handled below */ }
  if (!response.ok) throw new Error(result.error || 'Request failed (HTTP ' + response.status + ').');
  return result;
}
async function submitFile(path, data, headers) {
  const response = await fetch(path, {method: 'POST', body: data, headers: {...headers, 'X-Dashboard-Token': token}});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}
function queueCandidate(data) { return submitFile('/api/queue', data, {'Content-Type': 'application/json'}); }
function queueMessage(result) {
  return result.created ? 'Queued. The watcher will pick it up.' : 'This candidate revision is already in the inbox.';
}

/* ---------- Dashboard ---------- */
function renderActive(jobs) {
  const job = Core.activeJob(jobs.filter(item => item.status !== 'queued'));
  $('active-workflow').hidden = !job;
  if (!job) return;
  const progress = Core.jobProgress(job);
  $('active-candidate').textContent = job.candidate || job.name;
  $('active-percent').textContent = progress.percent + '%';
  setBar($('workflow-bar'), progress.percent, progress.state);
  $('workflow-stage').textContent = progress.percent + '% · ' + progress.label;
  $('workflow-time').textContent = 'Updated ' + date(job.updated) + (job.failures ? ' · Retries: ' + job.failures : '');
}

function renderRecent() {
  const rows = snapshot.jobs.slice(0, 6).map(job => {
    const row = document.createElement('tr');
    const progress = Core.jobProgress(job);
    row.append(text('td', job.candidate || job.name), text('td', progress.label),
      Object.assign(document.createElement('td'), {}), text('td', date(job.updated || job.last), 'nowrap'));
    row.children[2].append(pill(job.blocked ? 'Blocked · retrying' : statusLabels[job.status] || job.status, job.status));
    return row;
  });
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = text('td', 'No candidates yet. Import a file or create a resume to start.');
    cell.colSpan = 4; row.append(cell); rows.push(row);
  }
  $('recent').replaceChildren(...rows);
}

/* ---------- Candidates ---------- */
function renderJobs() {
  if (!snapshot) return;
  const query = $('filter').value.trim().toLowerCase();
  const rows = snapshot.jobs.filter(job => (job.name + ' ' + job.candidate).toLowerCase().includes(query)).map(job => {
    const row = document.createElement('tr');
    const name = text('td');
    name.append(text('strong', job.candidate || 'Candidate not yet read'), text('small', job.name));
    if (job.error) name.append(text('small', job.error, 'error'));
    const progress = Core.jobProgress(job);
    const cell = text('td');
    const mini = text('div', null, 'mini-bar');
    const bar = text('div', null, 'bar'); const fill = text('span'); bar.append(fill);
    setBar(fill, progress.percent, progress.state);
    mini.append(bar, text('small', progress.percent + '% · ' + (job.stage || progress.label)));
    cell.append(mini);
    if (job.revision) cell.append(link('Open verified PDF', artifactUrl(job.revision, 'resume.pdf')));
    const status = text('td');
    status.append(pill(job.blocked ? 'Blocked · retrying' : statusLabels[job.status] || job.status, job.status));
    row.append(name, cell, status, text('td', String(job.failures || 0)), text('td', date(job.last), 'nowrap'));
    return row;
  });
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = text('td', query ? 'No matching candidates.' : 'The inbox is empty.');
    cell.colSpan = 5; row.append(cell); rows.push(row);
  }
  $('jobs').replaceChildren(...rows);
}

/* ---------- Outputs ---------- */
const openPreviews = new Set();
function renderRevisions() {
  if (!snapshot) return;
  const query = $('output-filter').value.trim().toLowerCase();
  const cards = snapshot.revisions.filter(item => item.candidate.toLowerCase().includes(query)).map(item => {
    const card = text('article', null, 'revision');
    const head = text('div', null, 'revision-head');
    head.append(text('h3', item.candidate));
    if (item.url && item.sha256) head.append(pill('Published to Overleaf', 'ok'));
    else head.append(pill('Local PDF verified', 'queued'));
    const meta = [item.model, item.pages ? item.pages + (item.pages === 1 ? ' page' : ' pages') : '', date(item.modified)].filter(Boolean);
    card.append(head, text('small', meta.join(' · ')));
    const links = text('div', null, 'links');
    if (item.url) links.append(link('Open in Overleaf', item.url));
    for (const name of item.files) links.append(link(artifactLabels[name] || name, artifactUrl(item.key, name)));
    if (item.files.includes('resume.pdf')) {
      const toggle = text('button', openPreviews.has(item.key) ? 'Hide PDF preview' : 'Preview PDF', 'link');
      toggle.type = 'button';
      toggle.addEventListener('click', () => {
        if (openPreviews.has(item.key)) openPreviews.delete(item.key); else openPreviews.add(item.key);
        renderRevisions();
      });
      links.append(toggle);
    }
    card.append(links);
    if (item.ats) card.append(text('small', 'ATS keyword coverage ' + item.ats.score + '% · ' + roleName(item.ats.role)));
    if (item.sha256) card.append(text('small', 'Overleaf PDF SHA-256 ' + item.sha256, 'sha'));
    if (openPreviews.has(item.key)) {
      const frame = document.createElement('iframe');
      frame.className = 'pdf-frame';
      frame.title = 'PDF preview for ' + item.candidate;
      frame.src = artifactUrl(item.key, 'resume.pdf');
      card.append(frame);
    }
    return card;
  });
  const existing = new Map(Array.from($('revisions').querySelectorAll('iframe'), frame => [frame.src, frame]));
  for (const card of cards) {
    const frame = card.querySelector('iframe');
    const keep = frame && existing.get(frame.src);
    if (keep) frame.replaceWith(keep);
  }
  $('revisions').replaceChildren(...(cards.length ? cards : [text('p', 'No matching resume revisions.', 'empty')]));
}

/* ---------- ATS ---------- */
function roleName(id) { return catalog.roles.find(role => role.id === id)?.name || id || '—'; }
function renderAtsRows() {
  if (!snapshot) return;
  const rows = snapshot.revisions.filter(item => item.ats).map(item => {
    const row = document.createElement('tr');
    const coverage = text('td');
    coverage.append(text('strong', item.ats.score + '%'), text('small', Core.scoreLabel(item.ats.score)));
    const terms = text('td');
    const chips = text('div', null, 'chips');
    for (const term of item.ats.matched || []) chips.append(text('span', term, 'chip static match'));
    for (const term of item.ats.missing || []) chips.append(text('span', term, 'chip static miss'));
    terms.append(text('small', (item.ats.matched || []).length + ' matched · ' + (item.ats.missing || []).length + ' missing'), chips);
    row.append(text('td', item.candidate), text('td', roleName(item.ats.role)),
      text('td', catalog.templates[item.ats.template] || item.ats.template || '—'), coverage, terms);
    return row;
  });
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = text('td', 'No role-targeted resumes have been generated yet.');
    cell.colSpan = 5; row.append(cell); rows.push(row);
  }
  $('ats-rows').replaceChildren(...rows);
}

/** Shared renderer for an /api/preview report; used by the wizard and the ATS page. */
function renderReport(container, report) {
  if (!report) {
    container.replaceChildren(text('p', 'Select a target role and complete the required details to see ATS analysis.'));
    return;
  }
  const scores = text('div', null, 'score-row');
  for (const [label, value] of [['Keyword coverage', report.breakdown.keywords], ['Sections', report.breakdown.sections],
    ['Content evidence', report.breakdown.content], ['Overall', report.overall]]) {
    const box = text('div', null, 'score');
    box.append(text('small', label), text('strong', value + '%'));
    scores.append(box);
  }
  const verdict = text('p', `${Core.scoreLabel(report.overall)} · ${report.passes ? 'Likely to pass ATS screening' : 'Below the 80% ATS target'} · weights keywords ${report.weights.keywords}, sections ${report.weights.sections}, content ${report.weights.content}`);
  const matched = text('div', null, 'chips');
  for (const term of report.matched) matched.append(text('span', term, 'chip static match'));
  for (const term of report.missing) matched.append(text('span', term, 'chip static miss'));
  const priority = text('div', null, 'chips');
  for (const [level, terms] of Object.entries(report.priority_gaps || {})) {
    for (const term of terms) priority.append(text('span', `${level === 'mustHave' ? 'Must have' : level === 'highPriority' ? 'High priority' : 'Good to have'}: ${term}`, 'chip static miss'));
  }
  const suggestions = text('ul', null, 'suggestions');
  for (const item of report.suggestions) suggestions.append(text('li', item));
  const children = [scores, verdict, text('h3', 'Target keywords'), matched];
  if (priority.children.length) children.push(text('h3', 'Priority keywords not evidenced'), priority);
  children.push(text('h3', 'Suggestions'), suggestions, text('small', report.method));
  container.replaceChildren(...children);
}
window.renderReport = renderReport;

/* ---------- Templates ---------- */
let templatesRendered = false;
const templateNotes = {standard: 'Balanced spacing for one- or two-page resumes. Default for most roles.',
  compact: 'Tighter paragraph and section spacing to fit more verified detail on a page.'};
async function renderTemplates() {
  if (templatesRendered || !catalog.roles.length) return;
  templatesRendered = true;
  const cards = [];
  for (const [id, name] of Object.entries(catalog.templates)) {
    const card = text('section', null, 'card template-card');
    const head = text('div', null, 'card-head');
    const title = text('div'); title.append(text('h2', name), text('small', templateNotes[id] || ''));
    head.append(title, pill('ATS-readable', 'ok'));
    const code = text('pre', 'Rendering sample…', 'code');
    const foot = text('div', null, 'card-foot');
    const use = text('button', 'Use this template', 'primary');
    use.type = 'button';
    use.addEventListener('click', () => { window.dispatchEvent(new CustomEvent('use-template', {detail: id})); location.hash = '#/create'; });
    foot.append(use);
    card.append(head, code, foot);
    cards.push(card);
    postJSON('/api/preview', {profile: Core.SAMPLE, target: {role: catalog.roles[0].id, template: id, keywords: []}})
      .then(result => { code.textContent = result.latex; })
      .catch(error => { code.textContent = error.message; templatesRendered = false; });
  }
  $('template-cards').replaceChildren(...cards);
}

/* ---------- Logs ---------- */
async function loadLog() {
  const name = $('log-select').value;
  if (!name) return;
  try {
    const response = await fetch('/api/log?' + new URLSearchParams({name}));
    if (!response.ok) throw new Error('Log unavailable.');
    $('log-content').textContent = await response.text();
  } catch (error) {
    $('log-content').textContent = error.message;
  }
}
$('log-select').addEventListener('change', loadLog);

/* ---------- Settings ---------- */
async function loadSettings() {
  try {
    const response = await fetch('/api/settings');
    if (!response.ok) throw new Error('Settings unavailable.');
    const s = await response.json();
    const gpu = s.ollama.loaded.length ? s.ollama.loaded.map(m => m.name + (m.gpu ? ' · GPU' : ' · CPU')).join(', ') : 'No model loaded right now';
    const groups = [
      ['Local AI (Ollama)', [['Endpoint', s.ollama.url], ['Model', s.ollama.model],
        ['Reachable', s.ollama.reachable ? 'Yes' : 'No'], ['Model installed', s.ollama.modelInstalled ? 'Yes' : 'No'], ['Loaded', gpu]]],
      ['Watcher', [['Status', snapshot?.healthy ? 'Active' : 'Heartbeat missing or stale'], ['Last scan', date(snapshot?.lastScan)],
        ['Alerts', snapshot?.alert ? 'Active alert' : 'None']]],
      ['Access', s.access.map((item, index) => ['Route ' + (index + 1), item])],
      ['Resume engine', [['Roles', String(s.roles)], ['Templates', Object.values(s.templates).join(', ')],
        ['MongoDB persistence', s.mongodb ? 'Configured' : 'Not configured'], ['Alert webhook', s.alertWebhook ? 'Configured' : 'Not configured']]],
    ];
    $('settings-grid').replaceChildren(...groups.map(([title, rows]) => {
      const card = text('section', null, 'card setting');
      const dl = document.createElement('dl');
      for (const [key, value] of rows) dl.append(text('dt', key), text('dd', value));
      card.append(text('h2', title), dl);
      return card;
    }));
  } catch (error) {
    $('settings-grid').replaceChildren(text('p', error.message, 'notice warn'));
  }
}

/* ---------- Status polling ---------- */
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('Dashboard connection lost');
    snapshot = await response.json();
    $('health').textContent = snapshot.healthy ? 'Watcher active' : 'Heartbeat missing or stale';
    $('health').dataset.state = snapshot.healthy ? 'ok' : 'failed';
    $('heartbeat').textContent = snapshot.lastScan ? 'Last watcher scan ' + date(snapshot.lastScan) : 'Waiting for a watcher heartbeat';
    for (const key of ['queued', 'ok', 'failed']) $(key).textContent = snapshot.jobs.filter(job => job.status === key).length;
    $('published').textContent = snapshot.revisions.filter(item => item.url && item.sha256).length;
    $('alert').hidden = !snapshot.alert && snapshot.stateReadable;
    $('alert').textContent = !snapshot.stateReadable ? 'Watcher state is unreadable.' : 'A watcher alert is active. Open Activity Logs for details.';
    renderActive(snapshot.jobs);
    renderRecent();
    renderJobs();
    renderRevisions();
    renderAtsRows();
    const select = $('log-select');
    const previous = select.value;
    select.replaceChildren(...snapshot.logs.map(name => new Option(name, name)));
    if (snapshot.logs.includes(previous)) select.value = previous;
    if (!document.querySelector('[data-view=logs]').hidden) loadLog();
    window.dispatchEvent(new CustomEvent('status', {detail: snapshot}));
  } catch (error) {
    $('health').textContent = error.message;
    $('health').dataset.state = 'failed';
  } finally {
    refreshInFlight = false;
  }
}
$('filter').addEventListener('input', renderJobs);
$('output-filter').addEventListener('input', renderRevisions);

/* ---------- Import (document → reviewed draft) ---------- */
const importStages = new Map([['Reading document', 25], ['Extracting candidate details with Ollama', 50]]);
function showImportProgress(percent, stage) {
  $('import-bar').hidden = false;
  setBar($('import-progress'), percent);
  const label = $('import-stage');
  const message = percent + '% · ' + stage;
  if (label.textContent !== message) label.textContent = message;
  label.hidden = false;
}
function hideImportProgress() { $('import-bar').hidden = true; $('import-stage').hidden = true; }
let importPercent = 0;
function importCandidate(file, extension) {
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  const headers = {'X-Dashboard-Token': token};
  let uploaded = false; let polling = false; let active = true;
  importPercent = 0;
  function showStage(stage) {
    const percent = importStages.get(stage);
    if (percent !== undefined && percent >= importPercent) { importPercent = percent; showImportProgress(percent, stage); }
  }
  showImportProgress(0, 'Uploading file');
  async function pollStage() {
    if (!active || !uploaded || polling) return;
    polling = true;
    try {
      const response = await fetch('/api/import-progress?id=' + id, {headers});
      if (response.ok) { const {stage} = await response.json(); if (active) showStage(stage); }
    } catch { /* The POST response still reports the final outcome. */ } finally { polling = false; }
  }
  const timer = setInterval(pollStage, 500);
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.upload.onprogress = event => {
      if (!uploaded && event.lengthComputable && event.total > 0) {
        importPercent = Math.min(25, Math.floor(25 * event.loaded / event.total));
        showImportProgress(importPercent, 'Uploading file');
      }
    };
    request.upload.onload = () => { uploaded = true; showStage('Reading document'); pollStage(); };
    request.onload = () => {
      const result = request.response;
      if (request.status >= 200 && request.status < 300 && result) return resolve(result);
      showStage(result?.stage);
      reject(new Error(result?.error || 'Import failed (HTTP ' + request.status + '). Try again.'));
    };
    request.onerror = () => reject(new Error('Connection lost during import. Try again.'));
    request.onabort = () => reject(new Error('Import cancelled.'));
    request.open('POST', '/api/draft');
    request.responseType = 'json';
    request.setRequestHeader('X-Dashboard-Token', token);
    request.setRequestHeader('X-Resume-Extension', extension);
    request.setRequestHeader('X-Resume-Import-ID', id);
    request.send(file);
  }).finally(() => { active = false; clearInterval(timer); });
}
function clearDraft() {
  hideImportProgress();
  $('review').hidden = true;
  $('source').value = '';
  $('draft').value = '';
}
$('discard').addEventListener('click', clearDraft);
$('publish').addEventListener('click', async () => {
  const button = $('publish');
  button.disabled = true;
  try {
    const result = await queueCandidate($('draft').value);
    clearDraft();
    $('feedback').textContent = queueMessage(result);
    refresh();
  } catch (error) {
    $('review-feedback').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$('open-in-wizard').addEventListener('click', () => {
  let profile;
  try { profile = JSON.parse($('draft').value); } catch { $('review-feedback').textContent = 'The candidate JSON is not valid.'; return; }
  window.dispatchEvent(new CustomEvent('load-draft', {detail: {profile, source: $('source').value}}));
  clearDraft();
  location.hash = '#/create';
});
$('submit').addEventListener('submit', async event => {
  event.preventDefault();
  const file = $('candidate').files[0];
  if (!file) return;
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const extension = file.name.includes('.') ? '.' + file.name.split('.').pop().toLowerCase() : '';
  if (!$('review').hidden) { $('feedback').textContent = 'Confirm or discard the current draft before importing another file.'; return; }
  button.disabled = true;
  $('candidate').disabled = true;
  $('feedback').textContent = '';
  try {
    if (extension === '.json') {
      hideImportProgress();
      $('feedback').textContent = 'Checking candidate JSON…';
      $('feedback').textContent = queueMessage(await queueCandidate(file));
    } else {
      const draft = await importCandidate(file, extension);
      $('source').value = draft.source;
      $('draft').value = JSON.stringify(draft.profile, null, 2);
      $('review-feedback').textContent = 'Check names, roles and dates against the source.';
      $('review').hidden = false;
      showImportProgress(100, 'Draft ready for review');
      $('draft').focus();
    }
    form.reset();
    refresh();
  } catch (error) {
    $('feedback').textContent = error.message;
  } finally {
    button.disabled = false;
    $('candidate').disabled = false;
  }
});

/* ---------- Start ---------- */
async function loadCatalog() {
  try {
    const response = await fetch('/api/catalog');
    if (!response.ok) throw new Error('Could not load roles and templates.');
    catalog = await response.json();
    window.dispatchEvent(new CustomEvent('catalog', {detail: catalog}));
    if (!document.querySelector('[data-view=templates]').hidden) renderTemplates();
    renderAtsRows();
  } catch (error) {
    $('health').textContent = error.message;
  }
}
window.ResumeApp = {postJSON, queueCandidate, queueMessage, refresh, text, pill, link, date, setBar, artifactUrl,
  artifactLabels, roleName, getSnapshot: () => snapshot, getCatalog: () => catalog};
route();
loadCatalog();
refresh();
setInterval(refresh, 2000);

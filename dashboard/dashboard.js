'use strict';
const $ = id => document.getElementById(id);
const text = (tag, value, className) => {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
};
const date = value => new Date(value * 1000).toLocaleString();
const pill = (label, state) => {
  const node = text('span', label, 'pill');
  node.dataset.state = state;
  return node;
};
const statusLabels = {queued: 'Waiting', ok: 'Completed', failed: 'Awaiting retry', processing: 'Processing'};
const artifactLabels = {'resume.pdf':'Local PDF','overleaf.pdf':'Overleaf PDF','main.tex':'LaTeX',
  'overleaf-project.zip':'Project ZIP','result.json':'Result','audit.json':'Audit','profile.json':'Profile'};
let snapshot;
let refreshInFlight = false;

function renderJobs() {
  if (!snapshot) return;
  const query = $('filter').value.trim().toLowerCase();
  const rows = snapshot.jobs.filter(job => job.name.toLowerCase().includes(query)).map(job => {
    const row = document.createElement('tr');
    const name = text('td', job.name);
    if (job.ai_model) name.append(text('small', 'Model: ' + job.ai_model, 'sha'));
    if (job.error) name.append(text('small', job.error, 'error'));
    const status = document.createElement('td');
    status.append(pill(job.blocked ? 'Blocked · retrying' : statusLabels[job.status] || job.status, job.status));
    row.append(name, status, text('td', date(job.last)));
    return row;
  });
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = text('td', query ? 'No matching files.' : 'The inbox is empty.');
    cell.colSpan = 3;
    row.append(cell);
    rows.push(row);
  }
  $('jobs').replaceChildren(...rows);
}

function renderRevisions() {
  if (!snapshot) return;
  const query = $('output-filter').value.trim().toLowerCase();
  const cards = snapshot.revisions.filter(item => item.candidate.toLowerCase().includes(query)).map(item => {
    const card = document.createElement('article');
    card.className = 'revision';
    const meta = [item.model, item.pages ? item.pages + (item.pages === 1 ? ' page' : ' pages') : '', date(item.modified)].filter(Boolean);
    card.append(text('h3', item.candidate), text('small', meta.join(' · ')));
    const links = document.createElement('div');
    links.className = 'links';
    if (item.url) {
      const a = text('a', 'Open in Overleaf');
      a.href = item.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      links.append(a);
    }
    for (const name of item.files) {
      const a = text('a', artifactLabels[name] || name);
      a.href = '/api/artifact?' + new URLSearchParams({key: item.key, name});
      a.target = '_blank';
      a.rel = 'noopener';
      links.append(a);
    }
    card.append(links);
    if (item.ats) card.append(text('small', 'ATS keyword coverage ' + item.ats.score + '%'));
    if (item.sha256) card.append(text('small', 'Overleaf PDF SHA-256 ' + item.sha256, 'sha'));
    return card;
  });
  $('revisions').replaceChildren(...(cards.length ? cards : [text('p', 'No matching resume revisions.', 'empty')]));
}

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
    $('alert').textContent = !snapshot.stateReadable ? 'Watcher state is unreadable.' : 'A watcher alert is active. Open Logs for details.';
    if (typeof renderWorkflow === 'function') renderWorkflow(snapshot.jobs);
    renderJobs();
    renderRevisions();
    const select = $('log-select');
    const previous = select.value;
    select.replaceChildren(...snapshot.logs.map(name => new Option(name, name)));
    if (snapshot.logs.includes(previous)) select.value = previous;
    if (!$('logs').hidden) loadLog();
  } catch (error) {
    $('health').textContent = error.message;
    $('health').dataset.state = 'failed';
  } finally {
    refreshInFlight = false;
  }
}

for (const button of document.querySelectorAll('[data-panel]')) {
  button.addEventListener('click', () => {
    for (const item of document.querySelectorAll('[data-panel]')) {
      const selected = item === button;
      item.setAttribute('aria-pressed', String(selected));
      $(item.dataset.panel).hidden = !selected;
    }
    if (button.dataset.panel === 'logs') loadLog();
  });
}
$('filter').addEventListener('input', renderJobs);
$('output-filter').addEventListener('input', renderRevisions);
$('log-select').addEventListener('change', loadLog);
const token = document.querySelector('meta[name=dashboard-token]').content;

async function submitFile(path, data, headers) {
  const response = await fetch(path, {method: 'POST', body: data,
    headers: {...headers, 'X-Dashboard-Token': token}});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

const importStages = new Map([
  ['Reading document', 25],
  ['Extracting candidate details with Ollama', 50],
]);

function showImportProgress(percent, stage) {
  const bar = $('import-progress');
  bar.value = percent;
  bar.hidden = false;
  const label = $('import-stage');
  const message = percent + '% · ' + stage;
  if (label.textContent !== message) label.textContent = message;
  label.hidden = false;
}

function hideImportProgress() {
  $('import-progress').hidden = true;
  $('import-stage').hidden = true;
}

function importCandidate(file, extension) {
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');
  const headers = {'X-Dashboard-Token': token};
  let uploaded = false;
  let polling = false;
  let active = true;

  function showStage(stage) {
    const percent = importStages.get(stage);
    if (percent !== undefined && percent >= $('import-progress').value) {
      showImportProgress(percent, stage);
    }
  }

  showImportProgress(0, 'Uploading file');
  async function pollStage() {
    if (!active || !uploaded || polling) return;
    polling = true;
    try {
      const response = await fetch('/api/import-progress?id=' + id, {headers});
      if (response.ok) {
        const {stage} = await response.json();
        if (active) showStage(stage);
      }
    } catch {
      // The POST response still reports the final outcome.
    } finally {
      polling = false;
    }
  }
  const timer = setInterval(pollStage, 500);

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.upload.onprogress = event => {
      if (!uploaded && event.lengthComputable && event.total > 0) {
        const percent = Math.min(25, Math.floor(25 * event.loaded / event.total));
        showImportProgress(percent, 'Uploading file');
      }
    };
    request.upload.onload = () => {
      uploaded = true;
      showStage('Reading document');
      pollStage();
    };
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
  }).finally(() => {
    active = false;
    clearInterval(timer);
  });
}

function queueCandidate(data) {
  return submitFile('/api/queue', data, {'Content-Type': 'application/json'});
}

function queueMessage(result) {
  return result.created ? 'Queued. The watcher will pick it up.' : 'This candidate revision is already in the inbox.';
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

$('submit').addEventListener('submit', async event => {
  event.preventDefault();
  const file = $('candidate').files[0];
  if (!file) return;
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  const extension = file.name.includes('.') ? '.' + file.name.split('.').pop().toLowerCase() : '';
  if (!$('review').hidden) {
    $('feedback').textContent = 'Confirm or discard the current draft before importing another file.';
    button.disabled = false;
    return;
  }
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
    event.target.reset();
    refresh();
  } catch (error) {
    $('feedback').textContent = error.message;
  } finally {
    button.disabled = false;
    $('candidate').disabled = false;
  }
});
refresh();
setInterval(refresh, 2000);

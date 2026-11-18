'use strict';

const byId = id => document.getElementById(id);
const form = byId('ai-form');
const fileControl = byId('auto-file');
const role = form.elements.role;
const required = [['full_name','Full name'], ['email','Email','email'],
  ['years_of_experience','Years of experience','number']];
const optional = [['phone','Phone','tel'], ['location','Location'], ['current_company','Current company'],
  ['linkedin','LinkedIn','url'], ['github','GitHub','url'], ['portfolio','Portfolio','url']];
const collections = [
  ['work_experience','Work experience',[['company','Employer'],['position','Job title'],['start','Start date'],['end','End date'],['location','Location']]],
  ['education','Education',[['university','Institution'],['degree','Degree'],['specialization','Specialization'],['graduation_year','Graduation year'],['cgpa','CGPA']]],
  ['projects','Completed projects',[['name','Project name']]],
  ['coding_platforms','Coding platforms',[['platform','Platform'],['url','Profile URL','url']]]
];
let requestRunning = false;
let preparing = false;
let pending = null;
let tracked = null;
let nextSend = 0;
let sendDelay = 2000;
let observedPercent = 0;

function element(tag, text) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
}

function input([key, label, type = 'text'], mandatory = false) {
  const node = element('label', label);
  const field = element('input');
  field.name = key;
  field.type = type;
  field.required = mandatory;
  field.maxLength = 250;
  if (type === 'number') { field.min = '0'; field.max = '60'; field.step = '0.5'; }
  node.append(field);
  return node;
}

function addSection([key, title, fields]) {
  const details = element('details');
  const summary = element('summary', title);
  const rows = element('div');
  rows.dataset.entries = key;
  const add = element('button', 'Add entry');
  add.type = 'button';
  add.className = 'secondary';
  add.addEventListener('click', () => {
    if (rows.children.length >= 40) return;
    const row = element('div');
    row.className = 'auto-row auto-grid';
    const remove = element('button', 'Remove entry');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.addEventListener('click', () => { row.remove(); update(); });
    row.append(...fields.map(field => input(field)), remove);
    rows.append(row);
    update();
    row.querySelector('input').focus();
  });
  function update() { summary.textContent = title + ' (' + rows.children.length + ')'; }
  details.append(summary, rows, add);
  byId('auto-lists').append(details);
}

function mode() {
  const manual = form.elements.method.value === 'manual';
  byId('auto-manual').hidden = !manual;
  byId('auto-manual').disabled = !manual;
  byId('auto-upload').hidden = manual;
  fileControl.disabled = manual;
  return manual;
}

function candidate() {
  const info = Object.fromEntries([...required, ...optional].map(([key]) => [key, form.elements[key].value]));
  info.years_of_experience = Number(info.years_of_experience);
  for (const [key] of collections) {
    const rows = form.querySelector('[data-entries="' + key + '"]').children;
    info[key] = [...rows].map(row =>
      Object.fromEntries([...row.querySelectorAll('input')].map(item => [item.name, item.value])));
  }
  return info;
}

async function body() {
  const data = {role: role.value, consent: 'resumeops-auto-1'};
  if (mode()) return {...data, candidate: candidate()};
  const file = fileControl.files[0];
  const extension = file?.name.match(/\.(pdf|docx|txt)$/i)?.[1]?.toLowerCase();
  if (!extension || !file.size || file.size > 12 * 1024 * 1024) {
    throw new Error('Choose a PDF, DOCX or TXT file up to 12 MB.');
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return {...data, extension, document: btoa(binary)};
}

function progress(stage, value, error = '') {
  observedPercent = value;
  byId('auto-status').hidden = false;
  byId('auto-stage').textContent = stage;
  byId('auto-progress').value = value;
  byId('auto-percent').textContent = value + '%';
  byId('auto-error').textContent = error;
}

async function send() {
  if (requestRunning || !pending) return;
  requestRunning = true;
  const submit = byId('auto-submit');
  submit.disabled = true;
  progress('Submitting candidate evidence', 0);
  nextSend = Date.now() + sendDelay;
  sendDelay = Math.min(sendDelay * 2, 60000);
  try {
    const response = await ResumeAuth.call('/api/automation', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(pending)});
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 400 || response.status === 413) {
        await ResumeRecovery.remove('automation-pending');
        pending = null;
      }
      throw new Error(result.error || 'Unable to save the submission.');
    }
    tracked = result.id;
    sendDelay = 2000;
    await ResumeRecovery.remove('automation-pending');
    pending = null;
    form.reset();
    for (const rows of form.querySelectorAll('[data-entries]')) {
      rows.replaceChildren();
      const summary = rows.previousElementSibling;
      summary.textContent = summary.textContent.replace(/\(\d+\)$/, '(0)');
    }
    mode();
    await refresh();
  } catch (error) {
    progress('Submission needs attention', 0, error.message);
  } finally {
    submit.disabled = false;
    requestRunning = false;
  }
}

function links(job) {
  const box = element('div');
  box.className = 'auto-links';
  for (const [label, filename] of [['Download PDF','resume.pdf'], ['Download LaTeX','main.tex'],
    ['Download Overleaf PDF','overleaf.pdf']]) {
    const link = element('a', label);
    link.href = '/api/artifact?' + new URLSearchParams({key:job.revision, name:filename, download:'1'});
    link.addEventListener('click', async event => {
      event.preventDefault();
      try {
        const action = {url:link.getAttribute('href'), name:filename};
        await ResumeRecovery.put('automation-download', action);
        await ResumeRecovery.download(action, 'automation-download');
      } catch (error) { progress(job.stage, 100, error.message); }
    });
    box.append(link);
  }
  if (job.overleaf && /^https:\/\/www\.overleaf\.com\/project\/[A-Za-z0-9_-]+$/.test(job.overleaf)) {
    const link = element('a', 'Open Overleaf project');
    link.href = job.overleaf;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    box.append(link);
  }
  return box;
}

async function refresh() {
  const response = await ResumeAuth.call('/api/automation');
  if (!response.ok) throw new Error('Could not refresh your resumes.');
  const {jobs} = await response.json();
  byId('auto-empty').hidden = jobs.length > 0;
  const current = jobs.find(job => job.id === tracked) || jobs[0];
  if (current && !pending) progress(current.stage, current.percent, current.error || '');
  const list = byId('auto-job-list');
  const snapshot = JSON.stringify(jobs);
  if (list.dataset.snapshot === snapshot) return;
  list.dataset.snapshot = snapshot;
  list.replaceChildren(...jobs.map(job => {
    const item = element('li');
    item.append(element('strong', job.stage + ' · ' + job.percent + '%'));
    if (job.status === 'complete') {
      if (job.ats) item.append(element('span', 'ATS readiness ' + job.ats.ats_readiness + '/100'));
      item.append(links(job));
    }
    return item;
  }));
}

async function init() {
  byId('auto-basics').append(...required.map(field => input(field, true)));
  byId('auto-optional').append(...optional.map(field => input(field)));
  collections.forEach(addSection);
  form.addEventListener('change',mode);
  form.addEventListener('invalid', event => {
    const details = event.target.closest('details:not([open])');
    if (details) { details.open = true; event.target.getBoundingClientRect(); }
  },true);
  mode();
  await ResumeAuth.ready();
  pending = await ResumeRecovery.read('automation-pending');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (requestRunning || preparing) return;
    if (pending) { await send(); return; }
    preparing = true;
    try {
      pending = await body();
      await ResumeRecovery.put('automation-pending', pending);
      await send();
    } catch (error) {
      pending = null;
      progress('Submission needs attention', 0, error.message);
    } finally { preparing = false; }
  });
  if (pending) await send();
  const download = await ResumeRecovery.read('automation-download');
  if (download) {
    await ResumeRecovery.download(download, 'automation-download')
      .catch(error => progress('Download needs attention', observedPercent, error.message));
  }
  while (true) {
    if (pending && Date.now() >= nextSend) await send();
    await refresh().catch(error => progress('Connection interrupted', observedPercent, error.message));
    await new Promise(resolve => setTimeout(resolve, 1300));
  }
}
init().catch(error => progress('Builder unavailable', 0, error.message));

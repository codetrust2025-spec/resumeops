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
  // A null value means the job has stopped: no percentage that could suggest it is still running.
  if (value !== null) observedPercent = value;
  byId('auto-status').hidden = false;
  byId('auto-stage').textContent = stage;
  byId('auto-progress').value = value ?? 0;
  byId('auto-percent').textContent = value === null ? '' : value + '%';
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
    if (!tracked) byId('auto-status').hidden = true;
    fieldError(controlFor(error.message), error.message);
  } finally {
    submit.disabled = false;
    requestRunning = false;
  }
}

// Validation messages appear once, directly under the field they concern, and clear on edit.
function clearFieldErrors(scope) {
  for (const note of scope.querySelectorAll('.field-error')) note.remove();
  for (const control of scope.querySelectorAll('[aria-invalid="true"]')) control.removeAttribute('aria-invalid');
}

function fieldError(control, message) {
  const host = control.closest('label') || control.closest('details') || control;
  host.parentElement.querySelectorAll(':scope > .field-error').forEach(note => {
    if (note.previousElementSibling === host) note.remove();
  });
  const note = element('p', message);
  note.className = 'field-error';
  note.setAttribute('role', 'alert');
  host.after(note);
  control.setAttribute('aria-invalid', 'true');
  const clear = () => { note.remove(); control.removeAttribute('aria-invalid'); };
  for (const type of ['input', 'change']) control.addEventListener(type, clear, {once: true});
  if (control.focus) control.focus();
}

function controlFor(message) {
  const rules = [
    [/full name/i, () => form.elements.full_name],
    [/target role/i, () => role],
    [/consent|confirm the facts/i, () => byId('auto-consent')],
    [/pdf, docx or txt|uploaded file|resume file|word document|12 mb/i, () => fileControl],
    [/email/i, () => form.elements.email],
    [/experience must be|years/i, () => form.elements.years_of_experience],
    [/work entries|employer and position|work_experience/i, () => form.querySelector('[data-entries="work_experience"]')],
    [/education entries|degree and university|education/i, () => form.querySelector('[data-entries="education"]')],
  ];
  for (const [pattern, find] of rules) {
    const control = pattern.test(message) ? find() : null;
    if (control && !control.disabled && !control.closest('[hidden]')) return control;
  }
  return byId('auto-submit');
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

const ACTIVE = new Set(['queued', 'processing', 'handed_off', 'retrying']);
const FIELD_WORDS = {company: 'employer', position: 'job title', start: 'start date', end: 'end date',
  institution: 'institution', degree: 'degree', full_name: 'full name', email: 'email address'};

function retryText(job) {
  const at = job.next_retry ? new Date(job.next_retry * 1000).toLocaleTimeString() : 'shortly';
  return (job.error ? job.error + ' ' : '') + 'Retrying automatically at ' + at + '. No action needed.';
}

// Raw validation text from submissions made before editable questions existed.
function explain(error) {
  const raw = /^(experience|education)\[(\d+)\]\.(\w+): missing required information$/.exec(error || '');
  if (!raw) return error || 'This submission stopped.';
  return (raw[1] === 'experience' ? 'Work entry ' : 'Education entry ') + (Number(raw[2]) + 1) + ' has no ' +
    (FIELD_WORDS[raw[3]] || raw[3]) + '. This upload stopped before ResumeOps could ask you for it; ' +
    'upload the resume again to answer it.';
}

// The panel follows only the submission made on this page, or one still running after a reload.
// Finished and stopped jobs are reported once, in My resumes, so a fresh form never shows an old error.
function showCurrent(jobs) {
  if (!tracked) tracked = jobs.find(job => ACTIVE.has(job.status))?.id || null;
  const job = jobs.find(item => item.id === tracked);
  if (!job || pending) { if (!pending && !requestRunning) byId('auto-status').hidden = true; return; }
  if (job.status === 'needs_input') progress('Needs your input', null, 'Answer the questions under My resumes to continue.');
  else if (job.status === 'blocked') progress('Stopped', null, 'See My resumes for the reason.');
  else progress(job.stage, job.percent, job.status === 'retrying' ? retryText(job) : '');
}

function answerForm(job) {
  const box = element('form');
  box.className = 'auto-answer';
  box.noValidate = true;
  box.append(element('p', job.error || 'Your resume does not state some details.'));
  for (const question of job.questions || []) {
    const label = element('label', question.label);
    const field = element('input');
    field.name = question.id;
    field.type = question.field === 'email' ? 'email' : 'text';
    field.maxLength = 250;
    field.required = true;
    field.value = question.value || '';
    const hint = element('span', question.explanation);
    hint.className = 'field-hint';
    label.append(field, hint);
    box.append(label);
  }
  const submit = element('button', 'Continue');
  submit.type = 'submit';
  submit.className = 'primary';
  box.append(submit);
  box.addEventListener('submit', async event => {
    event.preventDefault();
    clearFieldErrors(box);
    const fields = [...box.querySelectorAll('input')];
    const empty = fields.find(field => !field.value.trim());
    if (empty) { fieldError(empty, 'Enter this detail to continue.'); return; }
    submit.disabled = true;
    try {
      const response = await ResumeAuth.call('/api/automation/answer', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: job.id, answers: Object.fromEntries(fields.map(field => [field.name, field.value]))})});
      const result = await response.json();
      if (!response.ok) {
        const target = result.field && fields.find(field => field.name === result.field);
        fieldError(target || submit, result.error || 'Could not save your answers.');
        return;
      }
      tracked = job.id;
      await refresh();
    } catch (error) {
      fieldError(submit, error.message);
    } finally {
      submit.disabled = false;
    }
  });
  return box;
}

function jobItem(job) {
  const item = element('li');
  item.dataset.status = job.status;
  item.dataset.id = job.id;
  if (job.status === 'needs_input') {
    item.append(element('strong', 'Needs your input'), answerForm(job));
  } else if (job.status === 'blocked') {
    item.append(element('strong', 'Needs attention'), element('span', explain(job.error)));
  } else {
    item.append(element('strong', job.stage + ' · ' + job.percent + '%'));
    if (job.status === 'retrying') item.append(element('span', retryText(job)));
  }
  for (const notice of job.notice || []) item.append(element('span', notice));
  if (job.status === 'complete') {
    if (job.ats) item.append(element('span', 'ATS readiness ' + job.ats.ats_readiness + '/100'));
    item.append(links(job));
  }
  return item;
}

// Keep each job's element (and anything typed into it) until that job itself changes.
const rendered = new Map();
function renderJobs(jobs) {
  const list = byId('auto-job-list');
  const items = jobs.map(job => {
    const json = JSON.stringify(job);
    const known = rendered.get(job.id);
    if (known && known.json === json) return known.item;
    const item = jobItem(job);
    rendered.set(job.id, {json, item});
    return item;
  });
  items.forEach((item, index) => {
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  });
  while (list.children.length > items.length) list.lastElementChild.remove();
}

async function refresh() {
  const response = await ResumeAuth.call('/api/automation');
  if (!response.ok) throw new Error('Could not refresh your resumes.');
  const {jobs} = await response.json();
  byId('auto-empty').hidden = jobs.length > 0;
  showCurrent(jobs);
  renderJobs(jobs);
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
  form.addEventListener('reset', () => clearFieldErrors(form));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (requestRunning || preparing) return;
    clearFieldErrors(form);
    if (pending) { await send(); return; }
    preparing = true;
    try {
      pending = await body();
      await ResumeRecovery.put('automation-pending', pending);
      await send();
    } catch (error) {
      pending = null;
      fieldError(controlFor(error.message), error.message);
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

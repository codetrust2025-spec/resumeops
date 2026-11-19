'use strict';

const byId = id => document.getElementById(id);
const form = byId('ai-form');
const fileControl = byId('auto-file');
const role = form.elements.role;
const customRole = byId('auto-custom-role');
const CUSTOM = 'Custom Role';
const required = [['full_name','Full name'], ['phone','Phone','tel']];
const optional = [['email','Email (optional)','email'],
  ['years_of_experience','Years of experience (optional)','number'],
  ['location','Location'], ['current_company','Current company'],
  ['linkedin','LinkedIn','url'], ['github','GitHub','url'], ['portfolio','Portfolio','url'],
  ['certifications','Certifications (one per line)','textarea'], ['achievements','Achievements (one per line)','textarea']];
const skillsField = ['skills','Technical skills (comma separated)','textarea'];
const collections = [
  ['work_experience','Work experience',[['company','Employer'],['position','Job title'],['start','Start date'],['end','End date'],['location','Location'],
    ['responsibilities','Responsibilities (one per line)','textarea']]],
  ['education','Education',[['university','Institution'],['degree','Degree'],['specialization','Specialization'],['graduation_year','Graduation year'],['cgpa','CGPA']]],
  ['projects','Completed projects',[['name','Project name'],['stack','Tech stack (comma separated)'],
    ['description','What you built (one point per line)','textarea']]],
  ['coding_platforms','Coding platforms',[['platform','Platform'],['url','Profile URL','url']]]
];
let catalogNames = [];
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
  const field = element(type === 'textarea' ? 'textarea' : 'input');
  field.name = key;
  if (type === 'textarea') { field.rows = 3; field.maxLength = 3000; node.classList.add('auto-wide'); }
  else { field.type = type; field.maxLength = 250; }
  field.required = mandatory;
  if (type === 'number') { field.min = '0'; field.max = '60'; field.step = '0.5'; }
  node.append(field);
  return node;
}

// Searchable role picker: the catalog roles plus "Custom Role". Any other title is a custom role.
async function loadRoles() {
  try {
    const response = await ResumeAuth.call('/api/catalog?visible=1');
    if (!response.ok) return;
    const {roles} = await response.json();
    catalogNames = roles.map(item => item.name);
    byId('auto-role-options').replaceChildren(...[...catalogNames, CUSTOM].map(name => {
      const option = element('option');
      option.value = name;
      return option;
    }));
  } catch (error) { /* the field still accepts any valid title */ }
}

function roleChanged() {
  const custom = role.value.trim().toLowerCase() === CUSTOM.toLowerCase();
  byId('auto-custom-wrap').hidden = !custom;
  customRole.required = custom;
  const known = catalogNames.some(name => name.toLowerCase() === role.value.trim().toLowerCase());
  byId('auto-role-hint').textContent = custom || !role.value.trim() || known ? '' :
    'Custom role: keywords come from job-market data instead of the role catalog.';
}

function selectedRole() {
  return role.value.trim().toLowerCase() === CUSTOM.toLowerCase() ? customRole.value.trim() : role.value.trim();
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
    row.querySelector('input, textarea').focus();
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
  const info = Object.fromEntries([...required, ...optional, skillsField].map(([key]) => [key, form.elements[key].value]));
  info.years_of_experience = info.years_of_experience.trim() === '' ? null : Number(info.years_of_experience);
  info.background = byId('auto-background').value;
  for (const [key] of collections) {
    const rows = form.querySelector('[data-entries="' + key + '"]').children;
    info[key] = [...rows].map(row =>
      Object.fromEntries([...row.querySelectorAll('input, textarea')].map(item => [item.name, item.value])));
  }
  return info;
}

async function body() {
  if (role.value.trim().toLowerCase() === CUSTOM.toLowerCase() && !customRole.value.trim()) {
    const error = new Error('Enter the custom role title.');
    error.control = customRole;
    throw error;
  }
  const data = {role: selectedRole(), consent: 'resumeops-auto-1'};
  if (mode()) return {...data, candidate: candidate()};
  const files = Array.from(fileControl.files);
  if (!files.length || files.length > 10 || files.reduce((sum, file) => sum + file.size, 0) > 12 * 1024 * 1024) {
    throw new Error('Choose up to 10 candidate documents, up to 12 MB total.');
  }
  const documents = [];
  for (const file of files) {
    const extension = file.name.match(/\.(pdf|docx|txt|jpg|jpeg|png)$/i)?.[1]?.toLowerCase();
    if (!extension || !file.size) throw new Error('Choose readable PDF, DOCX, TXT, JPG or PNG documents.');
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    }
    documents.push({extension, document: btoa(binary)});
  }
  return {...data, documents};
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
    // Show the new submission: first page, no search or filters hiding it.
    page = 0;
    byId('auto-search').value = '';
    byId('auto-filter-role').value = '';
    byId('auto-filter-status').value = '';
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
    [/custom role/i, () => customRole],
    [/target role/i, () => role],
    [/consent|confirm the facts/i, () => byId('auto-consent')],
    [/pdf|docx|candidate documents|uploaded file|resume file|word document|12 mb/i, () => fileControl],
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

// Compact action buttons: short visible text, full accessible names ("Download PDF", "Open Overleaf project").
function links(job) {
  const box = element('div');
  box.className = 'auto-links';
  for (const [text, label, filename] of [['PDF', 'Download PDF', 'resume.pdf'], ['LaTeX', 'Download LaTeX', 'main.tex'],
    ['Overleaf PDF', 'Download Overleaf PDF', 'overleaf.pdf']]) {
    const link = element('a', text);
    link.setAttribute('aria-label', label);
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
  const preview = element('a', 'Preview');
  preview.setAttribute('aria-label', 'Preview PDF');
  preview.href = '/api/artifact?' + new URLSearchParams({key: job.revision, name: 'resume.pdf'});
  preview.target = '_blank';
  preview.rel = 'noopener';
  box.append(preview);
  if (job.overleaf && /^https:\/\/www\.overleaf\.com\/project\/[A-Za-z0-9_-]+$/.test(job.overleaf)) {
    const link = element('a', 'Open Overleaf');
    link.setAttribute('aria-label', 'Open Overleaf project');
    link.href = job.overleaf;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    box.append(link);
  }
  return box;
}

// Explainable ATS readiness: category points, what lost points, and suggested (not added) keywords.
function atsDetails(ats) {
  const box = element('details');
  box.className = 'auto-ats';
  box.append(element('summary', 'ATS readiness ' + ats.ats_readiness + '/100'));
  if (ats.categories) {
    const table = element('table');
    for (const category of ats.categories) {
      const row = element('tr');
      row.append(element('th', category.label), element('td', category.points + ' / ' + category.max));
      table.append(row);
    }
    box.append(table);
  }
  const lists = [['Recommendations', ats.recommendations || ats.suggestions || []],
    ['Your skills that match the role', ats.role_relevant_skills || ats.matched || []],
    ['Suggested keywords (add only if you have used them)', ats.suggested_skills || ats.missing || []]];
  for (const [title, values] of lists) {
    if (!values.length) continue;
    box.append(element('h4', title));
    const list = element('ul');
    list.append(...values.slice(0, 15).map(value => element('li', value)));
    box.append(list);
  }
  return box;
}

// Suggested projects: never resume content until the candidate includes (and may edit) one.
function proposalsForm(job) {
  const box = element('form');
  box.className = 'auto-answer auto-proposals';
  box.noValidate = true;
  box.append(element('p', job.error || 'Suggested projects are ready for your review.'));
  job.proposals.forEach((proposal, index) => {
    const card = element('fieldset');
    card.className = 'auto-proposal';
    card.dataset.index = index;
    card.append(element('legend', 'Suggested project ' + (index + 1) + ' (AI proposal, not yet confirmed)'));
    const name = element('input'); name.name = 'name'; name.value = proposal.name; name.maxLength = 70;
    const nameLabel = element('label', 'Project name'); nameLabel.append(name);
    const facts = element('dl');
    for (const [term, value] of [['Problem', proposal.problem], ['Architecture', proposal.architecture],
      ['Key features', (proposal.features || []).join('; ')], ['Responsibilities', proposal.responsibilities]]) {
      if (value) facts.append(element('dt', term), element('dd', value));
    }
    const stack = element('input'); stack.name = 'stack'; stack.value = proposal.stack.join(', '); stack.maxLength = 300;
    const stackLabel = element('label', 'Tech stack (comma separated)'); stackLabel.append(stack);
    const bullets = element('textarea'); bullets.name = 'bullets'; bullets.rows = 4; bullets.maxLength = 1500;
    bullets.value = proposal.bullets.join('\n');
    const bulletsLabel = element('label', 'Bullets (one per line)'); bulletsLabel.append(bullets);
    const choice = element('div');
    choice.className = 'auto-choice';
    for (const [value, text] of [['include', 'I built this: include it'], ['skip', 'Skip']]) {
      const option = element('label'); const radio = element('input');
      radio.type = 'radio'; radio.name = 'decision-' + index; radio.value = value;
      option.append(radio, ' ' + text); choice.append(option);
    }
    card.append(nameLabel, facts, stackLabel, bulletsLabel, choice);
    box.append(card);
  });
  const submit = element('button', 'Save project choices');
  submit.type = 'submit';
  submit.className = 'primary';
  box.append(submit);
  box.addEventListener('submit', async event => {
    event.preventDefault();
    clearFieldErrors(box);
    const cards = [...box.querySelectorAll('.auto-proposal')];
    const undecided = cards.find(card => !card.querySelector('input[type=radio]:checked'));
    if (undecided) { fieldError(undecided.querySelector('input[type=radio]'), 'Choose include or skip.'); return; }
    const decisions = cards.map(card => {
      const include = card.querySelector('input[type=radio]:checked').value === 'include';
      return include ? {include, name: card.querySelector('[name=name]').value, stack: card.querySelector('[name=stack]').value,
        bullets: card.querySelector('[name=bullets]').value} : {include};
    });
    submit.disabled = true;
    try {
      const response = await ResumeAuth.call('/api/automation/projects', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: job.id, decisions})});
      const result = await response.json();
      if (!response.ok) {
        const card = /^project\.(\d+)$/.exec(result.field || '');
        fieldError(card ? cards[Number(card[1])].querySelector('[name=bullets]') : submit, result.error || 'Could not save.');
        return;
      }
      tracked = job.id;
      await refresh();
    } catch (error) { fieldError(submit, error.message); }
    finally { submit.disabled = false; }
  });
  return box;
}

function regenerateForm(job) {
  const box = element('form');
  box.className = 'auto-regenerate';
  const label = element('label', 'Regenerate for another role');
  const field = element('input');
  field.name = 'role'; field.setAttribute('list', 'auto-role-options'); field.maxLength = 100;
  field.placeholder = 'Search roles';
  label.append(field);
  const submit = element('button', 'Regenerate');
  submit.type = 'submit'; submit.className = 'secondary';
  box.append(label, submit);
  box.addEventListener('submit', async event => {
    event.preventDefault();
    clearFieldErrors(box);
    if (!field.value.trim() || field.value.trim().toLowerCase() === CUSTOM.toLowerCase()) {
      fieldError(field, 'Choose a role or type a role title.'); return;
    }
    submit.disabled = true;
    try {
      const response = await ResumeAuth.call('/api/automation/regenerate', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: job.id, role: field.value.trim()})});
      const result = await response.json();
      if (!response.ok) { fieldError(field, result.error || 'Could not regenerate.'); return; }
      tracked = result.id;
      await refresh();
    } catch (error) { fieldError(field, error.message); }
    finally { submit.disabled = false; }
  });
  return box;
}

function meta(job) {
  const when = job.created ? new Date(job.created * 1000).toLocaleString() : '';
  return [job.candidate || 'Candidate', job.role, when].filter(Boolean).join(' · ');
}

const ACTIVE = new Set(['queued', 'processing', 'handed_off', 'retrying']);
const FIELD_WORDS = {company: 'employer', position: 'job title', start: 'start date', end: 'end date',
  bullets: 'work activity', source_text: 'work or education information',
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
    'upload the document again to answer it.';
}

// The panel follows only the submission made on this page, or one still running after a reload.
// Finished and stopped jobs are reported once, in My resumes, so a fresh form never shows an old error.
function showCurrent(jobs) {
  if (!tracked) tracked = jobs.find(job => ACTIVE.has(job.status))?.id || null;
  const job = jobs.find(item => item.id === tracked);
  if (!job || pending) { if (!pending && !requestRunning) byId('auto-status').hidden = true; return; }
  if (job.status === 'needs_input') progress('Needs your input', null, (job.proposals || []).length ?
    'Review the suggested projects under My resumes to continue.' : 'Answer the questions under My resumes to continue.');
  else if (job.status === 'blocked') progress('Stopped', null, 'See My resumes for the reason.');
  else progress(job.stage, job.percent, job.status === 'retrying' ? retryText(job) : '');
}

function answerForm(job) {
  const box = element('form');
  box.className = 'auto-answer';
  box.noValidate = true;
  box.append(element('p', job.error || 'Your documents do not state some required details.'));
  for (const question of job.questions || []) {
    const label = element('label', question.label);
    const field = element(question.field === 'source_text' ? 'textarea' : 'input');
    field.name = question.id;
    if (field.tagName === 'INPUT') field.type = question.field === 'email' ? 'email' : 'text';
    field.maxLength = question.field === 'source_text' ? 7500 : 250;
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
    const fields = [...box.querySelectorAll('input, textarea')];
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

// One compact row per resume: "candidate · role · date" and the ATS score, then the real status and the
// actions. Questions and project choices stay open on the row; the ATS breakdown and Regenerate open
// under Details. A row keeps exactly one <strong>: its status.
// Permanent deletion. The server checks the same confirmation phrases and deletes only this account's
// resumes; a failure deletes nothing, and its message says so.
function deletedText({deleted = 0, kept = []}) {
  let text = deleted ? 'Permanently deleted ' + deleted + (deleted === 1 ? ' resume.' : ' resumes.') : 'Nothing was deleted.';
  const busy = kept.filter(item => item.reason === 'in_progress').length;
  const locked = kept.length - busy;
  if (busy) text += ' ' + busy + (busy === 1 ? ' resume being prepared was kept; delete it' : ' resumes being prepared were kept; delete them') + ' when finished.';
  if (locked) text += ' ' + locked + (locked === 1 ? ' protected record was' : ' protected records were') + ' kept.';
  return text;
}

async function deleteResumes(button, job, phrase) {
  button.disabled = true;
  const status = byId('auto-delete-status');
  status.textContent = '';
  try {
    const response = await ResumeAuth.call('/api/automation/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(job ? {id: job.id, confirm: 'DELETE'} : {all: true, confirm: phrase}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Could not delete. Nothing was deleted; try again.');
    try { await refresh(); } catch { /* the deletion itself succeeded; the next poll refreshes the list */ }
    status.textContent = deletedText(result);
    return '';
  } catch (error) {
    status.textContent = error.message;
    return error.message;
  } finally {
    button.disabled = false;
  }
}

function deleteOne(button, job) {
  if (!window.confirm('Permanently delete ' + meta(job) + '? Its PDF, LaTeX and generated files are deleted. '
      + 'This cannot be undone.')) return;
  deleteResumes(button, job);
}

function openDeleteAll() {
  const dialog = byId('auto-delete-dialog');
  byId('auto-delete-phrase').value = '';
  byId('auto-delete-error').textContent = '';
  byId('auto-delete-confirm').disabled = true;
  dialog.showModal();
  byId('auto-delete-phrase').focus();
}

async function confirmDeleteAll(event) {
  event.preventDefault();
  const phrase = byId('auto-delete-phrase').value;
  if (phrase !== 'DELETE ALL') return;
  const problem = await deleteResumes(byId('auto-delete-confirm'), null, phrase);
  if (problem) byId('auto-delete-error').textContent = problem;
  else byId('auto-delete-dialog').close();
}

function jobItem(job) {
  const item = element('li');
  item.dataset.status = job.status;
  item.dataset.id = job.id;
  const top = element('div');
  top.className = 'auto-line';
  const label = element('span', meta(job));
  label.className = 'auto-meta';
  top.append(label);
  if (job.status === 'complete' && job.ats) {
    const score = element('span', 'ATS ' + job.ats.ats_readiness);
    score.className = 'auto-score';
    score.title = 'ATS readiness out of 100';
    top.append(score);
  }
  const line = element('div');
  line.className = 'auto-line';
  const state = element('div');
  state.className = 'auto-state';
  const body = element('div');
  body.className = 'auto-body';
  body.id = 'auto-body-' + job.id.slice(0, 16);
  if (job.status === 'needs_input') {
    state.append(element('strong', 'Needs your input'));
    body.append((job.proposals || []).length ? proposalsForm(job) : answerForm(job));
  } else if (job.status === 'blocked') {
    state.append(element('strong', 'Needs attention'), element('span', explain(job.error)));
  } else if (job.status === 'complete') {
    state.append(element('strong', 'Complete'));
  } else {
    const bar = element('progress');
    bar.max = 100;
    bar.value = job.percent;
    bar.setAttribute('aria-label', 'Progress ' + job.percent + '%');
    state.append(element('strong', job.status === 'retrying' ? 'Retrying' : 'In progress'), bar,
      element('span', job.stage + ' · ' + job.percent + '%'));
    if (job.status === 'retrying') state.append(element('span', retryText(job)));
  }
  line.append(state);
  const remove = element('button', 'Delete');
  remove.type = 'button';
  remove.setAttribute('aria-label', 'Delete ' + meta(job));
  remove.addEventListener('click', () => deleteOne(remove, job));
  for (const notice of job.notice || []) body.append(element('span', notice));
  if (job.status === 'complete') {
    if (job.ats) body.append(atsDetails(job.ats));
    const regenerate = job.regenerable ? regenerateForm(job) : null;
    if (regenerate) body.append(regenerate);
    const actions = links(job);
    const open = show => {
      body.hidden = !show;
      details.setAttribute('aria-expanded', String(show));
      details.textContent = show ? 'Hide details' : 'Details';
    };
    const details = element('button', 'Details');
    details.type = 'button';
    details.setAttribute('aria-controls', body.id);
    details.addEventListener('click', () => open(body.hidden));
    if (regenerate) {
      const again = element('button', 'Regenerate');
      again.type = 'button';
      again.setAttribute('aria-controls', body.id);
      again.addEventListener('click', () => { open(true); regenerate.querySelector('input').focus(); });
      actions.append(again);
    }
    if (body.children.length) actions.append(details);
    actions.append(remove);
    line.append(actions);
    open(false);
  } else if (job.status !== 'processing') {
    line.append(remove);  // stopped, waiting for answers or queued; one being prepared right now is kept
  }
  item.append(top, line);
  if (body.children.length) item.append(body);
  return item;
}

// History: search, role and status filters, rows needing input first, then pages of PAGE_SIZE.
// Each job's element (and anything typed into it) is kept until that job itself changes.
const PAGE_SIZE = 6;
const rendered = new Map();
let allJobs = [];
let page = 0;

function matches(job) {
  const query = byId('auto-search').value.trim().toLowerCase();
  const roleFilter = byId('auto-filter-role').value;
  const statusFilter = byId('auto-filter-status').value;
  if (query && !((job.candidate || '') + ' ' + (job.role || '')).toLowerCase().includes(query)) return false;
  if (roleFilter && (job.role || '') !== roleFilter) return false;
  if (statusFilter === 'active') return ACTIVE.has(job.status);
  return !statusFilter || job.status === statusFilter;
}

function roleOptions(jobs) {
  const select = byId('auto-filter-role');
  const roles = [...new Set(jobs.map(job => job.role).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const current = [...select.options].slice(1).map(option => option.value);
  if (roles.join('\n') === current.join('\n')) return;
  const chosen = select.value;
  select.replaceChildren(select.options[0], ...roles.map(name => { const o = element('option', name); o.value = name; return o; }));
  select.value = roles.includes(chosen) ? chosen : '';
}

function renderJobs(jobs) {
  allJobs = jobs;
  const present = new Set(jobs.map(job => job.id));
  for (const id of rendered.keys()) if (!present.has(id)) rendered.delete(id);  // a deleted resume leaves no row
  roleOptions(jobs);
  const ordered = [...jobs.filter(job => job.status === 'needs_input'), ...jobs.filter(job => job.status !== 'needs_input')];
  const shown = ordered.filter(matches);
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  page = Math.min(page, pages - 1);
  const list = byId('auto-job-list');
  const items = shown.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(job => {
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
  byId('auto-empty').hidden = jobs.length > 0;
  byId('auto-delete-all').hidden = jobs.length === 0;
  byId('auto-count').textContent = jobs.length ? jobs.length + (jobs.length === 1 ? ' resume' : ' resumes') : '';
  // Which rows this page holds, e.g. "Page 1 of 3 · 1–6 of 15" ("15 shown" read as if all 15 were on screen).
  const first = page * PAGE_SIZE + 1;
  const last = Math.min(shown.length, (page + 1) * PAGE_SIZE);
  byId('auto-page').textContent = !jobs.length ? '' : shown.length ?
    'Page ' + (page + 1) + ' of ' + pages + ' · ' + first + '–' + last + ' of ' + shown.length
    : 'No resumes match the search or filters';
  byId('auto-prev').disabled = page === 0;
  byId('auto-next').disabled = page >= pages - 1;
}

function turnPage(step) {
  page += step;
  renderJobs(allJobs);
  byId('auto-history').scrollTop = 0;
}

async function refresh() {
  const response = await ResumeAuth.call('/api/automation');
  if (!response.ok) throw new Error('Could not refresh your resumes.');
  const {jobs} = await response.json();
  showCurrent(jobs);
  renderJobs(jobs);
}

async function init() {
  byId('auto-delete-all').addEventListener('click', openDeleteAll);
  byId('auto-delete-phrase').addEventListener('input', event => {
    byId('auto-delete-confirm').disabled = event.target.value !== 'DELETE ALL';
  });
  byId('auto-delete-cancel').addEventListener('click', () => byId('auto-delete-dialog').close());
  byId('auto-delete-form').addEventListener('submit', confirmDeleteAll);
  byId('auto-basics').append(...required.map(field => input(field, true)));
  byId('auto-optional').append(input(skillsField));
  byId('auto-optional').append(...optional.map(field => input(field)));
  collections.forEach(addSection);
  byId('auto-search').addEventListener('input', () => { page = 0; renderJobs(allJobs); });
  for (const id of ['auto-filter-role', 'auto-filter-status']) {
    byId(id).addEventListener('change', () => { page = 0; renderJobs(allJobs); });
  }
  byId('auto-prev').addEventListener('click', () => turnPage(-1));
  byId('auto-next').addEventListener('click', () => turnPage(1));
  form.addEventListener('change', event => { if (event.target.name === 'method') mode(); });
  role.addEventListener('input', roleChanged);
  role.addEventListener('change', roleChanged);
  form.addEventListener('invalid', event => {
    const details = event.target.closest('details:not([open])');
    if (details) { details.open = true; event.target.getBoundingClientRect(); }
  },true);
  mode();
  await ResumeAuth.ready();
  await loadRoles();
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
      fieldError(error.control || controlFor(error.message), error.message);
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

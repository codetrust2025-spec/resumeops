/* The public application form (/apply). The form is saved on this device as it is typed, and a submission is
   kept until the server confirms it: a closed tab, a lost connection or a busy server never loses an application.
   The private key is made here, before sending, so a retried submission is the same application. */
'use strict';
const $ = id => document.getElementById(id);
const form = $('ap-form');
const DRAFT = 'resumeops-apply-draft';
const PENDING = 'resumeops-apply-pending';
const APPS = 'resumeops-applications';
const SESSION = 'resumeops-apply-session';
const store = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
  drop(key) { try { localStorage.removeItem(key); } catch { /* private mode */ } },
};
function randomKey(bytes = 32) {
  const data = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...data)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
const session = store.get(SESSION) || randomKey(24);
store.set(SESSION, session);

const PERSONAL = [['full_name', 'Full name', 'text', true, 'name'], ['email', 'Email', 'email', true, 'email'],
  ['phone', 'Phone', 'tel', true, 'tel'], ['location', 'City', 'text', true, 'address-level2'],
  ['linkedin', 'LinkedIn URL', 'url'], ['github', 'GitHub URL', 'url'], ['portfolio', 'Portfolio URL', 'url']];
const LISTS = {
  education: [['degree', 'Degree', 'text', true, 'e.g. B.Tech'], ['university', 'Institution', 'text', true],
    ['graduation_year', 'Year of completion', 'text', true, 'e.g. 2022'], ['specialization', 'Specialization'],
    ['cgpa', 'CGPA / percentage']],
  work_experience: [['company', 'Employer', 'text', true], ['position', 'Job title', 'text', true],
    ['start', 'Start date', 'text', true, 'e.g. Jul 2022'], ['end', 'End date', 'text', true, 'e.g. Present'],
    ['location', 'Location'], ['responsibilities', 'What you did (one point per line)', 'textarea', true]],
  projects: [['name', 'Project name', 'text', true], ['stack', 'Technologies used (comma separated)', 'text', true],
    ['description', 'What you built (one point per line)', 'textarea', true]],
};
const NAMES = {education: 'Education', work_experience: 'Job', projects: 'Project'};

function field([name, label, type = 'text', required = false, hint = ''], path = name) {
  const wrap = document.createElement('label');
  wrap.className = 'ap-field' + (type === 'textarea' ? ' ap-wide' : '');
  const text = document.createElement('span');
  text.textContent = label;
  if (required) {
    const star = document.createElement('span');
    star.className = 'ap-req';
    star.textContent = ' *';
    text.append(star);
  }
  const control = document.createElement(type === 'textarea' ? 'textarea' : 'input');
  if (type === 'textarea') control.rows = 4; else control.type = type;
  control.name = name;
  control.dataset.field = path;
  control.required = required;
  control.maxLength = type === 'textarea' ? 3000 : 300;
  if (hint && type !== 'textarea' && !['name', 'email', 'tel', 'address-level2'].includes(hint)) control.placeholder = hint;
  if (['name', 'email', 'tel', 'address-level2'].includes(hint)) control.autocomplete = hint;
  const error = document.createElement('small');
  error.className = 'ap-field-error';
  wrap.append(text, control, error);
  return wrap;
}

function addRow(list, values = {}) {
  const rows = form.querySelector(`[data-list="${list}"]`);
  if (rows.children.length >= 20) return null;
  const row = document.createElement('fieldset');
  row.className = 'ap-row';
  const legend = document.createElement('legend');
  row.append(legend);
  const grid = document.createElement('div');
  grid.className = 'ap-grid';
  for (const spec of LISTS[list]) grid.append(field(spec, `${list}.?.${spec[0]}`));
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'ap-remove';
  remove.textContent = 'Remove';
  remove.addEventListener('click', () => { row.remove(); renumber(list); saveDraft(); });
  row.append(grid, remove);
  rows.append(row);
  for (const [key, value] of Object.entries(values)) {
    const control = row.querySelector(`[name="${key}"]`);
    if (control && value) control.value = value;
  }
  renumber(list);
  return row;
}

function renumber(list) {
  [...form.querySelector(`[data-list="${list}"]`).children].forEach((row, index) => {
    row.querySelector('legend').textContent = `${NAMES[list]} ${index + 1}`;
    for (const control of row.querySelectorAll('[data-field]')) {
      control.dataset.field = `${list}.${index}.${control.name}`;
    }
  });
}

function rowsOf(list) {
  return [...form.querySelector(`[data-list="${list}"]`).children].map(row =>
    Object.fromEntries([...row.querySelectorAll('[data-field]')].map(control => [control.name, control.value])))
    .filter(row => Object.values(row).some(value => value.trim()));
}

function value(name) { return form.querySelector(`[data-field="${name}"]`)?.value || ''; }

function collect() {
  const data = {consent: $('ap-consent').checked, fresher: $('ap-fresher').checked, website: form.elements.website.value};
  for (const [name] of PERSONAL) data[name] = value(name);
  for (const name of ['role', 'years_of_experience', 'skills', 'certifications', 'achievements']) data[name] = value(name);
  for (const list of Object.keys(LISTS)) data[list] = rowsOf(list);
  if (data.fresher) data.work_experience = [];
  return data;
}

function fill(data, onlyEmpty = false) {
  if (!data) return;
  for (const [name, val] of Object.entries(data)) {
    if (LISTS[name] || typeof val !== 'string') continue;
    const control = form.querySelector(`[data-field="${name}"]`);
    if (control && val && (!onlyEmpty || !control.value.trim())) control.value = val;
  }
  if (typeof data.fresher === 'boolean' && !onlyEmpty) $('ap-fresher').checked = data.fresher;
  for (const list of Object.keys(LISTS)) {
    const incoming = Array.isArray(data[list]) ? data[list] : [];
    if (!incoming.length) continue;
    const have = rowsOf(list);
    if (!onlyEmpty) form.querySelector(`[data-list="${list}"]`).replaceChildren();
    const first = LISTS[list][0][0];
    const second = LISTS[list][1][0];
    for (const row of incoming) {
      const same = onlyEmpty && have.some(item => (item[first] || '').trim().toLowerCase() === (row[first] || '').trim().toLowerCase()
        && (item[second] || '').trim().toLowerCase() === (row[second] || '').trim().toLowerCase());
      if (!same) addRow(list, row);
    }
    // A blank first row left from the start is replaced by the document's rows.
    const rows = [...form.querySelector(`[data-list="${list}"]`).children];
    if (onlyEmpty && rows.length > 1) {
      for (const row of rows) {
        if ([...row.querySelectorAll('[data-field]')].every(control => !control.value.trim())) row.remove();
      }
      renumber(list);
    }
  }
  fresherChanged();
}

let saving = 0;
let applied = false;  // after a successful submission the draft is gone for good
function saveDraft() {
  clearTimeout(saving);
  saving = setTimeout(() => { if (!applied) store.set(DRAFT, collect()); }, 300);
}
// Leaving or reloading the page (or a phone switching apps) saves at once, not after the typing pause.
function flushDraft() {
  if (!saving || applied || form.hidden) return;
  clearTimeout(saving);
  saving = 0;
  store.set(DRAFT, collect());
}
addEventListener('pagehide', flushDraft);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushDraft(); });

function fresherChanged() {
  const fresher = $('ap-fresher').checked;
  $('ap-work').hidden = fresher;
  form.querySelector('[data-add="work_experience"]').hidden = fresher;
  for (const control of $('ap-work').querySelectorAll('[data-field]')) control.disabled = fresher;
}

function clearErrors() {
  for (const node of form.querySelectorAll('.ap-field-error')) node.textContent = '';
  for (const node of form.querySelectorAll('[aria-invalid]')) node.removeAttribute('aria-invalid');
  $('ap-error').textContent = '';
}

function showError(message, path) {
  let control = path ? form.querySelector(`[data-field="${path}"]`) : null;
  if (!control && path && LISTS[path]) {
    control = form.querySelector(`[data-list="${path}"] [data-field]`) || form.querySelector(`[data-add="${path}"]`);
  }
  if (path === 'consent') control = $('ap-consent');
  $('ap-error').textContent = message;
  if (control) {
    const slot = control.closest('label')?.querySelector('.ap-field-error');
    if (slot) slot.textContent = message;
    control.setAttribute('aria-invalid', 'true');
    control.scrollIntoView({block: 'center', behavior: 'smooth'});
    control.focus({preventScroll: true});
  }
}

// Mandatory facts are checked here too, so a candidate sees every gap at once on a phone (the server decides).
function check(data) {
  const need = [['full_name', 'Enter your full name.'], ['email', 'Enter your email address.'],
    ['phone', 'Enter your phone number.'], ['location', 'Enter your city.'], ['role', 'Enter the target role.'],
    ['skills', 'List your technical skills, separated by commas.']];
  for (const [name, message] of need) if (!data[name].trim()) return [message, name];
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(data.email.trim())) return ['Enter a valid email address.', 'email'];
  const digits = data.phone.replace(/\D/g, '').length;
  if (digits < 7 || digits > 15) return ['Enter a valid phone number (7 to 15 digits).', 'phone'];
  const lists = [['education', 'Add your education: degree, institution and year.'],
    ['work_experience', 'Add your employment (or tick "I am a fresher").'], ['projects', 'Add at least one project you built.']];
  for (const [list, message] of lists) {
    if (list === 'work_experience' && data.fresher) continue;
    if (!data[list].length) return [message, list];
    for (const [index, row] of data[list].entries()) {
      for (const [name, label, , required] of LISTS[list]) {
        if (required && !(row[name] || '').trim()) return [`${NAMES[list]} ${index + 1}: enter ${label.toLowerCase()}.`, `${list}.${index}.${name}`];
      }
    }
  }
  if (!data.consent) return ['Confirm the details and agree to the processing.', 'consent'];
  return null;
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let sending = false;

async function send(pending) {
  if (sending) return;
  sending = true;
  $('ap-submit').disabled = true;
  try {
    for (let attempt = 1; ; attempt++) {
      $('ap-sending').textContent = attempt === 1 ? 'Sending your application…' : `Busy right now: sending again (attempt ${attempt})…`;
      let response;
      try {
        response = await fetch('/apply/api/submit', {method: 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(60000),
          body: JSON.stringify({form: pending.form, key: pending.key, candidate: pending.candidate || undefined,
            prefill_session: session})});
      } catch {
        await pause(Math.min(30000, 1500 * attempt) + Math.random() * 1500);
        continue;
      }
      let result = {};
      try { result = await response.json(); } catch { /* an HTML error page from the network */ }
      if (response.ok) { done(pending, result); return; }
      if (result.retry || [408, 429, 500, 502, 503, 504, 520, 522, 524].includes(response.status)) {
        await pause(Math.min(30000, 1500 * attempt) + Math.random() * 1500);
        continue;
      }
      store.drop(PENDING);
      $('ap-sending').textContent = '';
      showError(result.error || 'The application could not be saved.', result.field);
      return;
    }
  } finally {
    sending = false;
    $('ap-submit').disabled = false;
  }
}

function link(candidate, key) {
  return `${location.origin}/apply/status#c=${encodeURIComponent(candidate)}&k=${encodeURIComponent(key)}`;
}

function done(pending, result) {
  const apps = (store.get(APPS) || []).filter(item => item.c !== result.candidate);
  apps.unshift({c: result.candidate, k: pending.key, at: Date.now(), name: pending.form.full_name, role: pending.form.role});
  store.set(APPS, apps.slice(0, 10));
  applied = true;
  clearTimeout(saving);
  store.drop(PENDING);
  store.drop(DRAFT);
  $('ap-sending').textContent = '';
  form.hidden = true;
  $('ap-upload').hidden = true;
  const url = link(result.candidate, pending.key);
  $('ap-done-id').textContent = `Application ID: ${result.candidate}` + (pending.candidate ? ' (corrected details saved)' : '');
  $('ap-done-link').value = url;
  $('ap-open').href = url;
  $('ap-done').hidden = false;
  $('ap-done').scrollIntoView({block: 'start'});
  showMine();
}

function showMine() {
  const apps = store.get(APPS) || [];
  $('ap-mine').hidden = !apps.length;
  $('ap-mine-list').replaceChildren(...apps.map(item => {
    const entry = document.createElement('li');
    const anchor = document.createElement('a');
    anchor.href = link(item.c, item.k);
    anchor.textContent = [item.c, item.name, item.role].filter(Boolean).join(' · ');
    // On a shared computer, the next applicant must not find this link.
    const forget = document.createElement('button');
    forget.type = 'button';
    forget.className = 'ap-forget';
    forget.textContent = 'Remove from this device';
    forget.addEventListener('click', () => {
      store.set(APPS, (store.get(APPS) || []).filter(other => other.c !== item.c));
      showMine();
    });
    entry.append(anchor, forget);
    return entry;
  }));
}

// Upload: the quick reading fills the form at once; the AI reading fills anything still empty when it is ready.
async function prefill(file) {
  const status = $('ap-prefill');
  const extension = (file.name.match(/\.(pdf|docx)$/i) || [])[1]?.toLowerCase();
  if (!extension || !file.size || file.size > 6 * 1024 * 1024) {
    status.textContent = 'Choose a PDF or DOCX file of up to 6 MB.';
    return;
  }
  status.textContent = 'Reading your resume…';
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  let result;
  for (let attempt = 1; ; attempt++) {
    try {
      const response = await fetch('/apply/api/prefill', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(60000),
        body: JSON.stringify({extension, document: btoa(binary), session})});
      result = await response.json();
      if ((response.status === 503 || response.status === 429) && attempt < 6) { await pause(2000 * attempt); continue; }
      if (!response.ok) { status.textContent = result.error || 'Could not read this file. Fill in the form instead.'; return; }
      break;
    } catch {
      if (attempt >= 4) { status.textContent = 'Could not upload the file. Fill in the form instead.'; return; }
      await pause(1500 * attempt);
    }
  }
  fill(result.candidate, true);
  saveDraft();
  if (!result.ai) { status.textContent = 'Filled what the file states. Check every field and complete the rest.'; return; }
  status.textContent = 'Filled what the file states. AI is reading it for more details; you can keep typing.';
  const started = Date.now();
  while (Date.now() - started < 15 * 60 * 1000) {
    await pause(3000);
    let data;
    try {
      const response = await fetch('/apply/api/prefill?' + new URLSearchParams({session, id: result.ai.id}),
        {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) break;
      data = await response.json();
    } catch { continue; }
    if (data.status === 'complete') {
      fill(data.candidate, true);
      saveDraft();
      status.textContent = 'AI reading finished: empty fields were filled. Check every field before you submit.';
      return;
    }
    if (data.status === 'error') break;
    status.textContent = 'Filled what the file states. AI is reading it for more details (' + (data.stage || 'queued') + '); you can keep typing.';
  }
  status.textContent = 'Filled what the file states. Check every field and complete the rest.';
}

async function loadRoles() {
  try {
    const response = await fetch('/apply/api/catalog?visible=1', {credentials: 'same-origin'});
    const data = await response.json();
    $('ap-roles').replaceChildren(...data.roles.map(role => Object.assign(document.createElement('option'), {value: role.name})));
  } catch { /* free text still works */ }
}

async function editMode() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (!params.has('edit') || !params.get('c') || !params.get('k')) return null;
  const candidate = params.get('c');
  const key = params.get('k');
  $('ap-title').textContent = 'Correct your details';
  $('ap-submit').textContent = 'Save corrections';
  try {
    const response = await fetch('/apply/api/form', {credentials: 'same-origin', cache: 'no-store',
      headers: {'X-Candidate-Id': candidate, 'X-Candidate-Key': key}});
    if (!response.ok) throw new Error();
    const data = await response.json();
    fill(data.form || {});
    $('ap-consent').checked = false;
  } catch {
    $('ap-error').textContent = 'This private link is not valid. Open it again from your application.';
  }
  return {candidate, key};
}

async function init() {
  for (const spec of PERSONAL) $('ap-personal').append(field(spec));
  $('ap-role-fields').append(field(['role', 'Target role', 'text', true, 'e.g. Java Backend Developer']));
  $('ap-role-fields').querySelector('input').setAttribute('list', 'ap-roles');
  $('ap-years').append(field(['years_of_experience', 'Total experience in years (optional)', 'number']));
  $('ap-skills').append(field(['skills', 'Skills (comma separated)', 'textarea', true]));
  $('ap-more').append(field(['certifications', 'Certifications you have completed (one per line)', 'textarea']),
    field(['achievements', 'Achievements (one per line)', 'textarea']));
  for (const list of Object.keys(LISTS)) addRow(list);
  for (const button of form.querySelectorAll('[data-add]')) {
    button.addEventListener('click', () => { addRow(button.dataset.add)?.querySelector('input, textarea').focus(); saveDraft(); });
  }
  $('ap-fresher').addEventListener('change', () => { fresherChanged(); saveDraft(); });
  form.addEventListener('input', event => {
    const slot = event.target.closest('label')?.querySelector('.ap-field-error');
    if (slot) slot.textContent = '';
    event.target.removeAttribute('aria-invalid');
    saveDraft();
  });
  $('ap-file').addEventListener('change', () => { if ($('ap-file').files[0]) prefill($('ap-file').files[0]); });
  $('ap-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('ap-done-link').value); $('ap-copy').textContent = 'Copied'; }
    catch { $('ap-done-link').select(); }
  });
  loadRoles();
  showMine();
  const editing = await editMode();
  if (!editing) fill(store.get(DRAFT));
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (sending) return;
    clearErrors();
    const data = collect();
    const problem = check(data);
    if (problem) { showError(...problem); return; }
    const previous = store.get(PENDING);
    const pending = {form: data, key: editing ? editing.key : (previous?.key || randomKey()),
      candidate: editing ? editing.candidate : undefined};
    store.set(PENDING, pending);  // kept until the server confirms it
    send(pending);
  });
  const waiting = store.get(PENDING);
  if (waiting && !editing) send(waiting);  // an application the previous visit could not finish sending
}

init();

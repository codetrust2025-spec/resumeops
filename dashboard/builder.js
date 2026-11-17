'use strict';
/* ResumeOps builder: a 9-step flow (Start, Input, Role, Personal, Experience, Review, Generate,
   Processing, Results) over the existing dashboard APIs. Progress always comes from the watcher's
   /api/status record; nothing here invents percentages. */
const el = id => document.getElementById(id);
const form = el('candidate');
const fields = form.elements;
const steps = [...form.querySelectorAll('[data-step]')];
const token = document.querySelector('meta[name=dashboard-token]').content;
const STEP = {START:0, INPUT:1, ROLE:2, PERSONAL:3, EXPERIENCE:4, REVIEW:5, GENERATE:6, PROCESSING:7, RESULTS:8};
const STEP_NAMES = ['Start','Input','Role','Personal details','Experience & education','Review',
  'Generate','Processing','Results'];
const rows = (tag, value, className) => {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
};
const lines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
const loc = epoch => new Date(epoch * 1000).toLocaleString();
const params = (path, query) => path + '?' + new URLSearchParams(query);
const artifact = (key, name, download = false) =>
  params('/api/artifact', download ? {key, name, download: '1'} : {key, name});
const mobileLayout = window.matchMedia('(max-width:760px)');

let activeStep = 0;
let furthestStep = 0;          // highest form step reached for the current candidate
let roles = [];
let templates = {};
let watchedJob = '';
let lastJob = null;
let queuedAt = 0;               // when this browser queued the watched job (ms)
let seenWatched = false;        // whether /api/status has listed the watched job yet
let worker = {};
let serverOffset = 0;           // server clock minus browser clock, from /api/status
const shownPercent = {};        // highest percentage shown per job attempt (never decreases)
let shownRevision = '';
let resultsReady = false;
let revisions = [];
let historySnapshot = '';
let refreshPending = false;
let selection = 0;
let submitting = false;
let imported = false;           // a resume file was imported for this candidate
let yearsTouched = false;       // the experience level was chosen by hand (never overwrite it)
const ACTIVE_KEY = 'resumeops.activeJob';

function rememberJob(name, submitted) {
  try { localStorage.setItem(ACTIVE_KEY, JSON.stringify({name, submitted, queued: Date.now()})); } catch {}
}
function storedJob() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_KEY) || 'null'); } catch { return null; }
}
function forgetJob() {
  try { localStorage.removeItem(ACTIVE_KEY); } catch {}
}

async function request(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Dashboard-Token':token},
    body:JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed.');
  return result;
}

function feedback(message, failed = false) {
  el('builder-status').textContent = message;
  el('builder-status').classList.toggle('error', failed);
}

/* ---------- Experience level: chosen by hand or calculated from dated roles ---------- */
const LEVEL = years => years < 2 ? 'Fresher / Junior' : years < 5 ? 'Mid-level' : years < 10 ? 'Senior' : 'Lead / Principal';
fields.years.replaceChildren(...Array.from({length:26}, (_, years) =>
  new Option((years === 25 ? '25+ years' : years === 1 ? '1 year' : years + ' years') + ' · ' + LEVEL(years), String(years))));
fields.years.value = '0';
let derivedNote = '';

function experienceLevel() {
  const years = Number(fields.years.value);
  return (years === 25 ? '25+' : years) + ' years · ' + LEVEL(years);
}
function updateExperience() {
  el('experience-level').textContent = experienceLevel() + (derivedNote ? ' · ' + derivedNote : '');
}
fields.years.addEventListener('change', () => { yearsTouched = true; derivedNote = ''; updateExperience(); });

const MONTHS = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
function parseDate(text, isEnd) {
  const value = String(text || '').trim().toLowerCase();
  if (!value) return null;
  if (/present|current|now|till date|to date|ongoing/.test(value)) return new Date();
  let match = value.match(/^(\d{1,2})\s*[/\-.]\s*(\d{4})$/);
  if (match && +match[1] >= 1 && +match[1] <= 12) return new Date(+match[2], +match[1] - 1, 1);
  match = value.match(/^(\d{4})\s*[/\-.]\s*(\d{1,2})/);
  if (match && +match[2] >= 1 && +match[2] <= 12) return new Date(+match[1], +match[2] - 1, 1);
  match = value.match(/([a-z]{3})[a-z]*\.?\s*,?\s*'?(\d{4})/);
  if (match && match[1] in MONTHS) return new Date(+match[2], MONTHS[match[1]], 1);
  match = value.match(/\b(19|20)\d{2}\b/);
  if (match) return new Date(+match[0], isEnd ? 11 : 0, 1);
  return null;
}
/** Whole months covered by dated experience entries, overlaps merged. Null when nothing is dated. */
function experienceMonths() {
  const spans = [];
  for (const item of entryData('experience')) {
    const start = parseDate(item.start, false);
    if (!start) continue;
    // Same rule as the server: a role counts only with an end date or "Present".
    const end = item.end ? parseDate(item.end, true) : null;
    if (!end || end < start) continue;
    spans.push([start.getFullYear() * 12 + start.getMonth(), end.getFullYear() * 12 + end.getMonth() + 1]);
  }
  if (!spans.length) return null;
  spans.sort((a, b) => a[0] - b[0]);
  let total = 0, [from, to] = spans[0];
  for (const [start, end] of spans.slice(1)) {
    if (start <= to) to = Math.max(to, end);
    else { total += to - from; [from, to] = [start, end]; }
  }
  return total + (to - from);
}
function deriveYears() {
  if (yearsTouched) return;
  const months = experienceMonths();
  if (months === null) { derivedNote = ''; updateExperience(); return; }
  fields.years.value = String(Math.min(25, Math.floor(months / 12)));
  derivedNote = 'calculated from dated roles (' + months + (months === 1 ? ' month' : ' months') + ')';
  updateExperience();
}

/* ---------- Role and template ---------- */
function showTemplate() {
  const compact = fields.template.value === 'compact';
  el('template-name').textContent = templates[fields.template.value] || 'Standard';
  el('template-note').textContent = compact ? 'Tighter spacing for a longer work history.' :
    'A readable single-column layout with room for experience.';
}
fields.template.addEventListener('change', showTemplate);

async function roleChanged() {
  const role = fields.role.value;
  el('catalog-keywords').textContent = '';
  fields.keywords.value = '';
  if (!role) return;
  try {
    const result = await request(params('/api/keywords',{role}));
    if (fields.role.value !== role) return;
    el('catalog-keywords').textContent = result.catalog.join(' · ');
    fields.keywords.value = result.additional.join('\n');
  } catch (error) {
    feedback(error.message, true);
  }
}
fields.role.addEventListener('change', roleChanged);

async function keywordAction(action) {
  const role = fields.role.value;
  if (!role) return feedback('Choose a target role first.', true);
  try {
    if (action === 'extract') {
      const result = await request('/api/keywords/extract',{role, description:el('description').value});
      const unique = new Set([...lines(fields.keywords.value), ...result.keywords]);
      fields.keywords.value = [...unique].join('\n');
      feedback(result.keywords.length + ' role keywords found.');
    } else {
      await request('/api/keywords/update',{role,keywords:lines(fields.keywords.value)});
      feedback('Role keyword defaults saved.');
    }
  } catch (error) {
    feedback(error.message,true);
  }
}
el('extract-keywords').addEventListener('click', () => keywordAction('extract'));
el('save-keywords').addEventListener('click', () => keywordAction('save'));

/* ---------- Tabs (experience sections and result views) ---------- */
function tabList(list, onSelect) {
  const tabs = [...list.querySelectorAll('[role=tab]')];
  const select = (id, focus = false) => {
    for (const tab of tabs) {
      const selected = tab.id === id;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      el(tab.getAttribute('aria-controls')).hidden = !selected;
      if (selected && focus) tab.focus();
    }
    if (onSelect) onSelect(id);
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab.id));
    tab.addEventListener('keydown', event => {
      const options = {ArrowRight:(index+1)%tabs.length, ArrowLeft:(index+tabs.length-1)%tabs.length, Home:0, End:tabs.length-1};
      if (!(event.key in options)) return;
      event.preventDefault();
      select(tabs[options[event.key]].id, true);
    });
  });
  return select;
}
const openSection = tabList(document.querySelector('.seg'));
const openTab = tabList(document.querySelector('.tabs'));

/* ---------- Experience and education cards ---------- */
const entryFields = {
  experience: [['company','Employer'],['position','Position'],['start','Start date'],
    ['end','End date'],['location','Location'],['bullets','Verified work contributions']],
  education: [['institution','Institution'],['degree','Degree'],['start','Education start'],
    ['end','Education end'],['details','Education details']],
};
const REQUIRED = {experience:['company','position','start'], education:['institution','degree']};

function entryState(entry) {
  const kind = entry.dataset.kind;
  const value = key => entry.querySelector('[data-field=' + key + ']')?.value.trim() || '';
  const missing = REQUIRED[kind].filter(key => !value(key))
    .map(key => entryFields[kind].find(([name]) => name === key)[1]);
  const [nameKey, titleKey] = kind === 'experience' ? ['position','company'] : ['degree','institution'];
  const title = [value(nameKey), value(titleKey)].filter(Boolean).join(' · ') ||
    (kind === 'experience' ? 'New experience' : 'New education');
  const dates = [value('start'), value('end') || (kind === 'experience' && value('start') ? 'Present' : '')]
    .filter(Boolean).join(' – ');
  entry.querySelector('.entry-title b').textContent = title;
  entry.querySelector('.entry-title small').textContent = missing.length ? 'Missing: ' + missing.join(', ') :
    [dates, value('location')].filter(Boolean).join(' · ') || 'Complete';
  entry.toggleAttribute('data-missing', missing.length > 0);
  return missing;
}
function updateCounts() {
  for (const kind of ['experience','education']) {
    const entries = [...el(kind + '-entries').children];
    el('count-' + kind).textContent = '(' + entries.length + ')';
    el('tab-' + kind).toggleAttribute('data-missing', entries.some(entry => entry.hasAttribute('data-missing')));
  }
}

function addEntry(kind, focus = true) {
  const holder = el(kind + '-entries');
  const entry = document.createElement('details');
  entry.className = 'entry';
  entry.dataset.kind = kind;
  const summary = document.createElement('summary');
  const title = rows('span', '', 'entry-title');
  title.append(rows('b', ''), rows('small', ''));
  summary.append(title);
  const fieldset = document.createElement('fieldset');
  fieldset.append(rows('legend', kind === 'experience' ? 'Work entry' : 'Education entry'));
  const grid = rows('div', '', 'fields');
  for (const [key, label] of entryFields[kind]) {
    const wrapper = rows('label', label);
    const input = document.createElement(key === 'bullets' || key === 'details' ? 'textarea' : 'input');
    input.dataset.field = key;
    input.maxLength = key === 'bullets' ? 8000 : 250;
    input.required = REQUIRED[kind].includes(key);
    if (key === 'company' && !holder.children.length) input.value = fields.current_company.value;
    if (key === 'bullets') input.placeholder = 'One contribution per line';
    if (key === 'start' || key === 'end') input.placeholder = 'e.g. Jan 2020' + (key === 'end' ? ' or Present' : '');
    if (key === 'end' && kind === 'experience') input.title = 'Use a date or Present so this role counts toward years of experience';
    wrapper.append(input);
    grid.append(wrapper);
  }
  const remove = rows('button', 'Remove entry', 'secondary small');
  remove.type = 'button';
  remove.addEventListener('click', () => { entry.remove(); updateCounts(); deriveYears(); });
  fieldset.append(grid, remove);
  entry.append(summary, fieldset);
  grid.addEventListener('input', () => {
    entryState(entry);
    updateCounts();
    if (kind === 'experience') deriveYears();
  });
  holder.append(entry);
  entry.open = focus;
  entryState(entry);
  updateCounts();
  if (focus) {
    openSection('tab-' + kind);
    grid.querySelector('input').focus();
  }
  return entry;
}
el('add-experience').addEventListener('click', () => addEntry('experience'));
el('add-education').addEventListener('click', () => addEntry('education'));

/** Open every card that still lacks a required field so nothing blocks Continue invisibly. */
function expandIncomplete() {
  let first = null;
  for (const entry of form.querySelectorAll('.entry')) {
    if (entryState(entry).length) {
      entry.open = true;
      first = first || entry;
    }
  }
  updateCounts();
  return first;
}

function entryData(kind) {
  return [...el(kind + '-entries').querySelectorAll('fieldset')].map(entry =>
    Object.fromEntries([...entry.querySelectorAll('[data-field]')].map(input =>
      [input.dataset.field, input.dataset.field === 'bullets' ? lines(input.value) : input.value.trim()])));
}

function payload() {
  const profile = Object.fromEntries(['full_name','email','headline','phone','location','linkedin',
    'github','portfolio','current_company','summary'].map(key => [key,fields[key].value.trim()]));
  Object.assign(profile,{skills:lines(fields.skills.value),certifications:lines(fields.certifications.value),
    projects:lines(fields.projects.value),experience:entryData('experience'),education:entryData('education')});
  return {profile,target:{role:fields.role.value,template:fields.template.value,
    keywords:lines(fields.keywords.value),experience_years:Number(fields.years.value)}};
}

function profileText(profile) {
  return [profile.full_name,profile.headline,
    [profile.email,profile.phone,profile.location,profile.linkedin,profile.github,profile.portfolio].filter(Boolean).join(' · '),
    profile.current_company && !profile.experience.some(item =>
      item.company.toLowerCase()===profile.current_company.toLowerCase()) ?
      'Current company · '+profile.current_company : '',
    profile.summary,profile.skills.join(' · '),
    ...profile.experience.map(item => [item.position+' · '+item.company,
      [item.start,item.end,item.location].filter(Boolean).join(' · '),...item.bullets].filter(Boolean).join('\n')),
    ...(profile.projects || []), ...profile.education.map(item => item.degree+' · '+item.institution),
    ...profile.certifications].filter(Boolean).join('\n\n');
}

/* ---------- Step navigation ---------- */
function reachable(index) {
  if (index === STEP.PROCESSING) return Boolean(watchedJob);
  if (index === STEP.RESULTS) return resultsReady;
  return index <= furthestStep;
}
function renderStepper() {
  [...el('steps').children].forEach((item, index) => {
    if (index === activeStep) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
    // On Processing and Results every earlier step was completed for that job.
    const done = index < activeStep && (activeStep >= STEP.PROCESSING || index <= furthestStep);
    item.dataset.state = done ? 'done' : index === activeStep ? 'current' : 'todo';
    item.toggleAttribute('data-reachable', reachable(index) && index !== activeStep);
    item.querySelector('button').disabled = !reachable(index) && index !== activeStep;
  });
  el('step-meta').textContent = 'Step ' + (activeStep + 1) + ' of 9 · ' + STEP_NAMES[activeStep];
  el('step-bar').style.width = ((activeStep + 1) / 9 * 100) + '%';
  const formStep = activeStep >= STEP.INPUT && activeStep <= STEP.GENERATE;
  el('step-actions').hidden = !formStep;
  el('back').hidden = !formStep;
  el('next').hidden = !formStep || activeStep === STEP.GENERATE;
  el('job-mini').hidden = !watchedJob || activeStep === STEP.PROCESSING;
}
function showStep(index, focus = true) {
  activeStep = index;
  if (index <= STEP.GENERATE) furthestStep = Math.max(furthestStep, index);
  document.body.classList.remove('history-view');
  el('history').hidden = true;
  el('nav-builder').setAttribute('aria-current', 'page');
  el('nav-history').removeAttribute('aria-current');
  steps.forEach((step, i) => { step.hidden = i !== index; });
  clearValidation();
  if (index === STEP.EXPERIENCE) expandIncomplete();
  if (index === STEP.GENERATE) renderGenerateSummary();
  renderStepper();
  if (focus) {
    const heading = steps[index].querySelector('h2');
    heading.focus({preventScroll:true});
    document.querySelector('.stepper').scrollIntoView({block:'start', behavior:'auto'});
  }
}
el('steps').addEventListener('click', event => {
  const item = event.target.closest('li[data-target]');
  if (!item) return;
  const index = Number(item.dataset.target);
  if (index !== activeStep && reachable(index)) { feedback(''); showStep(index); }
});

/* ---------- Validation ---------- */
const validationNotice = rows('p', '', 'validation-notice');
validationNotice.setAttribute('role', 'alert');
validationNotice.hidden = true;
function clearValidation() {
  validationNotice.hidden = true;
  validationNotice.textContent = '';
  form.querySelectorAll('[aria-invalid="true"]').forEach(input => input.removeAttribute('aria-invalid'));
}
function fieldDescription(input) {
  const label = input.closest('label');
  const name = label?.firstChild?.textContent?.trim() || input.name || 'required field';
  const entry = input.closest('.entry');
  if (!entry) return name;
  const kind = entry.dataset.kind === 'experience' ? 'Experience' : 'Education';
  const index = [...entry.parentElement.children].indexOf(entry);
  return kind + ' ' + (index + 1) + ' — ' + name;
}
function showNotice(message) {
  steps[activeStep].querySelector('.step-head').after(validationNotice);
  validationNotice.textContent = message;
  validationNotice.hidden = false;
  feedback('');
}
function revealInvalidField(input) {
  // Open the tab and every collapsed card around the field before focusing it; values are kept.
  const pane = input.closest('[role=tabpanel]');
  if (pane && pane.hidden) openSection(pane.getAttribute('aria-labelledby'));
  for (let node = input.parentElement; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
  }
  expandIncomplete();
  showNotice('Please complete ' + fieldDescription(input) + ' before continuing.');
  input.setAttribute('aria-invalid', 'true');
  input.focus({preventScroll:true});
  input.scrollIntoView({block:'center', behavior:'auto'});
  input.reportValidity();
}
form.addEventListener('input', event => {
  const input = event.target;
  if (input.matches('input,textarea,select') && input.checkValidity()) {
    input.removeAttribute('aria-invalid');
    if (validationNotice.textContent.includes(fieldDescription(input))) validationNotice.hidden = true;
  }
});
function invalidField(index) {
  return [...steps[index].querySelectorAll('input,select,textarea')]
    .find(input => input.name !== 'verified' && !input.checkValidity());
}

/* ---------- Review: original text vs extracted details, missing fields ---------- */
function missingFields(profile) {
  const items = [];
  const need = (ok, label, step, focus, required = false) => { if (!ok) items.push({label, step, focus, required}); };
  need(fields.role.value, 'Target role', STEP.ROLE, () => fields.role, true);
  need(profile.full_name, 'Full name', STEP.PERSONAL, () => fields.full_name, true);
  need(profile.email, 'Email', STEP.PERSONAL, () => fields.email, true);
  need(profile.phone, 'Phone', STEP.PERSONAL, () => fields.phone);
  need(profile.location, 'Location', STEP.PERSONAL, () => fields.location);
  need(profile.summary, 'Professional summary', STEP.EXPERIENCE, () => { openSection('tab-skills'); return fields.summary; });
  need(profile.skills.length, 'Skills', STEP.EXPERIENCE, () => { openSection('tab-skills'); return fields.skills; });
  need(profile.experience.length || Number(fields.years.value) === 0, 'Work experience', STEP.EXPERIENCE,
    () => { openSection('tab-experience'); return el('add-experience'); });
  need(profile.education.length, 'Education', STEP.EXPERIENCE,
    () => { openSection('tab-education'); return el('add-education'); });
  for (const entry of form.querySelectorAll('.entry')) {
    const missing = entryState(entry);
    if (!missing.length) continue;
    const kind = entry.dataset.kind;
    const index = [...entry.parentElement.children].indexOf(entry) + 1;
    items.push({label: (kind === 'experience' ? 'Experience ' : 'Education ') + index + ' ' + missing.join(', ').toLowerCase(),
      step: STEP.EXPERIENCE, required: true, focus: () => {
        openSection('tab-' + kind);
        entry.open = true;
        return entry.querySelector('[required]:invalid') || entry.querySelector('input');
      }});
  }
  return items;
}
function renderMissing(items) {
  const box = el('missing-fields');
  box.hidden = !items.length;
  if (!items.length) return;
  const list = document.createElement('ul');
  for (const item of items) {
    const fix = rows('button', 'Fix ' + item.label + (item.required ? '' : ' (recommended)'));
    fix.type = 'button';
    fix.addEventListener('click', () => {
      showStep(item.step, false);
      const target = item.focus();
      if (target) { target.focus({preventScroll:true}); target.scrollIntoView({block:'center'}); }
    });
    list.append(rows('li', '').appendChild(fix).parentNode);
  }
  box.replaceChildren(rows('p', items.some(item => item.required) ? 'Some required details are missing:' :
    'Optional details you may want to add:'), list);
}
async function prepareReview() {
  const {profile, ats} = await request('/api/preview', payload());
  const role = roles.find(item => item.id === ats.role);
  el('review-summary').replaceChildren(
    rows('p', [role?.name, templates[ats.template], experienceLevel(), fields.current_company.value].filter(Boolean).join(' · '), 'preflight'),
    rows('p', profileText(profile), 'review-line'),
    rows('p', 'Preflight ATS readiness ' + ats.ats_readiness + '/100 · Keyword coverage ' + ats.score + '%', 'preflight'));
  const source = el('import-source').value;
  document.querySelector('.compare').classList.toggle('single', !source);
  renderMissing(missingFields(payload().profile));
  fields.verified.checked = false;
}

function renderGenerateSummary() {
  const data = payload();
  const role = roles.find(item => item.id === data.target.role);
  const pairs = [['Candidate', data.profile.full_name], ['Target role', role?.name || data.target.role],
    ['Template', templates[data.target.template] || data.target.template], ['Experience', experienceLevel()],
    ['Work entries', String(data.profile.experience.length)], ['Education entries', String(data.profile.education.length)]];
  el('generate-summary').replaceChildren(...pairs.flatMap(([key, value]) => [rows('dt', key), rows('dd', value || '—')]));
}

el('back').addEventListener('click', () => { feedback(''); showStep(Math.max(STEP.START, activeStep - 1)); });
el('next').addEventListener('click', async () => {
  clearValidation();
  feedback('');
  if (activeStep === STEP.INPUT) {
    const mode = form.querySelector('[name=source]:checked').value;
    if (mode === 'import' && !imported) {
      showNotice(el('resume-upload').files.length ?
        'Tap Import and prefill to read the selected file, or choose Enter manually.' :
        'Choose a resume file and tap Import and prefill, or choose Enter manually.');
      return;
    }
    showStep(STEP.ROLE);
    return;
  }
  const invalid = invalidField(activeStep);
  if (invalid) { revealInvalidField(invalid); return; }
  if (activeStep === STEP.EXPERIENCE) {
    const button = el('next');
    button.disabled = true;
    try {
      await prepareReview();
      showStep(STEP.REVIEW);
    } catch (error) {
      feedback(error.message, true);
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (activeStep === STEP.REVIEW) {
    const blocking = missingFields(payload().profile).filter(item => item.required);
    if (blocking.length) { showNotice('Fix ' + blocking[0].label + ' before generating.'); return; }
    if (!fields.verified.checked) { showNotice('Confirm the candidate facts before generating.'); return; }
  }
  showStep(activeStep + 1);
});

/* ---------- Start ---------- */
el('start-create').addEventListener('click', () => { feedback(''); showStep(STEP.INPUT); });
el('start-history').addEventListener('click', () => showHistory());
form.addEventListener('change', event => {
  if (event.target.name === 'source') clearValidation();
});

/* ---------- Generate (exactly one job per confirmed candidate) ---------- */
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (activeStep !== STEP.GENERATE || submitting) return;
  if (!fields.verified.checked) { feedback('Confirm the candidate facts before generating.', true); showStep(STEP.REVIEW); return; }
  const submitted = payload();
  const remembered = storedJob();
  if (watchedJob && remembered && remembered.submitted === JSON.stringify(submitted)) {
    feedback('This candidate is already being generated. Showing its progress.');
    showStep(STEP.PROCESSING);
    return;
  }
  submitting = true;
  const button = el('generate');
  button.disabled = true;
  try {
    const result = await request('/api/queue', submitted);
    watchedJob = result.name;
    lastJob = null;
    queuedAt = Date.now();
    seenWatched = false;
    rememberJob(result.name, JSON.stringify(submitted));
    el('result-success').hidden = true;
    ++selection;               // the previous result stays in history; hide it so it is not mistaken for this job
    shownRevision = '';
    resultsReady = false;
    el('pdf-preview').removeAttribute('src');
    resetLive();
    showStep(STEP.PROCESSING);
    if (mobileLayout.matches) el('generation').scrollIntoView({block:'start'});
    feedback(result.created ? 'Resume queued.' : 'This revision already exists.');
    refresh();
  } catch (error) {
    feedback(error.message, true);
  } finally {
    submitting = false;
    button.disabled = false;
  }
});

/* ---------- Results ---------- */
function showAts(ats) {
  const pane = el('ats-result');
  const score = ats ? ats.ats_readiness : null;
  el('ats-ring').style.setProperty('--score', score ?? 0);
  el('ats-ring-value').textContent = score ?? '–';
  if (!ats) { pane.replaceChildren(rows('p', 'No target-role analysis was saved for this revision.')); return; }
  const breakdown = rows('div', '', 'score-grid');
  for (const part of ats.breakdown) breakdown.append(rows('p', part.label + ' ' + part.points + '/' + part.max));
  const suggestions = document.createElement('ul');
  ats.suggestions.forEach(tip => suggestions.append(rows('li', tip)));
  pane.replaceChildren(rows('p', 'ATS readiness ' + ats.ats_readiness + '/100', 'score'),
    rows('p', 'Keyword coverage ' + ats.score + '%'), rows('p', ats.method), breakdown,
    rows('h3', 'Matched keywords'), rows('p', ats.matched.join(' · ') || 'None'),
    rows('h3', 'Missing keywords'), rows('p', ats.missing.join(' · ') || 'None'),
    rows('h3', 'Improvement suggestions'), suggestions.children.length ? suggestions : rows('p', 'No rubric gaps detected.'));
}
function setTileLink(tile, href, note) {
  if (href) { tile.href = href; tile.removeAttribute('aria-disabled'); tile.removeAttribute('tabindex'); }
  else { tile.removeAttribute('href'); tile.setAttribute('aria-disabled', 'true'); tile.tabIndex = -1; }
  if (note !== undefined) el('overleaf-note').textContent = note;
}
for (const tile of document.querySelectorAll('.tile[data-tab]')) {
  tile.addEventListener('click', () => {
    openTab(tile.dataset.tab);
    el(el(tile.dataset.tab).getAttribute('aria-controls')).scrollIntoView({block:'start', behavior:'smooth'});
  });
}
el('tile-history').addEventListener('click', () => showHistory());

function showTailoring(tailoring, experience) {
  const pane = el('ai-result');
  if (tailoring && (tailoring.skipped || tailoring.model_output_rejected)) {
    el('ai-note').textContent = tailoring.skipped ? 'Nothing to tailor' : 'AI reply discarded';
    pane.replaceChildren(rows('p', tailoring.skipped || ('The AI reply was discarded (' + tailoring.model_output_rejected +
      '). The resume uses the candidate\'s verified text unchanged.')));
    return;
  }
  if (!tailoring) {
    el('ai-note').textContent = 'Not tailored';
    pane.replaceChildren(rows('p', 'This revision was generated before AI tailoring was available, or AI tailoring was disabled.'));
    return;
  }
  el('ai-note').textContent = tailoring.changed + ' line' + (tailoring.changed === 1 ? '' : 's') + ' improved';
  const intro = rows('p', 'Ollama rewrote ' + tailoring.changed + ' of ' + (tailoring.accepted + tailoring.rejected) +
    ' lines for the target role. Every accepted line was checked to contain only the candidate\'s own facts; ' +
    tailoring.rejected + ' suggestion' + (tailoring.rejected === 1 ? ' was' : 's were') + ' rejected and kept as written.');
  const list = document.createElement('ol');
  list.className = 'changes';
  for (const change of tailoring.changes || []) {
    const item = document.createElement('li');
    item.append(rows('del', change.before), rows('ins', change.after));
    list.append(item);
  }
  const parts = [intro, list.children.length ? list : rows('p', 'No line needed rewording.')];
  if ((tailoring.rejections || []).length) {
    const rejected = document.createElement('ul');
    for (const item of tailoring.rejections) rejected.append(rows('li', item.reason));
    parts.push(rows('h3', 'Rejected suggestions'), rejected);
  }
  if (experience) {
    parts.push(rows('h3', 'Experience level'), rows('p', experience.used === null ?
      'No dated roles, so no years of experience are stated on the resume.' :
      'Stated as ' + experience.used + ' years' + (experience.claimed !== null && experience.claimed !== experience.used ?
        ' (selected ' + experience.claimed + ', limited to the dated roles)' : '') + '.'));
  }
  pane.replaceChildren(...parts);
}

async function showRevision(revision, navigate = true) {
  const ticket = ++selection;
  shownRevision = revision.key;
  try {
    const detail = await request(params('/api/revision', {key:revision.key}));
    const source = await fetch(artifact(revision.key, 'main.tex'));
    if (!source.ok) throw new Error('LaTeX source is unavailable.');
    const latex = await source.text();
    if (ticket !== selection) return false;
    el('result-name').textContent = detail.profile.full_name + ' · ' + loc(revision.modified);
    el('preview-text').replaceChildren(rows('p', profileText(detail.profile), 'review-line'));
    el('latex-source').textContent = latex;
    el('download-latex').href = artifact(revision.key, 'main.tex', true);
    el('copy-status').textContent = '';
    const links = [];
    for (const name of ['resume.pdf', 'overleaf.pdf']) {
      if (!revision.files.includes(name)) continue;
      const link = rows('a', name === 'resume.pdf' ? 'Download PDF' : 'Download verified Overleaf PDF', 'button secondary');
      link.href = artifact(revision.key, name, true);
      links.push(link);
    }
    if (revision.url) {
      const link = rows('a', 'Open Overleaf project', 'button secondary');
      link.href = revision.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      links.push(link);
    }
    el('pdf-actions').replaceChildren(...links);
    setTileLink(el('tile-pdf'), revision.files.includes('resume.pdf') ? artifact(revision.key, 'resume.pdf', true) : '');
    setTileLink(el('tile-overleaf'), revision.url || '', revision.url ? 'Open in Overleaf' : 'Not published to Overleaf');
    const pdf = el('pdf-preview');
    pdf.hidden = !revision.files.includes('resume.pdf');
    if (!pdf.hidden) pdf.src = artifact(revision.key, 'resume.pdf');
    else pdf.removeAttribute('src');
    el('preview-text-box').open = pdf.hidden;   // the text view is a fallback when no PDF exists
    showAts(detail.ats);
    showTailoring(detail.tailoring, detail.experience);
    openTab('tab-preview');
    resultsReady = true;
    if (navigate) showStep(STEP.RESULTS, false);
    else renderStepper();
    return true;
  } catch (error) {
    if (ticket === selection) { shownRevision = ''; feedback(error.message, true); }
    return false;
  }
}
el('copy-latex').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(el('latex-source').textContent);
    el('copy-status').textContent = 'LaTeX copied.';
  } catch {
    el('copy-status').textContent = 'Clipboard unavailable. Use Download LaTeX.';
  }
});

/* ---------- Reset ---------- */
function clearForm() {
  el('description').value = '';
  el('experience-entries').replaceChildren();
  el('education-entries').replaceChildren();
  form.reset();
  const upload = el('resume-upload');
  upload.value = '';
  el('import-resume').disabled = true;
  el('resume-import-progress').hidden = true;
  el('resume-import-progress').value = 0;
  el('resume-import-status').textContent = '';
  el('resume-import-status').className = '';
  el('import-source').value = '';
  el('review-summary').replaceChildren();
  el('missing-fields').hidden = true;
  fields.role.value = '';
  fields.years.value = '0';
  imported = false;
  yearsTouched = false;
  derivedNote = '';
  furthestStep = 0;
  openSection('tab-experience');
  updateCounts();
  roleChanged();
  updateExperience();
  showTemplate();
  applyLayout();
}
function stopWatching() {
  watchedJob = '';
  lastJob = null;
  forgetJob();
}
function activeJob() {
  return Boolean(watchedJob) && !(lastJob && lastJob.status === 'ok' && lastJob.percent === 100);
}
el('another').addEventListener('click', () => {
  clearForm();
  feedback('');
  showStep(STEP.START);
});
el('job-next').addEventListener('click', () => {
  clearForm();
  feedback('New candidate started. The previous job keeps running and its result will appear when verified.');
  showStep(STEP.INPUT);
});
el('job-mini-open').addEventListener('click', () => {
  if (watchedJob) showStep(STEP.PROCESSING);
  else if (resultsReady) showStep(STEP.RESULTS);
});

/* ---------- History ---------- */
function showHistory() {
  document.body.classList.add('history-view');
  el('history').hidden = false;
  el('nav-history').setAttribute('aria-current', 'page');
  el('nav-builder').removeAttribute('aria-current');
  el('history-filter').focus({preventScroll:true});
  window.scrollTo({top:0});
}
function hideHistory() {
  document.body.classList.remove('history-view');
  el('history').hidden = true;
  el('nav-builder').setAttribute('aria-current', 'page');
  el('nav-history').removeAttribute('aria-current');
}
el('nav-history').addEventListener('click', event => { event.preventDefault(); showHistory(); });
el('nav-builder').addEventListener('click', event => { event.preventDefault(); hideHistory(); });
el('history-close').addEventListener('click', hideHistory);

function revisionRow(item, heading = 'h3') {
  const row = document.createElement('article');
  const info = document.createElement('div');
  const ats = item.ats && typeof item.ats.ats_readiness === 'number' ? ' · ATS ' + item.ats.ats_readiness + '/100' : '';
  info.append(rows(heading, item.candidate), rows('small', loc(item.modified) + ats));
  const open = rows('button', 'View resume', 'secondary small');
  open.type = 'button';
  open.addEventListener('click', async () => {
    hideHistory();
    el('result-success').hidden = true;
    await showRevision(item, true);
  });
  row.append(info, open);
  return row;
}
function renderHistory() {
  const filter = el('history-filter').value.toLowerCase().trim();
  const items = revisions.filter(item => item.candidate.toLowerCase().includes(filter));
  el('revision-list').replaceChildren(...items.map(item => revisionRow(item)));
  if (!items.length) el('revision-list').append(rows('p', filter ? 'No matching resumes.' : 'No generated resumes yet.'));
  const recent = revisions.slice(0, 3).map(item => revisionRow(item, 'h4'));
  el('recent-list').replaceChildren(...(recent.length ? recent : [rows('p', 'No generated resumes yet.')]));
}
el('history-filter').addEventListener('input', renderHistory);

/* ---------- Live processing ---------- */
function resetLive() {
  el('job-title').textContent = 'Waiting to start — 0%';
  el('job-progress').value = 0;
  el('job-percent').textContent = '0%';
  el('job-count').textContent = '0 of 8 complete';
  el('job-stage').textContent = 'Waiting for the watcher';
  el('job-status').textContent = 'Queued';
  el('job-heartbeat').textContent = 'not started';
  el('job-detail').textContent = '';
  el('job-error-detail').hidden = true;
  el('job-warnings').replaceChildren();
  el('job-retry').hidden = true;
  el('job-pipeline').replaceChildren(...ResumeLive.PIPELINE.map(([label]) => {
    const node = rows('li', label);
    node.dataset.state = 'todo';
    return node;
  }));
}

function renderWorker(snapshot) {
  worker = snapshot.worker || {};
  const pill = el('worker-pill');
  const overleafDown = worker.overleaf && worker.overleaf.ok === false;
  const mongoDown = worker.mongodb && worker.mongodb.ok === false;
  pill.dataset.state = !snapshot.healthy ? 'offline' : overleafDown || mongoDown ? 'warning' : 'online';
  pill.textContent = !snapshot.healthy ? 'Watcher offline' : overleafDown ? 'Overleaf sign-in needed' : 'Watcher online';
  pill.title = [snapshot.lastScan ? 'Last watcher scan ' + loc(snapshot.lastScan) : 'The watcher has not reported yet',
    overleafDown ? 'Overleaf session signed out: run Sign_In_Overleaf.bat on the RTX laptop' : '',
    worker.mongodb ? (worker.mongodb.ok === false ? 'MongoDB unreachable; ' + (worker.mongodb.pending || 0) + ' saves queued' :
      'MongoDB connected') : ''].filter(Boolean).join(' · ');
}

async function refresh() {
  if (refreshPending) return;
  refreshPending = true;
  try {
    const snapshot = await request('/api/status');
    revisions = snapshot.revisions;
    const historyVersion = JSON.stringify(revisions);
    if (historyVersion !== historySnapshot) {
      historySnapshot = historyVersion;
      renderHistory();
    }
    if (typeof snapshot.now === 'number') serverOffset = snapshot.now - Date.now() / 1000;
    renderWorker(snapshot);
    const others = snapshot.jobs.filter(item => item.name !== watchedJob && ['processing','queued'].includes(item.status));
    el('job-others').hidden = !others.length;
    el('job-others').textContent = others.length ? 'Also in the watcher: ' + others.map(item =>
      (item.candidate || 'Candidate') + ' (' + (item.status === 'queued' ? 'waiting' : item.percent + '%') + ')').join(', ') : '';
    if (!watchedJob) return;
    const job = snapshot.jobs.find(item => item.name === watchedJob);
    if (!job) {
      // A job queued moments ago may not be listed by the first poll; only a job that was listed and
      // then vanished (or never appears) is reported as missing.
      const recent = !seenWatched && Date.now() - queuedAt < 30000;
      el('job-stage').textContent = recent ? 'Waiting for the watcher to list the job' :
        'This job is no longer in the inbox.';
      return;
    }
    seenWatched = true;
    lastJob = job;
    renderLive();
    const revision = revisions.find(item => item.key === job.revision);
    // Reset only after the watcher reports a verified 100% result whose outputs are listed.
    if (job.status === 'ok' && job.percent === 100 && revision) completeJob(job, revision);
  } catch (error) {
    if (watchedJob) el('job-status').textContent = 'Connection interrupted. Checking again; no work is restarted.';
  } finally {
    refreshPending = false;
  }
}

function renderLive() {
  if (!lastJob) return;
  const now = Date.now() / 1000 + serverOffset;
  const view = ResumeLive.describe(lastJob, now, shownPercent);
  const labels = {waiting:'Waiting', running:'Running', processing:'Processing', stalled:'No heartbeat',
    retrying:'Retrying', failed:'Failed', completed:'Completed'};
  el('generation').dataset.state = view.state;
  if ((view.state === 'failed' || view.state === 'retrying') && /^Resume queued\.$/.test(el('builder-status').textContent)) feedback('');
  el('job-title').textContent = view.title;
  el('job-candidate').textContent = (lastJob.candidate ? lastJob.candidate + ' · ' : '') +
    'Progress comes from the watcher in real time.';
  el('job-state').textContent = labels[view.state];
  el('job-state').dataset.state = view.state;
  el('job-progress').value = view.percent;
  el('job-percent').textContent = view.percent + '%';
  el('job-count').textContent = view.completedCount + ' of 8 complete';
  el('job-stage').textContent = view.stage;
  el('job-elapsed').textContent = view.elapsed;
  el('job-status').textContent = view.status;
  el('job-heartbeat').textContent = view.heartbeat;
  const runtime = view.gpu || (worker.ollama ? ResumeLive.gpuText(worker.ollama) +
    (worker.ollama.checked ? ' · measured ' + ResumeLive.clock(now - worker.ollama.checked) + ' ago' : '') : '');
  el('job-gpu').textContent = runtime || 'Not reported yet (measured after the AI step)';
  el('job-detail').textContent = view.detail;
  el('job-error-detail').textContent = view.errorDetail || '';
  el('job-error-detail').hidden = !view.errorDetail;
  el('job-warnings').replaceChildren(...view.warnings.map(text => rows('li', text)));
  el('job-pipeline').replaceChildren(...view.pipeline.map(item => {
    const node = rows('li', item.label);
    node.dataset.state = item.state;
    if (item.state === 'current') node.setAttribute('aria-current', 'step');
    return node;
  }));
  el('job-stages').replaceChildren(...view.stages.map(item => {
    const node = rows('li', item.label);
    node.dataset.state = item.state;
    if (item.state === 'current') node.setAttribute('aria-current', 'step');
    return node;
  }));
  el('job-retry').hidden = !view.canRetry;
  el('job-next').hidden = view.complete;
  el('job-timestamp').textContent = 'Last update ' + loc(lastJob.updated || lastJob.last);
  el('job-mini-name').textContent = lastJob.candidate || 'Resume';
  el('job-mini-text').textContent = view.state === 'failed' || view.state === 'retrying' ? '· ' + view.title :
    '· ' + view.percent + '% · ' + (lastJob.stage_label || view.stage);
}

function completeJob(job, revision) {
  const remembered = storedJob();
  // Clear the builder only if it still holds the submitted candidate, so a new candidate typed
  // while the previous job ran is never lost. Results are kept either way.
  const unchanged = !remembered || remembered.name !== job.name || remembered.submitted === JSON.stringify(payload());
  const onProcessing = activeStep === STEP.PROCESSING;
  watchedJob = '';
  showRevision(revision, unchanged || onProcessing).then(shown => {
    if (!shown) { watchedJob = job.name; return; }   // outputs not readable yet: keep watching
    el('result-success').hidden = false;
    if (/queued|already/.test(el('builder-status').textContent)) feedback('');
    lastJob = null;
    forgetJob();
    if (unchanged) {
      clearForm();
      showStep(STEP.RESULTS, false);
      el('results').scrollIntoView({block:'start'});
    } else {
      renderStepper();
      el('job-mini').hidden = activeStep === STEP.RESULTS;
      el('job-mini-name').textContent = job.candidate || 'Resume';
      el('job-mini-text').textContent = '· completed and verified';
      el('job-mini-open').textContent = 'View results';
    }
  });
}

el('job-retry').addEventListener('click', async () => {
  if (!lastJob) return;
  const button = el('job-retry');
  button.disabled = true;
  try {
    await request('/api/retry', {name:lastJob.name});
    el('job-status').textContent = 'Retry requested. The watcher will start it at its next scan.';
  } catch (error) {
    el('job-status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

/* ---------- Layout ---------- */
function applyLayout() {
  // Optional personal fields are open on desktop and collapsed on phones to save scrolling.
  for (const group of form.querySelectorAll('details[data-desktop-open]')) group.open = !mobileLayout.matches;
}
mobileLayout.addEventListener('change', applyLayout);

/** Called by import.js after a resume was read; fills the builder and moves on to the role. */
async function applyImport(data) {
  const profile = data.profile;
  const asText = value => typeof value === 'string' ? value : '';
  const joinLines = value => Array.isArray(value) ? value.filter(item => typeof item === 'string').join('\n') : '';
  const target = data.target && typeof data.target === 'object' && !Array.isArray(data.target) ? data.target : {};
  for (const key of ['full_name','email','phone','location','current_company','headline','linkedin','github','portfolio','summary']) {
    fields[key].value = asText(profile[key]);
  }
  for (const key of ['skills','certifications','projects']) fields[key].value = joinLines(profile[key]);
  for (const kind of ['experience','education']) {
    el(kind + '-entries').replaceChildren();
    for (const entry of (Array.isArray(profile[kind]) ? profile[kind] : []).slice(0, 40)) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry) || !Object.values(entry).some(Boolean)) continue;
      const card = addEntry(kind, false);
      for (const input of card.querySelectorAll('[data-field]')) {
        const value = entry[input.dataset.field];
        input.value = input.dataset.field === 'bullets' ? joinLines(value) : asText(value);
      }
      entryState(card);
    }
  }
  updateCounts();
  // A new upload must not inherit role, keywords or experience from a previous person.
  fields.role.value = '';
  fields.template.selectedIndex = 0;
  fields.keywords.value = '';
  el('catalog-keywords').textContent = '';
  yearsTouched = false;
  derivedNote = '';
  fields.years.value = '0';
  if (typeof target.role === 'string' && [...fields.role.options].some(option => option.value === target.role)) {
    fields.role.value = target.role;
    await roleChanged();
  }
  if (typeof target.template === 'string' && [...fields.template.options].some(option => option.value === target.template)) {
    fields.template.value = target.template;
  }
  if (Array.isArray(target.keywords)) fields.keywords.value = joinLines(target.keywords);
  if (Number.isInteger(target.experience_years) && target.experience_years >= 0 && target.experience_years <= 25) {
    fields.years.value = String(target.experience_years);
    yearsTouched = true;
  } else {
    deriveYears();
  }
  updateExperience();
  showTemplate();
  fields.verified.checked = false;
  el('import-source').value = asText(data.source);
  imported = true;
  // Never show the previous candidate's output after a new import.
  ++selection;
  shownRevision = '';
  resultsReady = false;
  el('pdf-preview').removeAttribute('src');
  furthestStep = STEP.INPUT;
  showStep(STEP.ROLE);
}

/* ---------- Start up ---------- */
applyLayout();
updateExperience();
updateCounts();
resetLive();
showStep(STEP.START, false);

async function init() {
  const restored = storedJob();
  if (restored && restored.name) {
    watchedJob = restored.name;
    queuedAt = restored.queued || 0;
    el('job-stage').textContent = 'Restoring the active job…';
    showStep(STEP.PROCESSING, false);
  }
  try {
    const catalog = await request('/api/catalog');
    roles = catalog.roles;
    templates = catalog.templates;
    fields.role.replaceChildren(new Option('Select a role', ''), ...roles.map(role => new Option(role.name, role.id)));
    fields.template.replaceChildren(...Object.entries(templates).map(([key, name]) => new Option(name, key)));
    showTemplate();
  } catch (error) {
    feedback(error.message, true);
  }
  // Poll the authoritative status every second while a job is tracked, otherwise every 2 s.
  const poll = async () => { await refresh(); setTimeout(poll, activeJob() ? 1000 : 2000); };
  poll();
  setInterval(renderLive, 1000);   // elapsed-time and heartbeat clocks only; progress comes from the server
}
init();

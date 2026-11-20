'use strict';

const byId = id => document.getElementById(id);
const form = byId('ai-form');
const fileControl = byId('auto-file');
const role = form.elements.role;
const customRole = byId('auto-custom-role');
const CUSTOM = 'Custom Role';
const required = [['full_name','Full name'], ['phone','Phone','tel']];
const PLACEHOLDERS = {full_name: 'Enter full name', phone: 'Enter phone number'};
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

// Icons come from the page's SVG sprite (<symbol id="i-…">); they are decorative, so screen readers skip them.
function icon(name, extra = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', extra ? 'ic ' + extra : 'ic');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

// A button or link with an icon and a visible text label (in a span, so the label can change).
function iconControl(tag, name, text, className) {
  const node = element(tag);
  if (tag === 'button') node.type = 'button';
  node.className = className;
  node.append(icon(name), element('span', text));
  return node;
}

function input([key, label, type = 'text'], mandatory = false) {
  const node = element('label');
  const text = element('span', label);
  text.className = 'auto-lbl';
  if (mandatory) {
    const mark = element('span', '*');  // shown, not read: the field itself is marked required
    mark.className = 'auto-req';
    mark.setAttribute('aria-hidden', 'true');
    text.append(mark);
  }
  node.append(text);
  const field = element(type === 'textarea' ? 'textarea' : 'input');
  field.name = key;
  if (type === 'textarea') { field.rows = 3; field.maxLength = 3000; node.classList.add('auto-wide'); }
  else { field.type = type; field.maxLength = 250; }
  field.required = mandatory;
  if (PLACEHOLDERS[key]) field.placeholder = PLACEHOLDERS[key];
  if (mandatory) field.setAttribute('aria-label', label);  // the name without the visual '*'
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
    'Custom role';
}

function selectedRole() {
  return role.value.trim().toLowerCase() === CUSTOM.toLowerCase() ? customRole.value.trim() : role.value.trim();
}

const SECTION_ICONS = {work_experience: 'briefcase', education: 'cap', projects: 'code', coding_platforms: 'github'};

function addSection([key, title, fields]) {
  const details = element('details');
  const summary = element('summary');
  const heading = element('span', title);
  heading.className = 'auto-sum';
  summary.append(icon('chevron-right', 'auto-chev'), icon(SECTION_ICONS[key] || 'doc'), heading);
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
  function update() { heading.textContent = title + ' (' + rows.children.length + ')'; }
  details.append(summary, rows, add);
  byId('auto-lists').append(details);
}

// The candidate-details fields: the whole form in AI-first mode, and an optional "Add details" section under
// the upload field (name and phone optional there, since the documents usually state them).
const manualHome = document.createComment('auto-manual');
byId('auto-manual').before(manualHome);

function mode() {
  const manual = form.elements.method.value === 'manual';
  const fields = byId('auto-manual');
  if (manual) manualHome.after(fields); else byId('auto-extra-slot').append(fields);
  fields.hidden = false;
  fields.disabled = false;
  fields.classList.toggle('auto-basics-optional', !manual);
  for (const field of byId('auto-basics').querySelectorAll('input')) field.required = manual;
  byId('auto-extra').hidden = manual;
  byId('auto-upload').hidden = manual;
  fileControl.disabled = manual;
  return manual;
}

// True when anything was typed in the details fields (a blank "Add entry" row counts as nothing).
function typedAnything(info) {
  return Object.entries(info).some(([key, value]) => Array.isArray(value)
    ? value.some(row => Object.values(row).some(item => String(item).trim()))
    : value !== null && String(value).trim() !== '');
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
  const typed = candidate();
  for (const [key] of collections) typed[key] = typed[key].filter(row => Object.values(row).some(v => v.trim()));
  return typedAnything(typed) ? {...data, documents, candidate: typed} : {...data, documents};
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
      const heading = rows.previousElementSibling.querySelector('.auto-sum');
      heading.textContent = heading.textContent.replace(/\(\d+\)$/, '(0)');
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

// A download link with the short visible text of the design and a full accessible name ("Download LaTeX").
function downloadLink(job, text, label, filename, iconName, className) {
  const link = iconControl('a', iconName, text, className);
  link.setAttribute('aria-label', label);
  link.href = '/api/artifact?' + new URLSearchParams({key: job.revision, name: filename, download: '1'});
  link.addEventListener('click', async event => {
    event.preventDefault();
    closeMenus();
    try {
      const action = {url: link.getAttribute('href'), name: filename};
      await ResumeRecovery.put('automation-download', action);
      await ResumeRecovery.download(action, 'automation-download');
    } catch (error) { progress(job.stage, 100, error.message); }
  });
  return link;
}

// The card's visible actions: Download PDF and Preview (Details is added by the card when it has details).
function primaryActions(job) {
  const box = element('div');
  box.className = 'auto-actions';
  const preview = iconControl('a', 'eye', 'Preview', 'auto-btn');
  preview.setAttribute('aria-label', 'Preview PDF');
  preview.href = '/api/artifact?' + new URLSearchParams({key: job.revision, name: 'resume.pdf'});
  preview.target = '_blank';
  preview.rel = 'noopener';
  box.append(downloadLink(job, 'Download PDF', 'Download PDF', 'resume.pdf', 'download', 'auto-btn auto-primary'), preview);
  return box;
}

// Three-dot menu: LaTeX, Overleaf PDF, Open Overleaf, Regenerate and Delete. It opens under its button,
// right-aligned (upward when there is no room below), above the page, and closes on a choice, a click
// elsewhere, Escape, scrolling or resizing. Arrow keys move between its items.
const POPOVER = typeof HTMLElement === 'function' && Object.prototype.hasOwnProperty.call(HTMLElement.prototype, 'popover');
let menuCount = 0;

function menuOpen(panel) {
  return POPOVER ? panel.matches(':popover-open') : panel.classList.contains('is-open');
}

function hideMenu(panel) {
  if (!menuOpen(panel)) return;
  if (POPOVER) panel.hidePopover();
  else {
    panel.classList.remove('is-open');
    panel.previousElementSibling.setAttribute('aria-expanded', 'false');
  }
}

function closeMenus() {
  for (const panel of document.querySelectorAll('.auto-menu')) hideMenu(panel);
}

function followMenus() {
  if (!POPOVER) return;  // the fallback menu sits in the card and moves with it
  for (const panel of document.querySelectorAll('.auto-menu')) {
    if (!menuOpen(panel)) continue;
    const button = panel.previousElementSibling;
    const box = button.getBoundingClientRect();
    const list = byId('auto-history').getBoundingClientRect();
    const top = Math.max(0, list.top);
    const bottom = Math.min(document.documentElement.clientHeight, list.bottom);
    if (!button.isConnected || box.bottom <= top || box.top >= bottom) hideMenu(panel);
    else placeMenu(button, panel);
  }
}

function placeMenu(button, panel) {
  if (!POPOVER) return;  // the fallback menu is positioned by CSS under its button
  let height = panel.offsetHeight;
  if (!height) {  // not shown yet: measure it invisibly
    panel.classList.add('auto-measure');
    height = panel.offsetHeight;
    panel.classList.remove('auto-measure');
  }
  const box = button.getBoundingClientRect();
  const view = document.documentElement;
  const below = view.clientHeight - box.bottom;
  const upward = below < height + 14 && box.top > below;
  panel.style.top = Math.max(8, upward ? box.top - 6 - height : box.bottom + 6) + 'px';
  panel.style.right = Math.max(8, view.clientWidth - box.right) + 'px';
}

function moreMenu(job, items) {
  const button = element('button');
  button.type = 'button';
  button.className = 'auto-btn auto-kebab';
  button.setAttribute('aria-label', 'More actions for ' + (job.candidate || 'this resume'));
  button.setAttribute('aria-expanded', 'false');
  button.append(icon('more'));
  const panel = element('div');
  panel.className = 'auto-menu';
  panel.id = 'auto-menu-' + (++menuCount);
  button.setAttribute('aria-controls', panel.id);
  panel.append(...items);
  if (POPOVER) {
    panel.popover = 'auto';
    button.popoverTargetElement = panel;
    panel.addEventListener('beforetoggle', event => {
      const open = event.newState === 'open';
      button.setAttribute('aria-expanded', String(open));
      if (open) placeMenu(button, panel);
    });
    panel.addEventListener('toggle', event => {
      if (event.newState !== 'open') return;
      placeMenu(button, panel);
      panel.querySelector('a, button')?.focus();
    });
  } else {
    button.addEventListener('click', () => {
      const open = !menuOpen(panel);
      closeMenus();
      if (!open) return;
      panel.classList.add('is-open');
      button.setAttribute('aria-expanded', 'true');
      panel.querySelector('a, button')?.focus();
    });
  }
  panel.addEventListener('keydown', event => {
    const choices = [...panel.querySelectorAll('a, button')];
    const at = choices.indexOf(document.activeElement);
    const to = {ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: choices.length - 1}[event.key];
    if (to !== undefined) {
      event.preventDefault();
      choices[(to + choices.length) % choices.length].focus();
    } else if (event.key === 'Escape' && !POPOVER) {
      hideMenu(panel);
      button.focus();
    }
  });
  return [button, panel];
}

function menuItems(job, regenerate, remove) {
  const items = [];
  if (job.status === 'complete') {
    items.push(downloadLink(job, 'LaTeX', 'Download LaTeX', 'main.tex', 'latex', ''),
      downloadLink(job, 'Overleaf PDF', 'Download Overleaf PDF', 'overleaf.pdf', 'file-down', ''));
    if (job.overleaf && /^https:\/\/www\.overleaf\.com\/project\/[A-Za-z0-9_-]+$/.test(job.overleaf)) {
      const link = iconControl('a', 'external', 'Open Overleaf', '');
      link.setAttribute('aria-label', 'Open Overleaf project');
      link.href = job.overleaf;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.addEventListener('click', () => setTimeout(closeMenus));
      items.push(link);
    }
    if (regenerate) items.push(regenerate);
  }
  if (remove) items.push(...(items.length ? [element('hr')] : []), remove);
  return items;
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
    ['Suggested keywords (add only if you have used them)', ats.suggested_skills || ats.missing || []],
    ['Recommended certifications (list one only after completing it)', ats.recommended_certifications || []]];
  for (const [title, values] of lists) {
    if (!values.length) continue;
    box.append(element('h4', title));
    const list = element('ul');
    list.append(...values.slice(0, 15).map(value => element('li', value)));
    box.append(list);
  }
  return box;
}

// Certifications worth earning for the role: unticked, because a certification is printed only once completed.
// The candidate ticks the ones they have, or types others; nothing else is added.
function certificationChoices(job) {
  if (!(job.certifications || []).length) return null;
  const box = element('fieldset');
  box.className = 'auto-certs';
  box.append(element('legend', 'Completed certifications (optional)'));
  for (const name of job.certifications) {
    const option = element('label'); const box_ = element('input');
    box_.type = 'checkbox'; box_.value = name; box_.name = 'certification';
    option.append(box_, ' ' + name); box.append(option);
    // A course's title changes with each edition ("... 2025 Edition"): a ticked course is printed with the title
    // exactly as on the candidate's certificate, which they confirm or correct here.
    const course = /^Udemy: (.+)$/.exec(name);
    if (course) {
      const title = element('input'); title.name = 'certification_title'; title.maxLength = 110; title.value = course[1];
      title.setAttribute('aria-label', 'Course title exactly as on your certificate');
      const titleLabel = element('label', 'Title exactly as on your Udemy certificate');
      titleLabel.className = 'auto-cert-title'; titleLabel.hidden = true; titleLabel.append(title);
      box_.titleField = title;
      box_.addEventListener('change', () => { titleLabel.hidden = !box_.checked; if (box_.checked) title.select(); });
      box.append(titleLabel);
    }
  }
  const other = element('textarea'); other.name = 'other_certifications'; other.rows = 2; other.maxLength = 900;
  const otherLabel = element('label', 'Other certifications (one per line)'); otherLabel.append(other);
  box.append(otherLabel);
  return box;
}

function chosenCertifications(form) {
  const box = form.querySelector('.auto-certs');
  if (!box) return undefined;
  const ticked = [...box.querySelectorAll('input[type=checkbox]:checked')].map(item => {
    if (!item.titleField) return item.value;
    const title = item.titleField.value.trim().replace(/^Udemy:\s*/i, '');
    if (!title) {
      const error = new Error('Enter the course title exactly as on your certificate, or untick the course.');
      error.control = item.titleField;
      throw error;
    }
    return 'Udemy: ' + title;
  });
  const typed = box.querySelector('[name=other_certifications]').value.split('\n').map(line => line.trim()).filter(Boolean);
  return [...new Set([...ticked, ...typed])];
}

// Suggested projects: never resume content until the candidate confirms the card. Like the drafted work lines,
// each starts included as an AI draft; the candidate edits it or chooses Skip, then confirms once.
// The background (problem, architecture, features) stays folded so the card is short on a phone.
function proposalCards(job) {
  return job.proposals.map((proposal, index) => {
    const card = element('fieldset');
    card.className = 'auto-proposal';
    card.dataset.index = index;
    card.append(element('legend', 'Suggested project ' + (index + 1) + ' (AI draft, not yet confirmed)'));
    const name = element('input'); name.name = 'name'; name.value = proposal.name; name.maxLength = 70;
    const nameLabel = element('label', 'Project name'); nameLabel.append(name);
    const facts = element('dl');
    for (const [term, value] of [['Problem', proposal.problem], ['Architecture', proposal.architecture],
      ['Key features', (proposal.features || []).join('; ')], ['Responsibilities', proposal.responsibilities]]) {
      if (value) facts.append(element('dt', term), element('dd', value));
    }
    const about = element('details');
    about.className = 'auto-about';
    about.append(element('summary', 'About this project'), facts);
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
      radio.checked = value === 'include';
      option.append(radio, ' ' + text); choice.append(option);
    }
    card.append(nameLabel, stackLabel, bulletsLabel, choice, ...(facts.children.length ? [about] : []));
    return card;
  });
}

// The decisions on the project cards, or null after marking the first one left undecided.
function projectDecisions(cards) {
  const undecided = cards.find(card => !card.querySelector('input[type=radio]:checked'));
  if (undecided) { fieldError(undecided.querySelector('input[type=radio]'), 'Choose include or skip.'); return null; }
  return cards.map(card => {
    const include = card.querySelector('input[type=radio]:checked').value === 'include';
    return include ? {include, name: card.querySelector('[name=name]').value, stack: card.querySelector('[name=stack]').value,
      bullets: card.querySelector('[name=bullets]').value} : {include};
  });
}

function proposalsForm(job) {
  const box = element('form');
  box.className = 'auto-answer auto-proposals';
  box.noValidate = true;
  box.append(element('p', job.error || 'Suggested projects are ready for your review.'), ...proposalCards(job));
  const certs = certificationChoices(job);
  if (certs) box.append(certs);
  const submit = element('button', 'Save project choices');
  submit.type = 'submit';
  submit.className = 'primary';
  box.append(submit);
  box.addEventListener('submit', async event => {
    event.preventDefault();
    clearFieldErrors(box);
    const cards = [...box.querySelectorAll('.auto-proposal')];
    const decisions = projectDecisions(cards);
    if (!decisions) return;
    submit.disabled = true;
    try {
      const certifications = chosenCertifications(box);
      const response = await ResumeAuth.call('/api/automation/projects', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: job.id, decisions, ...(certifications ? {certifications} : {})})});
      const result = await response.json();
      if (!response.ok) {
        const card = /^project\.(\d+)$/.exec(result.field || '');
        const certField = result.field === 'certifications' ? box.querySelector('[name=other_certifications]') : null;
        fieldError(card ? cards[Number(card[1])].querySelector('[name=bullets]') : certField || submit,
          result.error || 'Could not save.');
        return;
      }
      tracked = job.id;
      await refresh();
    } catch (error) { fieldError(error.control || submit, error.message); }
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

// "Nov 20, 2026, 2:42:51 PM"
function createdAt(job) {
  return job.created ? new Date(job.created * 1000).toLocaleString('en-US', {month: 'short', day: 'numeric',
    year: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'}) : '';
}

function meta(job) {
  return [job.candidate || 'Candidate', job.role, createdAt(job)].filter(Boolean).join(' · ');
}

// Avatar initials: first and last name, ignoring notes in brackets ("Ravi Example (fictional)" -> "RE").
function initials(name) {
  const words = (name || '').replace(/\([^)]*\)/g, ' ').split(/\s+/)
    .map(word => word.replace(/[^\p{L}\p{N}]/gu, '')).filter(Boolean);
  return (words.length > 1 ? [words[0], words[words.length - 1]] : words).map(word => word[0].toUpperCase()).join('') || 'CV';
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
  if (job.status === 'needs_input') progress('Needs your input', null,
    (job.questions || []).some(question => question.drafted) ? 'Review the drafts in My resumes.'
      : (job.questions || []).length ? 'Answer the questions in My resumes.'
        : 'Review the suggested projects in My resumes.');
  else if (job.status === 'blocked') progress('Stopped', null, 'See My resumes for the reason.');
  else progress(job.stage, job.percent, job.status === 'retrying' ? retryText(job) : '');
}

// Work lines and skills arrive as AI drafts for the selected role: several lines, editable, and optional.
const DRAFTED = new Set(['bullets', 'skills']);

function answerForm(job) {
  const box = element('form');
  box.className = 'auto-answer';
  box.noValidate = true;
  box.append(element('p', job.error || 'Your documents do not state some required details.'));
  for (const question of job.questions || []) {
    const label = element('label', question.label);
    if (question.drafted) label.classList.add('auto-drafted');
    const multiline = question.field === 'source_text' || DRAFTED.has(question.field);
    const field = element(multiline ? 'textarea' : 'input');
    field.name = question.id;
    if (field.tagName === 'INPUT') field.type = question.field === 'email' ? 'email' : 'text';
    // Drafted work lines show whole (no scrolling inside the box on a phone), up to eight rows.
    else field.rows = question.field === 'bullets' ? Math.min(8, Math.max(3, (question.value || '').split('\n').length))
      : question.field === 'skills' ? 2 : 3;
    field.maxLength = question.field === 'source_text' ? 7500 : question.field === 'bullets' ? 2400
      : DRAFTED.has(question.field) ? 1500 : 250;
    field.required = !DRAFTED.has(question.field);
    field.value = question.value || '';
    const hint = element('span', question.explanation);
    hint.className = 'field-hint';
    label.append(field, hint);
    box.append(label);
  }
  const cards = (job.proposals || []).length ? proposalCards(job) : [];
  box.append(...cards);
  const certs = certificationChoices(job);
  if (certs) box.append(certs);
  const submit = element('button', cards.length || (job.questions || []).some(q => q.drafted) ? 'Confirm and continue' : 'Continue');
  submit.type = 'submit';
  submit.className = 'primary';
  box.append(submit);
  box.addEventListener('submit', async event => {
    event.preventDefault();
    clearFieldErrors(box);
    const fields = [...box.querySelectorAll('input, textarea')]
      .filter(field => !field.closest('.auto-proposal') && !field.closest('.auto-certs'));
    const empty = fields.find(field => field.required && !field.value.trim());
    if (empty) { fieldError(empty, 'Enter this detail to continue.'); return; }
    const decisions = cards.length ? projectDecisions(cards) : undefined;
    if (cards.length && !decisions) return;
    let certifications;
    try { certifications = chosenCertifications(box); }
    catch (error) { fieldError(error.control || submit, error.message); return; }
    submit.disabled = true;
    try {
      const response = await ResumeAuth.call('/api/automation/answer', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: job.id, answers: Object.fromEntries(fields.map(field => [field.name, field.value])),
          ...(decisions ? {decisions} : {}), ...(certifications ? {certifications} : {})})});
      const result = await response.json();
      if (!response.ok) {
        const card = /^project\.(\d+)$/.exec(result.field || '');
        const target = card ? cards[Number(card[1])]?.querySelector('[name=bullets]')
          : result.field === 'certifications' ? box.querySelector('[name=other_certifications]')
          : result.field && fields.find(field => field.name === result.field);
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

// One card per resume, as in the design: initials, name, role and date; the real status and (when complete) the
// ATS score; Download PDF, Preview and Details; and the three-dot menu for everything else. Questions and project
// choices stay open on the card; the ATS breakdown and Regenerate open under Details. A card keeps exactly one
// <strong>: its status.
const STATUS_ICONS = {complete: 'check', needs_input: 'alert', blocked: 'stop'};

function jobItem(job) {
  const item = element('li');
  item.dataset.status = job.status;
  item.dataset.id = job.id;
  const main = element('div');
  main.className = 'auto-main';
  const avatar = element('span', initials(job.candidate));
  avatar.className = 'auto-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  const who = element('div');
  who.className = 'auto-meta';
  const name = element('span', job.candidate || 'Candidate');
  name.className = 'auto-name';
  name.title = job.candidate || '';
  who.append(name);
  if (job.role) {
    const role = element('span', job.role);
    role.className = 'auto-role';
    role.title = job.role;
    who.append(role);
  }
  if (job.created) {
    const date = element('span');
    date.className = 'auto-date';
    date.append(icon('calendar'), createdAt(job));
    who.append(date);
  }
  const state = element('div');
  state.className = 'auto-state';
  const detail = element('div');
  detail.className = 'auto-detail';
  const body = element('div');
  body.className = 'auto-body';
  body.id = 'auto-body-' + job.id.slice(0, 16);
  const pill = text => {
    const node = element('strong');
    node.append(icon(STATUS_ICONS[job.status] || 'clock'), text);
    return node;
  };
  if (job.status === 'needs_input') {
    state.append(pill('Needs your input'));
    body.append((job.questions || []).length ? answerForm(job) : proposalsForm(job));  // drafts and projects: one card
  } else if (job.status === 'blocked') {
    state.append(pill('Needs attention'));
    detail.append(element('span', explain(job.error)));
  } else if (job.status === 'complete') {
    state.append(pill('Complete'));
    if (job.ats) {
      const separator = element('span');
      separator.className = 'auto-sep';
      const score = element('span', 'ATS ' + job.ats.ats_readiness);
      score.className = 'auto-score';
      score.title = 'ATS readiness out of 100';
      state.append(separator, score);
    }
  } else {
    const bar = element('progress');
    bar.max = 100;
    bar.value = job.percent;
    bar.setAttribute('aria-label', 'Progress ' + job.percent + '%');
    state.append(pill(job.status === 'retrying' ? 'Retrying' : 'In progress'));
    detail.append(bar, element('span', job.stage + ' · ' + job.percent + '%'));
    if (job.status === 'retrying') detail.append(element('span', retryText(job)));
  }
  main.append(avatar, who, state);
  // Delete: stopped, waiting for answers, queued or complete. One being prepared right now is kept.
  let remove = null;
  if (job.status !== 'processing') {
    remove = iconControl('button', 'trash', 'Delete', 'auto-menu-danger');
    remove.setAttribute('aria-label', 'Delete ' + meta(job));
    remove.addEventListener('click', () => { closeMenus(); deleteOne(remove, job); });
  }
  for (const notice of job.notice || []) body.append(element('span', notice));
  let again = null;
  if (job.status === 'complete') {
    if (job.ats) body.append(atsDetails(job.ats));
    const regenerate = job.regenerable ? regenerateForm(job) : null;
    if (regenerate) body.append(regenerate);
    const actions = primaryActions(job);
    const details = iconControl('button', 'info', 'Details', 'auto-btn');
    const open = show => {
      body.hidden = !show;
      details.setAttribute('aria-expanded', String(show));
      details.lastChild.textContent = show ? 'Hide details' : 'Details';
    };
    details.setAttribute('aria-controls', body.id);
    details.addEventListener('click', () => open(body.hidden));
    if (regenerate) {
      again = iconControl('button', 'refresh', 'Regenerate', '');
      again.setAttribute('aria-controls', body.id);
      again.addEventListener('click', () => { closeMenus(); open(true); regenerate.querySelector('input').focus(); });
    }
    if (body.children.length) actions.append(details);
    main.append(actions);
    open(false);
  }
  const items = menuItems(job, again, remove);
  if (items.length) main.append(...moreMenu(job, items));
  item.append(main);
  if (detail.children.length) item.append(detail);
  if (body.children.length) item.append(body);
  return item;
}

// History: search, role and status filters, rows needing input first, then pages. On a desktop a page holds as
// many collapsed cards as fit in the panel (3 to 12), so the list does not scroll; on a phone it holds 6.
// Each job's element (and anything typed into it) is kept until that job itself changes.
const PHONE_PAGE_SIZE = 6;
const rendered = new Map();
let allJobs = [];
let page = 0;
let pageSize = PHONE_PAGE_SIZE;

// The height of one collapsed, complete card at the list's current width, measured on a hidden reference card
// (so every page holds the same number, whichever cards it shows). Re-measured only when the size changes.
let reference = {key: '', row: 84};

function cardHeight() {
  const list = byId('auto-job-list');
  const key = list.clientWidth + 'x' + innerHeight;
  if (reference.key === key) return reference.row;
  const probe = jobItem({id: 'reference'.padEnd(64, '0'), candidate: 'Reference Candidate', role: 'Reference role',
    created: 1, status: 'complete', stage: '', percent: 100, revision: 'reference/0', overleaf: '', regenerable: false,
    notice: [], ats: {ats_readiness: 99}});
  probe.style.visibility = 'hidden';
  probe.style.position = 'absolute';
  probe.style.width = list.clientWidth + 'px';
  list.append(probe);
  const row = probe.offsetHeight || 84;
  probe.remove();
  reference = {key, row};
  return row;
}

function fittedPageSize() {
  if (!matchMedia('(min-width: 901px)').matches) return PHONE_PAGE_SIZE;
  const list = byId('auto-job-list');
  const row = cardHeight();
  const gap = parseFloat(getComputedStyle(list).rowGap) || 12;
  const space = byId('auto-history').clientHeight - 8;
  return Math.max(3, Math.min(12, Math.floor((space + gap) / (row + gap))));
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// The summary strip: total, completed, and the completed share.
function renderStats(jobs) {
  const done = jobs.filter(job => job.status === 'complete').length;
  const percent = jobs.length ? Math.round(done * 100 / jobs.length) : 0;
  const count = byId('auto-count');
  if (count.textContent !== jobs.length + (jobs.length === 1 ? ' resume' : ' resumes')) {
    count.replaceChildren(element('span', String(jobs.length)), jobs.length === 1 ? ' resume' : ' resumes');
  }
  setText(byId('auto-done'), done + ' completed');
  byId('auto-done-bar').style.width = percent + '%';
  const share = byId('auto-done-percent');
  if (share.textContent !== percent + '% completed') share.replaceChildren(element('b', percent + '%'), ' completed');
}

// Page numbers (at most five, around the current page); hidden when everything fits on one page.
function renderPager(pages) {
  byId('auto-pager').hidden = pages <= 1;
  byId('auto-prev').disabled = page === 0;
  byId('auto-next').disabled = page >= pages - 1;
  const holder = byId('auto-pages');
  const key = pages + ':' + page;
  if (holder.dataset.key === key) return;
  const refocus = holder.contains(document.activeElement);
  holder.dataset.key = key;
  const start = Math.max(0, Math.min(page - 2, pages - 5));
  const numbers = [];
  for (let index = start; index < Math.min(pages, start + 5); index++) {
    const button = element('button', String(index + 1));
    button.type = 'button';
    button.setAttribute('aria-label', 'Page ' + (index + 1));
    if (index === page) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => turnPage(index - page));
    numbers.push(button);
  }
  holder.replaceChildren(...numbers);
  if (refocus) holder.querySelector('[aria-current]')?.focus();
}

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
  const fitted = fittedPageSize();
  if (fitted !== pageSize) {
    page = Math.floor(page * pageSize / fitted);  // keep the first card of the page in view
    pageSize = fitted;
  }
  const ordered = [...jobs.filter(job => job.status === 'needs_input'), ...jobs.filter(job => job.status !== 'needs_input')];
  const shown = ordered.filter(matches);
  const pages = Math.max(1, Math.ceil(shown.length / pageSize));
  page = Math.min(page, pages - 1);
  const list = byId('auto-job-list');
  const items = shown.slice(page * pageSize, (page + 1) * pageSize).map(job => {
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
  renderStats(jobs);
  // Which cards this page holds, e.g. "Showing 1 – 6 of 15 resumes".
  const first = page * pageSize + 1;
  const last = Math.min(shown.length, (page + 1) * pageSize);
  setText(byId('auto-page'), !jobs.length ? '' : shown.length ?
    'Showing ' + first + ' – ' + last + ' of ' + shown.length + (shown.length === 1 ? ' resume' : ' resumes')
    : 'No resumes match the search or filters');
  renderPager(pages);
}

function turnPage(step) {
  closeMenus();
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
  // An open menu stays with its button while the page or list scrolls (or the window resizes, as phone toolbars do),
  // and closes once the button leaves the visible part of the list. Resizing also refits the page.
  document.addEventListener('scroll', followMenus, {capture: true, passive: true});
  let resizing = 0;
  addEventListener('resize', () => {
    followMenus();
    clearTimeout(resizing);
    resizing = setTimeout(() => renderJobs(allJobs), 120);
  });
  if (!POPOVER) {
    document.addEventListener('click', event => { if (!event.target.closest('.auto-menu, .auto-kebab')) closeMenus(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenus(); });
  }
  form.addEventListener('change', event => { if (event.target.name === 'method') mode(); });
  role.addEventListener('input', roleChanged);
  role.addEventListener('change', roleChanged);
  form.addEventListener('invalid', event => {
    // Open every closed section around the field, and on phones "More details" too, which shows the lists.
    for (let details = event.target.closest('details'); details; details = details.parentElement.closest('details')) {
      details.open = true;
    }
    if (event.target.closest('#auto-lists')) {
      const more = event.target.closest('.auto-accordions')?.querySelector(':scope > details');
      if (more) more.open = true;
    }
    event.target.getBoundingClientRect();
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

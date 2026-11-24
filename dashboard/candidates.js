/* Operator view of public-form applications (/candidates): every application with everything the candidate entered,
   Start processing (the operator's own resume workflow on the candidate's typed details), where each resume is,
   queue control, downloads, new private links and deletion. Reads /api/intake/candidates (operator accounts only). */
'use strict';
const $ = id => document.getElementById(id);
let data = {candidates: []};
const opened = new Set();  // applications whose details are shown (kept across refreshes)
// /candidates#RO-XXXXXXXXXX (from a resume in My Resumes) opens that application's details once it is listed.
let linked = (location.hash.match(/^#(RO-[2-9A-HJKMNP-Z]{10})$/) || [])[1] || null;

// Share via WhatsApp: a professional invitation with the public form link, prefilled. wa.me opens the WhatsApp app on
// a phone and WhatsApp Web or Desktop on a computer; the operator picks the contact and taps send.
function whatsappLink(formUrl) {
  const text = ['Hello,', '',
    'Please share your details through the ResumeOps application form so our team can prepare your professional, '
      + 'ATS-friendly resume. It takes a few minutes and needs no login or account:',
    formUrl, '',
    'After you submit, you get a private link to follow your application and download your resume when it is ready.',
    '', 'Thank you!'].join('\n');
  return 'https://wa.me/?text=' + encodeURIComponent(text);
}

const LABELS = {submitted: 'Not started', queued: 'Waiting in queue', retrying: 'Retrying', processing: 'Preparing',
  handed_off: 'Building', needs_input: 'Waiting for review', complete: 'Complete', blocked: 'Stopped'};

// Line icons (24px grid, stroked), as in the approved Candidates design.
const ICONS = {
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  stop: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  play: '<path d="M7 4v16l13-8z"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 3v5h5"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 21v-5h-5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'cd-ic');
  svg.innerHTML = ICONS[name];
  return svg;
}

// A button with a leading icon; the label stays its accessible name.
function iconButton(name, text, className) {
  const button = el('button', null, className);
  button.type = 'button';
  button.append(icon(name), document.createTextNode(text));
  return button;
}

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
  if (!entry.started) return 'submitted';
  const status = entry.job?.status || 'queued';
  return status === 'retrying' ? 'queued' : status === 'handed_off' ? 'processing' : status;
}

function stats() {
  const counts = {};
  for (const entry of data.candidates) if (!entry.deleted) counts[bucket(entry)] = (counts[bucket(entry)] || 0) + 1;
  // Three summary cards; each application's detailed status stays on its own row (and in the status filter).
  const items = [['file', 'Applications', data.candidates.filter(entry => !entry.deleted).length],
    ['check', 'Completed', counts.complete || 0],
    ['chip', 'GPU Usage', `${data.gpu?.busy ?? 0} of ${data.gpu?.slots ?? 1}`]];
  $('cd-stats').replaceChildren(...items.map(([name, label, value]) => {
    const box = el('div', null, 'cd-stat');
    box.append(icon(name), el('strong', String(value)), el('span', label));
    return box;
  }));
  const paused = data.queue?.paused;
  $('cd-queue-state').textContent = paused ? 'Paused. Applications are saved.' :
    'One AI job at a time. Your resumes have priority.';
  $('cd-pause').replaceChildren(icon(paused ? 'play' : 'pause'), document.createTextNode(paused ? 'Resume processing' : 'Pause processing'));
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
  tr.id = 'cand-' + entry.candidate;
  const who = el('td');
  const created = el('span', null, 'cd-muted cd-when');
  created.append(icon('calendar'), document.createTextNode(when(entry.created)));
  who.append(el('strong', entry.deleted ? '(deleted by the candidate)' : entry.name), el('span', entry.candidate, 'cd-id'), created);
  const contact = el('td');
  if (!entry.deleted) contact.append(el('span', entry.email), el('span', entry.phone, 'cd-muted'));
  const role = el('td', entry.role || '');
  if (entry.versions > 1) role.title = `${entry.versions} versions`;  // compact rows; the count is on hover
  const state = el('td');
  const job = entry.job || {};
  const status = bucket(entry);
  if (status === 'submitted') {
    state.append(el('span', LABELS.submitted, 'cd-badge cd-submitted'), el('span', 'Waiting for you to start processing', 'cd-muted'));
  }
  if (status !== 'submitted') state.append(el('span', LABELS[job.status] || LABELS[status] || status, `cd-badge cd-${status}`));
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
  // A version prepared only in another operator account (the local dashboard on this computer, say): its files are
  // that account's, so none are offered here (they could not be downloaded); Process again prepares one here.
  const elsewhere = job.elsewhere ? 'Prepared in another ResumeOps account. Process again to get the files here.' : '';
  if (status === 'complete' && job.revision && !job.elsewhere) {
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
    // An application saved before processing became the operator's step has the candidate's own resume, which only
    // Start processing brings into the operator's review.
    const legacy = Boolean(entry.job?.candidates_own);
    if (!entry.started || entry.details_changed || legacy) {
      const label = !entry.started || legacy ? 'Start processing' : 'Start again with new details';
      const go = iconButton('play', label, 'cd-start');
      go.addEventListener('click', () => start(entry, go));
      outputs.prepend(go);
    } else if (status === 'complete' || status === 'blocked') {
      // A finished or stopped resume can be prepared again from the same details (the earlier one stays).
      const again = iconButton('refresh', 'Process again', 'cd-small');
      if (elsewhere) again.title = elsewhere;
      again.addEventListener('click', () => {
        if (window.confirm(`Prepare a new resume for ${entry.name} from the same details? The current one stays.`)) start(entry, again, true);
      });
      outputs.append(again);
    }
    if (status === 'needs_input' && !legacy && !job.elsewhere) {
      const review = el('a', 'Review in My Resumes', 'cd-link');
      review.href = '/#resume-' + job.id;  // that resume's card in My Resumes
      outputs.append(review);
    }
    const more = iconButton('info', opened.has(entry.candidate) ? 'Hide details' : 'Details', 'cd-small');
    more.setAttribute('aria-expanded', String(opened.has(entry.candidate)));
    more.addEventListener('click', () => {
      if (opened.has(entry.candidate)) opened.delete(entry.candidate); else opened.add(entry.candidate);
      render();
    });
    const key = iconButton('link', 'New private link', 'cd-small');
    key.addEventListener('click', () => newLink(entry));
    const drop = iconButton('trash', 'Delete', 'cd-small cd-danger');
    drop.addEventListener('click', () => remove(entry));
    outputs.append(more, key, drop);
  }
  // Without a Process again button to carry it, the note on another account's version stays visible.
  if (elsewhere && !outputs.querySelector('[title]')) outputs.append(el('span', elsewhere, 'cd-muted'));
  tr.append(who, contact, role, state, outputs);
  if (!opened.has(entry.candidate) || !entry.form) return [tr];
  const extra = el('tr', null, 'cd-details-row');
  const cell = el('td');
  cell.colSpan = 5;
  cell.append(details(entry.form, entry.candidate));
  extra.append(cell);
  return [tr, extra];
}

// Everything the candidate entered, as submitted (text only: nothing is interpreted as markup).
function details(form, candidate) {
  const box = el('div', null, 'cd-details');
  const lines = value => String(value || '').split('\n').map(line => line.trim()).filter(Boolean);
  const section = (title, pairs) => {
    const filled = pairs.filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '');
    if (!filled.length) return;
    const part = el('section');
    part.append(el('h3', title));
    const list = el('dl');
    for (const [label, value] of filled) list.append(el('dt', label), el('dd', String(value)));
    part.append(list);
    box.append(part);
  };
  const listed = (title, entries, describe) => {
    if (!entries.length) return;
    const part = el('section');
    part.append(el('h3', title));
    for (const item of entries) {
      const block = el('div', null, 'cd-entry');
      block.append(el('strong', describe.title(item)));
      for (const [label, value] of describe.fields(item)) {
        if (String(value || '').trim()) block.append(el('span', `${label}: ${value}`));
      }
      const points = lines(describe.points ? describe.points(item) : '');
      if (points.length) {
        const list = el('ul');
        for (const point of points) list.append(el('li', point));
        block.append(list);
      }
      part.append(block);
    }
    box.append(part);
  };
  const bullets = (title, value) => {
    if (!lines(value).length) return;
    const part = el('section');
    part.append(el('h3', title));
    const list = el('ul');
    for (const point of lines(value)) list.append(el('li', point));
    part.append(list);
    box.append(part);
  };
  const rows = name => (Array.isArray(form[name]) ? form[name] : []).filter(item => item && typeof item === 'object');
  section('Personal details', [['Full name', form.full_name], ['Email', form.email], ['Phone', form.phone],
    ['City', form.location], ['LinkedIn', form.linkedin], ['GitHub', form.github], ['Portfolio', form.portfolio]]);
  // Details for recruiters only (never printed on the resume): photo, date of birth, locations and salary.
  if (form.has_photo && candidate) {
    const part = el('section');
    part.className = 'cand-photo';
    part.append(el('h3', 'Passport photo'));
    const photo = el('img');
    photo.src = '/api/intake/photo?' + new URLSearchParams({candidate});
    photo.alt = 'Passport photo of ' + (form.full_name || 'the candidate');
    photo.width = 120;
    photo.loading = 'lazy';
    part.append(photo);
    box.append(part);
  }
  section('Recruiter details', [['Date of birth', form.dob], ['Current location', form.location],
    ['Preferred location', form.preferred_location], ['Current salary', form.current_salary],
    ['Salary type', form.salary_type]]);
  section('Target role', [['Role', form.role], ['Total experience (years)', form.years_of_experience],
    ['Fresher', form.fresher ? 'Yes' : '']]);
  section('Skills', [['Skills', form.skills]]);
  listed('Education', rows('education'), {
    title: item => [item.degree, item.specialization].filter(Boolean).join(', ') || 'Education',
    fields: item => [['Institution', item.university], ['Year of completion', item.graduation_year],
      ['CGPA / percentage', item.cgpa]]});
  listed('Employment', form.fresher ? [] : rows('work_experience'), {
    title: item => [item.position, item.company].filter(Boolean).join(' at ') || 'Job',
    fields: item => [['Dates', [item.start, item.end].filter(Boolean).join(' – ')], ['Location', item.location]],
    points: item => item.responsibilities});
  listed('Projects', rows('projects'), {
    title: item => item.name || 'Project', fields: item => [['Technologies', item.stack]],
    points: item => item.description});
  bullets('Certifications', form.certifications);
  bullets('Achievements', form.achievements);
  return box;
}

// again: Process again on a finished resume, a new version from the same details (the earlier ones stay).
async function start(entry, button, again = false) {
  button.disabled = true;
  const response = await ResumeAuth.call('/api/intake/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(again ? {candidate: entry.candidate, again: true} : {candidate: entry.candidate})});
  const result = await response.json().catch(() => ({}));
  $('cd-message').textContent = !response.ok ? (result.error || 'Could not start processing.') : result.created
    ? `Processing started for ${entry.name}. Its AI drafts appear for your review under My Resumes.`
    : `${entry.name}'s resume was already prepared from these exact details; open it in My Resumes.`;
  await refresh();
}

async function remove(entry) {
  if (!window.confirm(`Delete the application of ${entry.name} (${entry.candidate}) and every resume prepared from it? This cannot be undone.`)) return;
  const response = await ResumeAuth.call('/api/intake/delete', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({candidate: entry.candidate, confirm: 'DELETE'})});
  const result = await response.json().catch(() => ({}));
  $('cd-message').textContent = !response.ok ? (result.error || 'Could not delete the application.')
    : result.kept?.length ? 'Resume in progress. Delete after it finishes.'
      : `Deleted the application of ${entry.name}.`;
  opened.delete(entry.candidate);
  await refresh();
}

function render() {
  stats();
  const term = $('cd-search').value.trim().toLowerCase();
  const wanted = $('cd-status').value;
  // Applications their candidates deleted keep no details; they are only counted above.
  const shown = data.candidates.filter(entry => !entry.deleted && (!wanted || bucket(entry) === wanted) && (!term ||
    [entry.name, entry.email, entry.phone, entry.candidate, entry.role].some(item => (item || '').toLowerCase().includes(term))));
  $('cd-rows').replaceChildren(...shown.flatMap(row));
  $('cd-empty').hidden = data.candidates.some(entry => !entry.deleted);
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

// Open and show the application a link named, once (filters are cleared so it is in the list).
function showLinked() {
  if (!linked || !data.candidates.some(entry => entry.candidate === linked && !entry.deleted)) return;
  const id = linked;
  linked = null;
  $('cd-search').value = '';
  $('cd-status').value = '';
  opened.add(id);
  render();
  const target = document.getElementById('cand-' + id);
  if (!target) return;
  target.classList.add('cd-focus');
  target.scrollIntoView({block: 'start'});
  setTimeout(() => target.classList.remove('cd-focus'), 4000);
}

async function refresh() {
  const response = await ResumeAuth.call('/api/intake/candidates');
  const message = $('cd-message');
  if (!response.ok) {
    message.textContent = response.status === 404 || response.status === 403
      ? 'Only operator accounts can see candidates.' : 'Could not load candidates.';
    message.dataset.loading = '1';
    return;
  }
  data = await response.json();
  // The list refreshes every few seconds: it clears only its own loading error, never the result of an action
  // (seen in CI: "Processing started" vanished before it could be read).
  if (message.dataset.loading) { message.textContent = ''; delete message.dataset.loading; }
  if (!$('cd-form-url').value) {
    $('cd-form-url').value = data.form_url;
    $('cd-whatsapp').href = whatsappLink(data.form_url);
    $('cd-whatsapp').hidden = false;
  }
  showHuman(data.human_check);
  render();
  showLinked();
}

function showHuman(state) {
  if (!state) return;
  $('cd-human-state').textContent = !state.configured
    ? 'Off. Anyone can submit; rate limits apply.'
    : state.test_keys ? 'Test keys accept every request. Replace with production keys.'
      : `On: new applications and uploads need the check (site key ${state.site_key}).`;
  $('cd-human-off').hidden = !state.configured;
}

async function saveHuman(siteKey, secret) {
  const response = await ResumeAuth.call('/api/intake/human-check', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({site_key: siteKey, secret})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) { $('cd-human-state').textContent = result.error || 'Could not save the keys.'; return; }
  $('cd-human-secret').value = '';
  showHuman(result);
}

async function init() {
  $('cd-search').addEventListener('input', render);
  $('cd-status').addEventListener('change', render);
  $('cd-copy-form').addEventListener('click', () => navigator.clipboard.writeText($('cd-form-url').value).catch(() => $('cd-form-url').select()));
  $('cd-link-copy').addEventListener('click', () => navigator.clipboard.writeText($('cd-link').value).catch(() => $('cd-link').select()));
  $('cd-link-close').addEventListener('click', () => $('cd-link-dialog').close());
  $('cd-pause').addEventListener('click', async () => {
    const paused = !$('cd-pause').dataset.paused;
    if (paused && !window.confirm('Pause processing? Applications stay saved until you resume.')) return;
    await ResumeAuth.call('/api/intake/queue', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paused})});
    await refresh();
  });
  $('cd-human-form').addEventListener('submit', event => {
    event.preventDefault();
    saveHuman($('cd-human-site').value.trim(), $('cd-human-secret').value.trim());
  });
  $('cd-human-off').addEventListener('click', () => {
    if (window.confirm('Turn off the human check? The form then accepts applications without it.')) saveHuman('', '');
  });
  await ResumeAuth.ready();
  for (;;) {
    await refresh().catch(error => { $('cd-message').textContent = error.message; });
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
}
init();

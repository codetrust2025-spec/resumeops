'use strict';
const candidateForm = $('candidate-form');
let roleCatalog = [];

async function loadCatalog() {
  try {
    const response = await fetch('/api/catalog');
    if (!response.ok) throw new Error('Could not load roles and templates.');
    const catalog = await response.json();
    roleCatalog = catalog.roles;
    candidateForm.elements.role.replaceChildren(new Option('Select a role', ''),
      ...roleCatalog.map(role => new Option(role.name, role.id)));
    candidateForm.elements.template.replaceChildren(
      ...Object.entries(catalog.templates).map(([key, name]) => new Option(name, key)));
  } catch (error) {
    $('candidate-feedback').textContent = error.message;
  }
}

candidateForm.elements.role.addEventListener('change', () => {
  const role = roleCatalog.find(item => item.id === candidateForm.elements.role.value);
  $('role-keywords').textContent = role ? 'Target keywords: ' + role.keywords.join(', ') : '';
});

const entryFields = {
  experience: [['company','Employer'],['position','Position'],['start','Start date'],
    ['end','End date'],['location','Location'],['bullets','Confirmed achievements, one per line']],
  education: [['institution','Institution'],['degree','Degree'],['start','Start date'],
    ['end','End date'],['details','Details']],
};

function addEntry(kind) {
  const entry = document.createElement('fieldset');
  entry.append(text('legend', kind === 'experience' ? 'Experience' : 'Education'));
  const grid = text('div', '', 'candidate-grid');
  for (const [key, caption] of entryFields[kind]) {
    const label = text('label', caption);
    const field = document.createElement(key === 'bullets' ? 'textarea' : 'input');
    field.dataset.field = key;
    field.maxLength = key === 'bullets' ? 8000 : 250;
    if (['company','position','institution','degree'].includes(key) || (kind === 'experience' && key === 'start')) {
      field.required = true;
    }
    label.append(field);
    grid.append(label);
  }
  const remove = text('button', 'Remove entry');
  remove.type = 'button';
  remove.addEventListener('click', () => entry.remove());
  entry.append(grid, remove);
  $(kind + '-entries').append(entry);
}

$('add-experience').addEventListener('click', () => addEntry('experience'));
$('add-education').addEventListener('click', () => addEntry('education'));

const lines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
function readEntries(kind) {
  return Array.from($(kind + '-entries').querySelectorAll('fieldset'), entry =>
    Object.fromEntries(Array.from(entry.querySelectorAll('[data-field]'), input =>
      [input.dataset.field, input.dataset.field === 'bullets' ? lines(input.value) : input.value])));
}

candidateForm.addEventListener('submit', async event => {
  event.preventDefault();
  const feedback = $('candidate-feedback');
  const form = new FormData(candidateForm);
  const queue = event.submitter.value === 'queue';
  if (queue && !form.get('verified')) {
    feedback.textContent = 'Blocked: confirm the candidate facts before queueing.';
    return;
  }
  const profile = Object.fromEntries(['full_name','email','headline','phone','location','linkedin',
    'github','summary'].map(key => [key, form.get(key)]));
  Object.assign(profile, {skills: lines(form.get('skills')), certifications: lines(form.get('certifications')),
    experience: readEntries('experience'), education: readEntries('education')});
  const target = {role: form.get('role'), template: form.get('template'), keywords: lines(form.get('keywords'))};
  const payload = JSON.stringify({profile, target});
  event.submitter.disabled = true;
  feedback.textContent = '';
  try {
    if (queue) {
      feedback.textContent = queueMessage(await queueCandidate(payload));
      refresh();
    } else {
      const result = await submitFile('/api/preview', payload, {'Content-Type':'application/json'});
      const candidate = result.profile;
      $('preview-score').textContent = 'ATS keyword coverage: ' + result.ats.score + '%';
      $('preview-keywords').textContent = 'Matched: ' + (result.ats.matched.join(', ') || 'None') +
        ' · Missing: ' + (result.ats.missing.join(', ') || 'None');
      $('preview-content').textContent = [candidate.full_name, candidate.headline,
        [candidate.email,candidate.phone,candidate.location].filter(Boolean).join(' · '),
        candidate.summary, candidate.skills.join(', '),
        ...candidate.experience.map(item => [item.position + ' · ' + item.company,
          [item.start,item.end].filter(Boolean).join(' – '), ...item.bullets].join('\n')),
        ...candidate.education.map(item => item.degree + ' · ' + item.institution),
        candidate.certifications.join(', ')].filter(Boolean).join('\n\n');
      $('candidate-preview').hidden = false;
    }
  } catch (error) {
    feedback.textContent = 'Blocked: ' + error.message;
  } finally {
    event.submitter.disabled = false;
  }
});

function renderWorkflow(jobs) {
  const job = jobs.find(item => item.status === 'processing') ||
    jobs.find(item => item.status === 'failed') ||
    jobs.find(item => item.status === 'ok' && item.percent === 100);
  $('active-workflow').hidden = !job;
  if (!job) return;
  $('active-candidate').textContent = job.candidate || job.name;
  $('workflow-progress').value = job.percent;
  $('workflow-stage').textContent = job.percent + '% · ' + (job.stage || 'Waiting to retry') +
    (job.error ? ' · ' + job.error : '');
  $('workflow-time').textContent = 'Updated ' + date(job.updated) +
    (job.failures ? ' · Retries: ' + job.failures : '');
}

loadCatalog();

'use strict';
/* Five-step role-targeted resume wizard with live preview and real watcher progress. */
(function () {
  const App = window.ResumeApp;
  const {text, pill, link, setBar, artifactUrl, artifactLabels} = App;
  const form = $('wizard');
  let state = Core.emptyState();
  let step = 1;
  let maxStep = 1;
  let roles = [];
  let templates = {};
  let preview = null;        // last successful /api/preview response
  let previewError = '';
  let previewTimer = 0;
  let previewSeq = 0;
  let queued = null;         // {name} returned by /api/queue
  let dirty = false;

  /* ---------- Binding ---------- */
  const getPath = path => path.split('.').reduce((value, key) => value?.[key], state);
  function setPath(path, value) {
    const keys = path.split('.');
    const last = keys.pop();
    keys.reduce((value, key) => value[key], state)[last] = value;
  }
  function syncInputs() {
    for (const field of form.querySelectorAll('[data-bind]')) {
      const value = getPath(field.dataset.bind);
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else if (field.value !== (value ?? '')) field.value = value ?? '';
    }
  }
  form.addEventListener('input', event => {
    const field = event.target;
    if (field.dataset.bind) setPath(field.dataset.bind, field.type === 'checkbox' ? field.checked : field.value);
    else if (field.dataset.entry) {
      const [kind, index, key] = field.dataset.entry.split('.');
      state[kind][Number(index)][key] = field.value;
      if (['position', 'company', 'name', 'degree', 'institution'].includes(key)) updateEntryTitle(kind, Number(index));
    } else return;
    markDirty();
  });
  form.addEventListener('change', event => {
    if (event.target.dataset.bind === 'role') renderRoleDetail();
  });
  function markDirty() {
    dirty = true;
    $('draft-state').textContent = queued ? 'Queued · edits create a new revision' : 'Draft · Unsaved';
    if (step === 2) renderExperienceSummary();
    renderPaper();
    schedulePreview();
    renderErrors($('wizard-errors').children.length > 0);
  }

  /* ---------- Repeating entries ---------- */
  const ENTRY_FIELDS = {
    experience: [['position', 'Job title *'], ['company', 'Employer *'], ['start', 'Start date *', 'e.g. May 2023'],
      ['end', 'End date', 'e.g. Present'], ['location', 'Location'], ['bullets', 'Responsibilities and achievements (one per line)', '', true]],
    education: [['degree', 'Degree *'], ['institution', 'Institution *'], ['start', 'Start'], ['end', 'End'], ['details', 'Details', 'e.g. CGPA, honours', true]],
    projects: [['name', 'Project name *'], ['link', 'Link'], ['technologies', 'Technologies (comma-separated)', '', true],
      ['bullets', 'What the candidate built (one per line)', '', true]],
  };
  const BLANK = {experience: {company: '', position: '', start: '', end: '', location: '', bullets: ''},
    education: {institution: '', degree: '', start: '', end: '', details: ''},
    projects: {name: '', link: '', technologies: '', bullets: ''}};
  const entryTitle = (kind, item) => kind === 'experience' ? [item.position, item.company].filter(Boolean).join(' · ') || 'New experience'
    : kind === 'education' ? [item.degree, item.institution].filter(Boolean).join(' · ') || 'New education'
      : item.name || 'New project';
  function updateEntryTitle(kind, index) {
    const node = $(kind + '-entries').children[index]?.querySelector('.entry-head strong');
    if (node) node.textContent = entryTitle(kind, state[kind][index]);
  }
  function renderEntries(kind) {
    const nodes = state[kind].map((item, index) => {
      const card = text('div', null, 'entry');
      const head = text('div', null, 'entry-head');
      const remove = text('button', 'Remove', 'link');
      remove.type = 'button';
      remove.setAttribute('aria-label', 'Remove ' + entryTitle(kind, item));
      remove.addEventListener('click', () => { state[kind].splice(index, 1); renderEntries(kind); markDirty(); });
      head.append(text('strong', entryTitle(kind, item)), remove);
      const grid = text('div', null, 'grid two');
      for (const [key, caption, placeholder, wide] of ENTRY_FIELDS[kind]) {
        const label = text('label', caption);
        const multiline = key === 'bullets' || (wide && key !== 'technologies');
        const field = document.createElement(multiline ? 'textarea' : 'input');
        if (multiline) field.rows = key === 'bullets' ? 4 : 2;
        field.maxLength = key === 'bullets' ? 8000 : 250;
        field.dataset.entry = `${kind}.${index}.${key}`;
        field.value = item[key] || '';
        if (placeholder) field.placeholder = placeholder;
        if (wide) label.classList.add('full');
        label.append(field);
        grid.append(label);
      }
      card.append(head, grid);
      return card;
    });
    if (!nodes.length) nodes.push(text('p', kind === 'projects' ? 'No projects added. Projects are optional.' : `No ${kind} added yet.`, 'hint'));
    $(kind + '-entries').replaceChildren(...nodes);
  }
  for (const button of form.querySelectorAll('[data-add]')) {
    button.addEventListener('click', () => {
      const kind = button.dataset.add;
      state[kind].push({...BLANK[kind]});
      renderEntries(kind);
      $(kind + '-entries').lastElementChild.querySelector('input,textarea').focus();
      markDirty();
    });
  }
  function renderExperienceSummary() {
    const years = Core.experienceYears(Core.buildProfile(state).experience);
    const node = $('experience-summary');
    node.replaceChildren(text('span', 'Experience from dated roles: ' + years + (years === 1 ? ' year' : ' years')),
      pill(Core.experienceLevel(years), 'ok'));
  }

  /* ---------- Target role ---------- */
  function renderRoleDetail() {
    const role = roles.find(item => item.id === state.role);
    const node = $('role-detail');
    if (!role) { node.replaceChildren(text('small', 'Choose the role this resume targets. Keywords come from the role catalogue.')); return; }
    const chips = text('div', null, 'chips');
    for (const term of role.keywords) chips.append(text('span', term, 'chip static'));
    const children = [text('small', 'Role keywords used for ATS coverage'), chips];
    if (role.titleVariations?.length) children.push(text('small', 'Common titles: ' + role.titleVariations.join(', ')));
    const groups = App.getCatalog().keywordGroups?.[role.id];
    if (groups) {
      children.push(text('small', 'Must-have terms: ' + groups.priorities.mustHave.join(', ')));
    }
    node.replaceChildren(...children);
  }
  function renderKeywords() {
    const chips = state.keywords.map((term, index) => {
      const chip = text('span', term, 'chip');
      const remove = text('button', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', 'Remove keyword ' + term);
      remove.addEventListener('click', () => { state.keywords.splice(index, 1); renderKeywords(); markDirty(); });
      chip.append(remove);
      return chip;
    });
    $('keyword-chips').replaceChildren(...(chips.length ? chips : [text('small', 'No additional keywords. Role keywords are always included.')]));
  }
  function addKeyword() {
    const input = $('keyword-input');
    state.keywords = Core.addKeywords(state.keywords, input.value.split(','));
    input.value = '';
    renderKeywords();
    markDirty();
  }
  $('add-keyword').addEventListener('click', addKeyword);
  $('keyword-input').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addKeyword(); } });
  $('extract-keywords').addEventListener('click', async () => {
    const feedback = $('jd-feedback');
    if (!state.jobDescription.trim()) { feedback.textContent = 'Paste a job description first.'; return; }
    $('extract-keywords').disabled = true;
    try {
      const result = await App.postJSON('/api/jd-keywords', {text: state.jobDescription});
      const before = state.keywords.length;
      state.keywords = Core.addKeywords(state.keywords, result.keywords);
      feedback.textContent = result.keywords.length ? `Found ${result.keywords.length} known terms; added ${state.keywords.length - before}.` : 'No known role or tool terms found in this text.';
      renderKeywords();
      markDirty();
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      $('extract-keywords').disabled = false;
    }
  });

  /* ---------- Steps ---------- */
  function renderStepper() {
    $('stepper').replaceChildren(...Core.STEPS.map((label, index) => {
      const number = index + 1;
      const item = text('li');
      item.dataset.state = number === step ? 'current' : number < maxStep || number < step ? 'done' : 'todo';
      const button = text('button');
      button.type = 'button';
      button.disabled = number > maxStep;
      if (number === step) button.setAttribute('aria-current', 'step');
      button.append(text('span', number < step ? '✓' : String(number), 'num'), text('span', label, 'label'));
      button.addEventListener('click', () => goTo(number));
      item.append(button);
      return item;
    }));
  }
  function goTo(number) {
    step = number;
    maxStep = Math.max(maxStep, number);
    for (const section of form.querySelectorAll('.step')) section.hidden = Number(section.dataset.step) !== step;
    $('back').disabled = step === 1;
    $('next').hidden = step >= 4;
    $('generate').hidden = step !== 4;
    if (step === 5) { $('next').hidden = true; $('generate').hidden = true; }
    if (step === 2) renderExperienceSummary();
    if (step === 3) renderRoleDetail();
    if (step === 4) renderReview();
    if (step === 5) renderGenerate();
    renderStepper();
    $('wizard-errors').replaceChildren();
  }
  function renderErrors(show) {
    const errors = Core.validate(state, Math.min(step, 4));
    for (const field of form.querySelectorAll('[aria-invalid]')) field.removeAttribute('aria-invalid');
    if (show) {
      for (const error of errors) {
        const target = error.field.includes('.') ? form.querySelector(`[data-entry="${error.field}"]`)
          : form.querySelector(`[data-bind="profile.${error.field}"], [data-bind="${error.field}"]`);
        target?.setAttribute('aria-invalid', 'true');
      }
      $('wizard-errors').replaceChildren(...errors.map(error => text('li', error.message)));
    }
    const all = Core.validate(state, 3);
    $('preview-errors').replaceChildren(...all.slice(0, 6).map(error => text('li', error.message)),
      ...(previewError && !all.length ? [text('li', previewError)] : []));
    return errors;
  }
  $('next').addEventListener('click', () => {
    const errors = renderErrors(true);
    if (errors.length) { $('wizard-errors').firstElementChild?.scrollIntoView({block: 'nearest'}); return; }
    goTo(step + 1);
  });
  $('back').addEventListener('click', () => goTo(Math.max(1, step - 1)));

  /* ---------- Review ---------- */
  const plural = (count, noun) => count + ' ' + noun + (count === 1 ? '' : 's');
  function renderReview() {
    const profile = Core.buildProfile(state);
    const role = roles.find(item => item.id === state.role);
    const facts = [['Candidate', profile.full_name], ['Target role', role?.name || '—'], ['Template', templates[state.template] || state.template],
      ['Experience', Core.experienceYears(profile.experience) + ' years · ' + plural(profile.experience.length, 'role')],
      ['Education', plural(profile.education.length, 'record')], ['Projects', (profile.projects || []).length + ''],
      ['Skills', profile.skills.length + ''], ['Extra keywords', state.keywords.length + '']];
    $('review-summary').replaceChildren(...facts.map(([label, value]) => { const box = text('div'); box.append(text('small', label), text('strong', value)); return box; }));
    const compare = $('source-compare');
    const structured = text('label', 'Structured candidate facts');
    structured.append(text('pre', JSON.stringify(profile, null, 2), 'code'));
    if (state.source) {
      const source = text('label', 'Imported source text');
      source.append(text('pre', state.source, 'code'));
      compare.replaceChildren(source, structured);
    } else {
      const note = text('label', 'Source');
      note.append(text('p', 'Entered manually in the wizard. There is no imported document to compare against, so check each fact with the candidate.'));
      compare.replaceChildren(note, structured);
    }
    renderReviewAts();
  }
  function renderReviewAts() {
    if (step === 4) window.renderReport($('review-ats'), preview?.report);
  }

  /* ---------- Live preview ---------- */
  function renderPaper() {
    const p = Core.buildProfile(state);
    const paper = $('paper');
    const nodes = [text('h3', p.full_name || 'Candidate name', p.full_name ? '' : 'placeholder')];
    if (p.headline) nodes.push(text('p', p.headline, 'headline'));
    const contact = [p.email, p.phone, p.location, p.linkedin, p.github, p.portfolio].filter(Boolean).join(' | ');
    nodes.push(text('p', contact || 'Contact details', 'contact' + (contact ? '' : ' placeholder')));
    const section = title => nodes.push(text('h4', title));
    if (p.summary) { section('PROFESSIONAL SUMMARY'); nodes.push(text('p', p.summary)); }
    if (p.skills.length) { section('TECHNICAL SKILLS'); nodes.push(text('p', 'Skills: ' + p.skills.join(' • '))); }
    if (p.experience.length) {
      section('PROFESSIONAL EXPERIENCE');
      for (const item of p.experience) {
        const row = text('div', null, 'row');
        const title = text('p'); title.append(text('strong', item.position || 'Job title'), document.createTextNode(' | ' + (item.company || 'Employer')));
        row.append(title, text('span', [item.start, item.end].filter(Boolean).join(' – '), 'muted'));
        nodes.push(row);
        if (item.location) nodes.push(text('p', item.location, 'muted'));
        if (item.bullets.length) { const ul = text('ul'); for (const b of item.bullets) ul.append(text('li', b)); nodes.push(ul); }
      }
    }
    if (p.projects?.length) {
      section('PROJECTS');
      for (const item of p.projects) {
        const title = text('p'); title.append(text('strong', item.name || 'Project'), document.createTextNode(item.link ? ' | ' + item.link : ''));
        nodes.push(title);
        if (item.technologies.length) nodes.push(text('p', item.technologies.join(', '), 'muted'));
        if (item.bullets.length) { const ul = text('ul'); for (const b of item.bullets) ul.append(text('li', b)); nodes.push(ul); }
      }
    }
    if (p.education.length) {
      section('EDUCATION');
      for (const item of p.education) {
        const title = text('p'); title.append(text('strong', item.degree || 'Degree'), document.createTextNode(' | ' + (item.institution || 'Institution')));
        nodes.push(title);
        const extra = [[item.start, item.end].filter(Boolean).join(' – '), item.details].filter(Boolean).join(' | ');
        if (extra) nodes.push(text('p', extra, 'muted'));
      }
    }
    if (p.certifications.length) { section('CERTIFICATIONS'); nodes.push(text('p', p.certifications.join(' • '))); }
    if (nodes.length <= 3) nodes.push(text('p', 'Sections appear here as you add verified details.', 'placeholder'));
    paper.replaceChildren(...nodes);
  }
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 450);
  }
  async function runPreview() {
    const blocking = Core.validate(state, 3);
    const seq = ++previewSeq;
    if (blocking.length) {
      preview = null;
      previewError = '';
      $('coverage-score').textContent = '—';
      $('latex-code').textContent = 'Complete the required details to render LaTeX.\n\n' + blocking.map(error => '• ' + error.message).join('\n');
      $('preview-status').textContent = 'Updates as verified details change';
      window.renderReport($('preview-ats'), null);
      renderAtsPage();
      renderReviewAts();
      return;
    }
    $('preview-status').textContent = 'Checking with ResumeOps…';
    try {
      const result = await App.postJSON('/api/preview', Core.buildRequest(state));
      if (seq !== previewSeq) return;
      preview = result;
      previewError = '';
      $('coverage-score').textContent = result.ats.score + '%';
      $('latex-code').textContent = result.latex;
      $('preview-status').textContent = 'Validated by the ResumeOps profile checks';
      window.renderReport($('preview-ats'), result.report);
    } catch (error) {
      if (seq !== previewSeq) return;
      preview = null;
      previewError = error.message;
      $('coverage-score').textContent = '—';
      $('latex-code').textContent = 'The server rejected this draft: ' + error.message;
      $('preview-status').textContent = 'Validation error';
      window.renderReport($('preview-ats'), null);
    }
    renderErrors(false);
    renderAtsPage();
    renderReviewAts();
  }
  function renderAtsPage() {
    const container = $('ats-draft');
    if (preview) window.renderReport(container, preview.report);
    else container.replaceChildren(text('p', 'Complete the wizard through the target role to analyse the current draft.'));
  }

  /* ---------- Tabs ---------- */
  function selectTab(name) {
    for (const tab of document.querySelectorAll('.preview [role=tab]')) tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
    for (const panel of document.querySelectorAll('.preview .tab-panel')) panel.hidden = panel.dataset.panel !== name;
  }
  for (const tab of document.querySelectorAll('.preview [role=tab]')) tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  $('fullscreen').addEventListener('click', () => {
    const panel = document.querySelector('.preview');
    const on = panel.classList.toggle('fullscreen');
    $('fullscreen').textContent = on ? 'Exit full screen' : 'Full screen';
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.querySelector('.preview.fullscreen')) $('fullscreen').click();
  });
  $('copy-latex').addEventListener('click', async () => {
    if (!preview) { $('latex-feedback').textContent = 'Nothing to copy yet.'; return; }
    try {
      await navigator.clipboard.writeText(preview.latex);
      $('latex-feedback').textContent = 'LaTeX copied.';
    } catch {
      const range = document.createRange();
      range.selectNodeContents($('latex-code'));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      $('latex-feedback').textContent = 'Clipboard blocked. The code is selected; press Ctrl+C.';
    }
  });
  $('download-latex').addEventListener('click', () => {
    if (!preview) { $('latex-feedback').textContent = 'Nothing to download yet.'; return; }
    const url = URL.createObjectURL(new Blob([preview.latex], {type: 'application/x-tex'}));
    const a = document.createElement('a');
    a.href = url;
    a.download = (preview.profile.full_name || 'resume').toLowerCase().replace(/[^a-z0-9]+/g, '-') + '-' + state.role + '.tex';
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('latex-feedback').textContent = 'Downloaded ' + a.download;
  });

  /* ---------- Generate ---------- */
  $('generate').addEventListener('click', async () => {
    if (!state.verified) {
      $('wizard-errors').replaceChildren(text('li', 'Confirm the candidate facts are accurate before generating.'));
      return;
    }
    const errors = Core.validate(state, 5);
    if (errors.length) { $('wizard-errors').replaceChildren(...errors.map(error => text('li', error.message))); return; }
    $('generate').disabled = true;
    try {
      const result = await App.queueCandidate(JSON.stringify(Core.buildRequest(state)));
      queued = {name: result.name, created: result.created};
      dirty = false;
      $('draft-state').textContent = 'Queued';
      goTo(5);
      App.refresh();
    } catch (error) {
      $('wizard-errors').replaceChildren(text('li', 'Blocked: ' + error.message));
    } finally {
      $('generate').disabled = false;
    }
  });
  function renderGenerate() {
    const snapshot = App.getSnapshot();
    const job = queued && snapshot ? snapshot.jobs.find(item => item.name === queued.name) : null;
    const progress = queued ? (job ? Core.jobProgress(job) : {percent: 0, label: 'Queued · waiting for the watcher', state: 'queued'})
      : {percent: 0, label: 'Not queued yet. Go back to Review & ATS and choose Generate resume.', state: 'idle'};
    $('gen-candidate').textContent = job?.candidate || Core.buildProfile(state).full_name || 'Candidate';
    $('gen-percent').textContent = progress.percent + '%';
    setBar($('gen-bar'), progress.percent, progress.state);
    if (queued && !dirty) $('draft-state').textContent = progress.state === 'ok' ? 'Generated' : progress.state === 'failed' ? 'Awaiting retry' : 'Queued';
    $('gen-stage').textContent = (queued && !queued.created && !job ? 'This exact revision was already queued. ' : '') + progress.label;
    const current = Core.stageIndex(progress.percent);
    $('gen-timeline').replaceChildren(...Core.PIPELINE.map(([value, label], index) => {
      const item = text('li', value + '% · ' + label);
      item.dataset.state = index <= current ? (progress.percent >= 100 || index < current ? 'done' : 'current') : 'todo';
      return item;
    }));
    const outputs = $('gen-outputs');
    const revision = job?.revision && snapshot.revisions.find(item => item.key === job.revision);
    if (revision) {
      const links = [];
      if (revision.url) links.push(link('Open Overleaf project', revision.url));
      for (const name of revision.files) links.push(link((name.endsWith('.pdf') ? 'Download ' : '') + (artifactLabels[name] || name), artifactUrl(revision.key, name)));
      outputs.replaceChildren(...links);
      showPdf(revision);
    } else {
      outputs.replaceChildren();
    }
  }
  let shownPdf = '';
  function showPdf(revision) {
    if (shownPdf === revision.key) return;
    shownPdf = revision.key;
    const frame = document.createElement('iframe');
    frame.className = 'pdf-frame';
    frame.title = 'Verified PDF for ' + revision.candidate;
    frame.src = artifactUrl(revision.key, 'resume.pdf');
    const links = text('div', null, 'links');
    links.append(link('Download PDF', artifactUrl(revision.key, 'resume.pdf')));
    if (revision.url) links.append(link('Open Overleaf project', revision.url));
    if (revision.sha256) links.append(text('small', 'Overleaf PDF SHA-256 ' + revision.sha256, 'sha'));
    $('pdf-panel').replaceChildren(links, frame);
    selectTab('pdf');
  }
  window.addEventListener('status', () => { if (step === 5) renderGenerate(); });

  /* ---------- Lifecycle ---------- */
  function load(newState) {
    state = newState;
    queued = null;
    shownPdf = '';
    preview = null;
    maxStep = 1;
    $('pdf-panel').replaceChildren(text('p', 'The verified PDF appears here after generation.', 'empty'));
    $('draft-state').textContent = 'Draft · Unsaved';
    syncInputs();
    for (const kind of ['experience', 'education', 'projects']) renderEntries(kind);
    renderKeywords();
    renderRoleDetail();
    renderPaper();
    selectTab('resume');
    goTo(1);
    schedulePreview();
  }
  $('new-draft').addEventListener('click', () => {
    if (dirty && !confirm('Discard the current draft?')) return;
    load(Core.emptyState());
    dirty = false;
  });
  window.addEventListener('load-draft', event => {
    load(Core.stateFromProfile(event.detail.profile, event.detail.source));
    maxStep = 4;
    renderStepper();
  });
  window.addEventListener('use-template', event => { state.template = event.detail; syncInputs(); markDirty(); });
  window.addEventListener('catalog', event => {
    roles = event.detail.roles;
    templates = event.detail.templates;
    $('role-select').replaceChildren(new Option('Select a role', ''), ...roles.map(role => new Option(role.name, role.id)));
    $('template-select').replaceChildren(...Object.entries(templates).map(([key, name]) => new Option(name, key)));
    syncInputs();
    renderRoleDetail();
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  load(Core.emptyState());
  renderAtsPage();
}());

'use strict';

const ai = id => document.getElementById(id);
const basic = [
  ['role','Target role'], ['years_of_experience','Total years of experience','number'],
  ['full_name','Full name'], ['email','Email','email'], ['phone','Phone','tel'],
  ['location','Location'], ['current_company','Current company'], ['linkedin','LinkedIn','url'],
  ['github','GitHub','url'], ['portfolio','Portfolio URL','url']
];
const collections = [
  ['work_experience','Work experience',[['company','Employer'],['position','Designation'],['start','Start date'],['end','End date'],['location','Location']]],
  ['education','Education',[['degree','Degree'],['specialization','Specialization'],['university','University'],['graduation_year','Graduation year'],['cgpa','CGPA']]],
  ['projects','Projects',[['name','Completed project name']]],
  ['coding_platforms','Coding platforms',[['platform','Platform'],['url','Platform URL','url']]]
];
const state = {};
const panels = ['ai-intake','ai-review','ai-result'];
async function claimEditor() {
  return new Promise((resolve,reject) => {
    navigator.locks.request('resumeops-ai-editor:' + ResumeAuth.owner, {ifAvailable:true}, lock => {
      resolve(Boolean(lock));
      if (lock) return new Promise(release => {
        window.addEventListener('pagehide',release,{once:true});
      });
    }).catch(reject);
  });
}
let active = false;
const pause = () => new Promise(resolve => setTimeout(resolve, 1200));
const newId = () => crypto.randomUUID().replaceAll('-','');
function showProgress(title, message, kind = 'working', metrics = null, progress = null) {
  const panel = ai('ai-progress');
  panel.hidden = false;
  panel.dataset.progressState = kind;
  ai('ai-progress-title').textContent = title;
  // Keep screen-reader updates stable while frequent Ollama stream metrics arrive.
  if (ai('ai-status').textContent !== message) ai('ai-status').textContent = message;
  const reported = progress && Number.isFinite(progress.percent) ? progress.percent : 0;
  const percent = kind === 'complete' ? 100 : Math.max(0, Math.min(99, reported));
  const ring = ai('ai-progress-ring');
  ring.setAttribute('aria-valuenow', String(percent));
  ai('ai-progress-value').textContent = percent + '%';
  ring.querySelector('.ai-ring-fill').style.strokeDashoffset = String(175.93 * (1 - percent / 100));
  const completed = kind === 'complete' ? 5 : progress?.steps_completed ?? 0;
  const total = progress?.steps_total ?? 5;
  const fields = progress?.fields_emitted ?? 0;
  const fieldTotal = progress?.fields_total ?? 0;
  const stepText = kind === 'complete' ? '5 of 5 stages complete' :
    'Step ' + Math.min(total, completed + 1) + ' of ' + total +
    ' · ' + completed + ' completed' +
    (fields && fieldTotal ? ' · ' + fields + '/' + fieldTotal + ' AI fields emitted' : '');
  ai('ai-progress-steps').textContent = kind === 'info' ? '' :
    stepText + ' · Stage-based progress, not time remaining';
  const details = [];
  if (metrics && typeof metrics === 'object') {
    if (Number.isFinite(metrics.chars) && metrics.chars > 0)
      details.push(metrics.chars.toLocaleString() + ' characters generated');
    // Server "tokens" are Ollama stream chunks, not guaranteed tokenizer tokens.
    if (Number.isFinite(metrics.tokens) && metrics.tokens > 0)
      details.push(metrics.tokens.toLocaleString() + ' stream chunks');
    if (Number.isFinite(metrics.elapsed) && metrics.elapsed >= 0)
      details.push(Math.floor(metrics.elapsed) + 's elapsed');
  }
  ai('ai-progress-metrics').textContent = details.join(' · ');
}
const status = message => showProgress('Resume builder', message, 'info');
function updateProgress(record, title) {
  const stage = record.status === 'queued' ? 'Queued for the processing worker' :
    record.stage || 'Processing resume';
  showProgress(title, stage, 'working', record.metrics, record.progress);
}

function node(tag,text) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  return element;
}
function button(text, action) {
  const element = node('button',text);
  element.type='button';
  element.className='secondary';
  element.addEventListener('click',action);
  return element;
}
function control([key, label, type], value = '') {
  const wrapper = node('label',label);
  const input = node('input');
  input.name = key;
  input.type = type || 'text';
  if (key === 'role') { input.setAttribute('list','ai-roles'); input.maxLength=100; }
  input.value = value;
  if (key === 'years_of_experience') {
    input.min='0'; input.max='60'; input.step='0.5';
  }
  input.required = ['role','years_of_experience','full_name','email'].includes(key);
  wrapper.append(input);
  return wrapper;
}
function disclosure(title, id, open = false) {
  const detail = node('details');
  detail.id = id;
  detail.className = 'ai-disclosure';
  detail.open = open;
  detail.append(node('summary', title));
  return detail;
}
function updateCounts() {
  const optional = ai('ai-contact-summary');
  if (optional) {
    const count = basic.slice(4).filter(([key]) =>
      ai('ai-basics').querySelector('[name=' + key + ']')?.value.trim()).length;
    optional.textContent = count ? ' · ' + count + ' added' : ' · optional';
  }
  for (const [key,title] of collections) {
    const count = ai('ai-' + key)?.children.length || 0;
    const summary = ai('ai-section-' + key)?.querySelector('summary');
    if (summary) summary.textContent = title + ' (' + count + ')';
  }
}
function updateReviewCounts() {
  for (const details of ai('ai-claims').querySelectorAll('details[data-section]')) {
    const all = details.querySelectorAll('[data-claim]');
    const checked = [...all].filter(item => item.querySelector('input[type=checkbox]').checked).length;
    details.querySelector('summary').textContent =
      details.dataset.section + ' · ' + checked + '/' + all.length + ' selected';
  }
}
function addEntry(key, fields, value = {}) {
  const container = ai('ai-' + key);
  if (container.children.length >= 40) return;
  const entry = node('div');
  entry.className = 'ai-entry';
  const group = node('div');
  group.className = 'ai-fields';
  for (const field of fields) group.append(control(field,value[field[0]] || ''));
  entry.append(group,button('Remove entry',() => {
    entry.remove(); updateCounts(); save();
  }));
  container.append(entry);
  updateCounts();
}
function intake() {
  const data = Object.fromEntries(basic.map(([key]) => [key,ai('ai-basics').querySelector('[name='+key+']').value]));
  data.years_of_experience = data.years_of_experience === '' ? null : Number(data.years_of_experience);
  for (const [key] of collections) {
    data[key] = [...ai('ai-' + key).children].map(entry => Object.fromEntries(
      [...entry.querySelectorAll('input')].map(input => [input.name,input.value])));
  }
  return data;
}
function fill(data) {
  for (const [key] of basic) ai('ai-basics').querySelector('[name='+key+']').value = data[key] ?? '';
  for (const [key,,fields] of collections) {
    ai('ai-' + key).replaceChildren();
    for (const item of data[key] || []) addEntry(key,fields,item);
  }
  updateCounts();
}
async function save() {
  if (!ResumeAuth.owner) return;
  state.intake = intake();
  await ResumeRecovery.put('ai-builder',state);
}
async function api(path, input) {
  const response = await ResumeAuth.call(path,input ? {method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(input)} : {});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function handle(action) {
  return async event => {
    event?.preventDefault();
    if (active) return;
    active = true;
    for (const panel of panels) ai(panel).inert = true;
    try { await action(); } catch(error) {showProgress('Unable to complete', error.message, 'error');}
    finally {
      for (const panel of panels) ai(panel).inert = false;
      active = false;
    }
  };
}
async function poll(id, title = 'Processing resume') {
  for (;;) {
    const draft = await api('/api/import-progress?id=' + id);
    if (draft.status === 'complete') return draft;
    if (draft.status === 'error') throw new Error(draft.error || 'Could not complete the draft.');
    if (draft.status === 'unknown') throw new Error('The saved operation is unavailable. Start it again.');
    updateProgress(draft, title);
    await pause();
  }
}
// "B.Tech, Computer Science", "Bachelor of Technology in ECE", "M.Tech (Data Science)" and
// "B.E. - IT" become separate Degree and Branch/Specialization fields. Text is only split, never added.
function splitDegree(text, details) {
  const value = text.trim();
  const parts = /^(.+?)\s*(?:,|:|\s[-–]\s|\s+in\s+|\()\s*([^()]+?)\)?$/i.exec(value);
  if (parts && parts[1].split(/\s+/).length <= 6 && !/^hon(?:s|ours)?\.?$/i.test(parts[2].trim())) {
    return {degree:parts[1].trim(), specialization:parts[2].trim()};
  }
  // "B.Sc. Statistics", "MBA Finance": a known degree abbreviation followed by the branch.
  const prefixed = /^((?:[BM]\.?\s?(?:Tech|E|Sc|S|Com|A|Arch|Pharm|Ed|Des)\.?|BCA|MCA|BBA|MBA|PGDM|Ph\.?\s?D\.?|Diploma))\s+([A-Z][A-Za-z&/.' ]{1,79})$/.exec(value);
  if (prefixed) return {degree:prefixed[1], specialization:prefixed[2].trim()};
  // Fall back to a details line that is only a branch name, never a CGPA or other text.
  const branch = /^[A-Za-z&/.' ]{2,80}$/.test(details.trim()) && details.trim().split(/\s+/).length <= 7 ? details.trim() : '';
  return {degree:value, specialization:branch};
}
async function importFile() {
  const job = state.import;
  showProgress('Importing resume', 'Uploading document...', 'working');
  if (job.file) {
    const response = await ResumeAuth.call('/api/draft',{method:'POST',body:job.file,headers:{
      'X-Resume-Extension':job.file.name.toLowerCase().match(/\.(pdf|docx|txt)$/)?.[0] || '',
      'X-Resume-Async':'1','X-Resume-Import-ID':job.id}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    delete job.file;
    await save();
  }
  const {profile} = await poll(job.id, 'Importing resume');
  const next = Object.fromEntries(basic.map(([key]) => [key,profile[key] || '']));
  next.role = intake().role;
  next.work_experience = (profile.experience || []).map(item => Object.fromEntries(
    collections[0][2].map(([field]) => [field,item[field] || ''])));
  next.education = (profile.education || []).map(item => ({...splitDegree(item.degree || '',item.details || ''),
    // A resume line such as "B.Tech, Example University, 2022" is often extracted as a start date only.
    university:item.institution, graduation_year:/\b\d{4}\b/.exec(item.end || item.start || '')?.[0] || ''}));
  next.projects = (profile.projects || []).map(name => ({name}));
  fill(next);
  state.importId = job.id;
  delete state.import;
  await save();
  ai('ai-upload-options').open = false;
  showProgress('Resume import complete', 'Details imported. Check the fields before drafting.', 'complete');
}
function renderReview(draft) {
  ai('ai-intake').hidden = true;
  ai('ai-review').hidden = false;
  ai('ai-claims').replaceChildren();
  const claimGroups = new Map();
  for (const claim of draft.claims) {
    if (!claimGroups.has(claim.section)) {
      const section = disclosure(claim.section, 'ai-review-section-' + claimGroups.size,
                                 claimGroups.size === 0);
      section.dataset.section = claim.section;
      ai('ai-claims').append(section);
      claimGroups.set(claim.section, section);
    }
    const row = node('div');
    row.className='ai-claim';
    row.dataset.claim=claim.id;
    const sourceEntry = claim.section === 'Responsibility' ? draft.candidate.work_experience[claim.index]?.company :
      claim.section === 'Project detail' ? draft.candidate.projects[claim.index]?.name : '';
    row.dataset.provenance=claim.provenance;
    const provenance = {
      verified_from_source:'verified from supplied source',
      unsupported_role_keyword:'role keyword not found in supplied evidence',
      unverified_suggestion:'unverified AI suggestion'
    }[claim.provenance];
    const label = node('label',claim.section + (sourceEntry ? ' - ' + sourceEntry : '') + ' (' + provenance + ')');
    const check = node('input');
    check.type='checkbox';
    check.checked=Object.hasOwn(state.approval?.claims || {},claim.id);
    label.prepend(check);
    const text = node('textarea');
    text.rows=2; text.value=state.edits?.[claim.id] ?? state.approval?.claims?.[claim.id] ?? claim.text;
    text.setAttribute('aria-label','Edit ' + claim.section.toLowerCase());
    row.append(label,node('p',claim.reason),text);
    claimGroups.get(claim.section).append(row);
  }
  updateReviewCounts();
  ai('ai-attest').checked=state.approval?.attested === true;
  const suggestions = draft.suggestions;
  ai('ai-ideas').replaceChildren(node('h3','Portfolio ideas (not completed)'),
    ...suggestions.project_ideas.map(item => node('p',item.title + ': ' + item.description)),
    node('h3','Learning recommendations (not earned)'),
    node('p',suggestions.certification_recommendations.items.join(', ')));
  ai('ai-sources').replaceChildren(node('h3','Role research'));
  for (const source of draft.intelligence.sources) {
    const label = node(source.url ? 'a' : 'span',source.label);
    if (source.url) { label.href=source.url; label.rel='noopener noreferrer'; label.target='_blank'; }
    const line = node('p');
    line.append(label,document.createTextNode(source.terms.length ? ': ' + source.terms.join(', ') : ''));
    if (source.provider) line.append(document.createTextNode(
      ' (' + source.provider + ', retrieved ' + source.retrieved_at.slice(0,10) + ', title relevance ' + source.relevance + '%)'));
    ai('ai-sources').append(line);
    if (source.responsibilities?.length) {
      const details=node('details');
      details.append(node('summary','Requirements and responsibilities'));
      const list=node('ul');
      for (const item of source.responsibilities) list.append(node('li',item));
      details.append(list);
      ai('ai-sources').append(details);
    }
  }
}
async function draftContent() {
  const draft = state.draft;
  showProgress('Drafting resume', 'Starting Ollama...', 'working');
  await api('/api/ai-draft',draft);
  renderReview(await poll(draft.id, 'Drafting resume'));
  showProgress('AI draft complete', 'Review your AI suggestions.', 'complete');
  ai('ai-review').scrollIntoView({block:'start'});
}
function selected() {
  const claims = {};
  const edits = {};
  for (const row of ai('ai-claims').querySelectorAll('[data-claim]')) {
    const text = row.querySelector('textarea').value;
    edits[row.dataset.claim] = text;
    if (row.querySelector('input').checked) claims[row.dataset.claim] = text;
  }
  state.edits = edits;
  updateReviewCounts();
  return {attested:ai('ai-attest').checked,claims};
}
async function preview() {
  const {profile,ats,quality} = await api('/api/ai-review',{id:state.draft.id,approval:state.approval});
  const view=ai('ai-preview-content');
  view.replaceChildren(node('h3',profile.full_name),
    node('p',[profile.email,profile.phone,profile.location,profile.linkedin,profile.github,profile.portfolio].filter(Boolean).join(' | ')));
  if (profile.current_company) view.append(node('p','Current company: ' + profile.current_company));
  const score = node('p','ATS readiness: ' + ats.ats_readiness + '/100');
  score.className = 'ai-score';
  view.append(score);
  function section(title, entries) {
    if (!entries.length) return;
    const group=disclosure(title + ' (' + entries.length + ')', 'ai-preview-section-' +
      view.querySelectorAll('details').length, title === 'Professional summary');
    const body = node('div');
    body.className = 'ai-preview-items';
    body.append(...entries);
    group.append(body);
    view.append(group);
  }
  section('Professional summary',profile.summary ? [node('p',profile.summary)] : []);
  section('Technical skills',profile.skills.map(text => node('p',text)));
  section('Core competencies',(profile.competencies || []).map(text => node('p',text)));
  section('Work experience',profile.experience.flatMap(item => [
    node('h5',item.position + ' - ' + item.company),
    node('p',[item.start,item.end,item.location].filter(Boolean).join(' | ')),
    ...item.bullets.map(text => node('p',text))]));
  section('Education',profile.education.map(item => node('p',
    [item.degree,item.institution,item.end,item.details].filter(Boolean).join(' | '))));
  for (const [key,title] of [['projects','Projects'],['achievements','Achievements'],
    ['certifications','Certifications'],['coding_platforms','Coding platforms']]) {
    section(title,(profile[key] || []).map(text => node('p',text)));
  }
  section('ATS analysis', [node('p','Matched: ' + ats.matched.join(', ')),
    node('p','Missing: ' + ats.missing.join(', ')),
    ...ats.breakdown.map(part => node('p',part.label + ': ' + part.points + '/' + part.max)),
    ...ats.suggestions.map(tip => node('p',tip))]);
  if (quality) {
    section('Review checks', [
      node('p','Target role: ' + quality.role_title),
      node('p',quality.verified_claims + ' candidate-approved statements.'),
      node('p',quality.role_keywords_available ? quality.keyword_matches + ' relevant keyword matches.' :
        'No sourced or curated keyword list is available for this title.'),
      node('p',quality.experience_dates_support_level ? 'Selected experience level passes the date check.' :
        'Check the selected experience level against the supplied employment dates.')]);
  }
  ai('ai-review').hidden=true;
  ai('ai-result').hidden=false;
  status('Review the preview before generating.');
  ai('ai-result').scrollIntoView({block:'start'});
}
async function queue() {
  const result = await api('/api/ai-queue',state.queuing);
  await ResumeRecovery.put('job',{name:result.name,queued:Date.now()});
  await ResumeRecovery.remove('ai-builder');
  location.assign('/workflow');
}
async function init() {
  await ResumeAuth.ready();
  if (!await claimEditor()) {
    ai('ai-intake').hidden = true;
    status('The AI builder is open in another tab. Close it and reload to continue here.');
    return;
  }
  ResumeAuth.onBeforeRenewal(save);
  for (const field of basic.slice(0,4)) ai('ai-basics').append(control(field));
  const contact = disclosure('Contact and profiles', 'ai-contact');
  const summary = contact.querySelector('summary');
  const count = node('span');
  count.id = 'ai-contact-summary';
  summary.append(count);
  const contactFields = node('div');
  contactFields.className = 'ai-fields ai-optional-fields';
  for (const field of basic.slice(4)) contactFields.append(control(field));
  contact.append(contactFields);
  ai('ai-basics').append(contact);
  for (const [key,title,fields] of collections) {
    const section=disclosure(title, 'ai-section-' + key);
    section.classList.add('ai-collection');
    const list=node('div'); list.id='ai-'+key;
    section.append(list,button('Add '+title.toLowerCase(),() => {
      section.open = true;
      addEntry(key,fields);
    }));
    ai('ai-lists').append(section);
  }
  updateCounts();
  const roles = node('datalist');
  roles.id='ai-roles';
  ai('ai-basics').append(roles);
  const catalog = await api('/api/catalog');
  roles.replaceChildren(...catalog.roles.map(role => new Option(role.name,role.name)));
  Object.assign(state,await ResumeRecovery.read('ai-builder'));
  if (state.intake) fill(state.intake);
  if (state.queuing === true) state.queuing={id:state.draft.id,approval:state.approval};
  ai('ai-reset').addEventListener('click',handle(async () => {
    if (!window.confirm('Clear the AI builder draft saved in this browser? Completed resumes and History will remain.')) return;
    await ResumeRecovery.remove('ai-builder');
    for (const key of Object.keys(state)) delete state[key];
    fill({});
    ai('ai-file').value = '';
    ai('ai-intake').hidden = false;
    ai('ai-review').hidden = true;
    ai('ai-result').hidden = true;
    status('Saved draft cleared in this browser. Completed resumes remain in History.');
  }));
  ai('ai-form').addEventListener('input',() => {
    updateCounts(); save().catch(error => status(error.message));
  });
  ai('ai-form').addEventListener('invalid', event => {
    const section = event.target.closest('details:not([open])');
    if (!section) return;
    section.open = true;
    // Let the browser focus the newly visible control during native validation.
    event.target.getBoundingClientRect();
  }, true);
  ai('ai-form').addEventListener('submit',handle(async () => {
    state.draft={id:newId(),candidate:intake(),import_id:state.importId};
    delete state.approval;
    delete state.edits;
    await save();
    await draftContent();
  }));
  ai('ai-import').addEventListener('click',handle(async () => {
    const file=ai('ai-file').files[0];
    if (!file || !file.size || file.size>12*1024*1024 || !/\.(pdf|docx|txt)$/i.test(file.name)) {
      throw new Error('Choose a PDF, DOCX or TXT file up to 12 MB.');
    }
    state.import={id:newId(),file};
    await save();
    await importFile();
  }));
  ai('ai-review').addEventListener('input',() => {
    state.approval=selected();
    ai('ai-result').hidden=true;
    save().catch(error => status(error.message));
  });
  ai('ai-preview').addEventListener('click',handle(async () => {
    state.approval=selected(); await save(); await preview();
  }));
  ai('ai-edit-review').addEventListener('click',() => {
    ai('ai-result').hidden = true;
    ai('ai-review').hidden = false;
    ai('ai-review').scrollIntoView({block:'start'});
  });
  ai('ai-generate').addEventListener('click',handle(async () => {
    state.queuing={id:state.draft.id,approval:structuredClone(state.approval)};
    await save();
    await queue();
  }));
  ai('ai-back').addEventListener('click',() => {
    delete state.draft; delete state.approval; delete state.edits;
    ai('ai-intake').hidden=false; ai('ai-review').hidden=true; ai('ai-result').hidden=true;
    save().catch(error => status(error.message));
  });
  await handle(async () => {
    if (state.queuing) await queue();
    else if (state.import) await importFile();
    else if (state.draft) await draftContent();
  })();
}
init().catch(error => showProgress('Builder unavailable', error.message, 'error'));

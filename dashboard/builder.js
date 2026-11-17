'use strict';
const el = id => document.getElementById(id);
const form = el('candidate');
const fields = form.elements;
const steps = [...form.querySelectorAll('[data-step]')];
const token = document.querySelector('meta[name=dashboard-token]').content;
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
let activeStep = 0;
let roles = [];
let templates = {};
let watchedJob = '';
let lastJob = null;
let serverOffset = 0;            // server clock minus browser clock, from /api/status
const shownPercent = {};         // highest percentage shown per job attempt (never decreases)
const ACTIVE_KEY = 'resumeops.activeJob';
function rememberJob(name, submitted) {
  try { localStorage.setItem(ACTIVE_KEY, JSON.stringify({name, submitted})); } catch {}
}
function storedJob() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_KEY) || 'null'); } catch { return null; }
}
function forgetJob() {
  try { localStorage.removeItem(ACTIVE_KEY); } catch {}
}
let shownRevision = '';
let revisions = [];
let historySnapshot = '';
let refreshPending = false;
let selection = 0;

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

function experienceLevel() {
  const years = Number(fields.years.value);
  const level = years < 2 ? 'Fresher / Junior' : years < 5 ? 'Mid-level' : years < 10 ? 'Senior' : 'Lead / Principal';
  return (years === 25 ? '25+' : years) + ' years · ' + level;
}

function updateExperience() {
  el('experience-level').value = experienceLevel();
}
fields.years.addEventListener('input', updateExperience);

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

const entryFields = {
  experience: [['company','Employer'],['position','Position'],['start','Start date'],
    ['end','End date'],['location','Location'],['bullets','Verified work contributions']],
  education: [['institution','Institution'],['degree','Degree'],['start','Education start'],
    ['end','Education end'],['details','Education details']],
};

const mobileLayout = window.matchMedia('(max-width:760px)');

function wrapEntry(entry, open) {
  // Phone-only collapsible shell around an entry; desktop keeps the bare fieldset.
  if (entry.parentElement?.classList.contains('resume-entry')) return;
  const details = document.createElement('details');
  details.className = 'resume-entry';
  details.open = open;
  details.append(entry.entryHeading);
  entry.replaceWith(details);
  details.append(entry);
}
function unwrapEntry(entry) {
  const details = entry.parentElement;
  if (details?.classList.contains('resume-entry')) details.replaceWith(entry);
}

function addEntry(kind, focus = true) {
  const holder = el(kind+'-entries');
  const heading = rows('summary', (kind === 'experience' ? 'Experience' : 'Education') +
    ' ' + (holder.children.length + 1));
  const entry = document.createElement('fieldset');
  entry.entryHeading = heading;
  entry.append(rows('legend',kind === 'experience' ? 'Work entry' : 'Education entry'));
  const grid = rows('div','','fields');
  for (const [key,label] of entryFields[kind]) {
    const wrapper = rows('label',label);
    const input = document.createElement(key === 'bullets' ? 'textarea' : 'input');
    input.dataset.field = key;
    input.maxLength = key === 'bullets' ? 8000 : 250;
    input.required = (kind === 'experience' ? ['company','position','start'] : ['institution','degree']).includes(key);
    if (key === 'company' && !el('experience-entries').children.length) input.value = fields.current_company.value;
    if (key === 'bullets') input.placeholder = 'One contribution per line';
    wrapper.append(input);
    grid.append(wrapper);
  }
  const remove = rows('button','Remove entry','secondary');
  remove.type='button';
  remove.addEventListener('click', () => (entry.closest('.resume-entry') || entry).remove());
  entry.append(grid,remove);
  const title = () => {
    const name = grid.querySelector(kind === 'experience' ? '[data-field=company]' :
      '[data-field=institution]').value.trim();
    const title = grid.querySelector(kind === 'experience' ? '[data-field=position]' :
      '[data-field=degree]').value.trim();
    heading.textContent = [kind === 'experience' ? 'Experience' : 'Education', name, title]
      .filter(Boolean).join(' · ');
  };
  grid.addEventListener('input', title);
  holder.append(entry);
  if (mobileLayout.matches) wrapEntry(entry, focus);
  if (focus) grid.querySelector('input').focus();
}
el('add-experience').addEventListener('click', () => addEntry('experience'));
el('add-education').addEventListener('click', () => addEntry('education'));

function entryData(kind) {
  return [...el(kind+'-entries').querySelectorAll('fieldset')].map(entry =>
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

function showStep(index, focus = true) {
  activeStep=index;
  document.querySelector('.editor').dataset.activeStep = String(index);
  steps.forEach((step,i) => {step.hidden=i!==index;});
  [...el('steps').children].forEach((step,i) => {
    if (i===index) step.setAttribute('aria-current','step');
    else step.removeAttribute('aria-current');
  });
  el('back').hidden=index===0;
  el('next').hidden=index===3;
  el('generate').hidden=index!==3;
  if (focus) {
    if (mobileLayout.matches) {
      steps[index].querySelector('h2').focus({preventScroll:true});
      el('steps').scrollIntoView({block:'start',behavior:'auto'});
    } else {
      steps[index].querySelector('h2').focus();
    }
  }
}

// Optional fields are grouped into collapsed sections on phones only. Each moved label leaves a
// marker so the exact desktop markup is restored when the viewport widens (e.g. rotation).
function optionalGroup(id, summary, names, after) {
  const details = document.createElement('details');
  details.id = id;
  details.className = 'optional-details';
  const grid = rows('div','','fields');
  details.append(rows('summary', summary), grid);
  const labels = names.map(name => fields[name].closest('label'));
  return {
    collapse() {
      if (details.isConnected) return;
      for (const label of labels) {
        const marker = document.createComment(id);
        label.markerNode = marker;
        label.replaceWith(marker);
        grid.append(label);
      }
      details.open = false;
      after().after(details);
    },
    expand() {
      if (!details.isConnected) return;
      for (const label of labels) label.markerNode.replaceWith(label);
      details.remove();
    },
  };
}
const optionalGroups = [
  optionalGroup('more-personal', 'More details (company and links)',
    ['current_company','linkedin','github','portfolio'], () => fields.full_name.closest('.fields')),
  optionalGroup('more-experience', 'More details (projects and certifications)',
    ['certifications','projects'], () => fields.skills.closest('.fields')),
];
function applyLayout() {
  for (const group of optionalGroups) mobileLayout.matches ? group.collapse() : group.expand();
  for (const entry of document.querySelectorAll('#experience-entries fieldset, #education-entries fieldset')) {
    mobileLayout.matches ? wrapEntry(entry, false) : unwrapEntry(entry);
  }
  if (!mobileLayout.matches) expandHistory(true);
}
mobileLayout.addEventListener('change', applyLayout);
const historyToggle=el('history-toggle');
function expandHistory(expanded) {
  el('history-content').classList.toggle('expanded',expanded);
  el('history').classList.toggle('history-open',expanded);
  historyToggle.setAttribute('aria-expanded',String(expanded));
  historyToggle.textContent=expanded ? 'Hide history' : 'Show history';
}
historyToggle.addEventListener('click', () => expandHistory(historyToggle.getAttribute('aria-expanded')!=='true'));
document.querySelector('a[href="#history"]').addEventListener('click', () => expandHistory(true));
applyLayout();
if (mobileLayout.matches) expandHistory(false);
showStep(0,false);
updateExperience();
// Validation must reveal required inputs inside collapsed mobile experience/education entries.
// A native reportValidity() call on a closed <details> input can silently fail on iOS Safari.
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
  const fieldset = input.closest('fieldset');
  if (!fieldset) return name;
  const kind = fieldset.closest('#experience-entries') ? 'Experience' :
    fieldset.closest('#education-entries') ? 'Education' : '';
  if (!kind) return name;
  const entries = [...el(kind.toLowerCase() + '-entries').querySelectorAll('fieldset')];
  const index = entries.indexOf(fieldset);
  return kind + ' ' + (index + 1) + ' — ' + name;
}
function revealInvalidField(input) {
  // Open every collapsed ancestor before focus/reportValidity; keep all form values unchanged.
  for (let node = input.parentElement; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
  }
  const message = 'Please complete ' + fieldDescription(input) + ' before continuing.';
  const heading = steps[activeStep].querySelector('h2');
  heading.after(validationNotice);
  validationNotice.textContent = message;
  validationNotice.hidden = false;
  input.setAttribute('aria-invalid', 'true');
  feedback(message, true);
  input.focus({preventScroll:true});
  input.scrollIntoView({block:'center', behavior:'auto'});
  input.reportValidity();
}
form.addEventListener('input', event => {
  const input = event.target;
  if (input.matches('input,textarea,select') && input.checkValidity()) {
    input.removeAttribute('aria-invalid');
    if (validationNotice.textContent.includes(fieldDescription(input))) {
      validationNotice.hidden = true;
      feedback('');
    }
  }
});
el('back').addEventListener('click', () => {clearValidation();feedback('');showStep(activeStep-1);});
el('next').addEventListener('click',async () => {
  clearValidation();
  feedback('');
  const invalid=[...steps[activeStep].querySelectorAll('input,select,textarea')].find(input => !input.checkValidity());
  if (invalid) {revealInvalidField(invalid);return;}
  if (activeStep!==2) {showStep(activeStep+1);return;}
  const button=el('next');
  button.disabled=true;
  try {
    const {profile,ats}=await request('/api/preview',payload());
    const role=roles.find(item => item.id===ats.role);
    el('review-summary').replaceChildren(
      rows('p',[role?.name, templates[ats.template], experienceLevel(), fields.current_company.value].filter(Boolean).join(' · ')),
      rows('p',profileText(profile),'review-line'),
      rows('p','Preflight ATS readiness '+ats.ats_readiness+'/100 · Keyword coverage '+ats.score+'%'));
    fields.verified.checked=false;
    showStep(3);
  } catch (error) {
    feedback(error.message,true);
  } finally {
    button.disabled=false;
  }
});

form.addEventListener('submit',async event => {
  event.preventDefault();
  if(activeStep!==3) return;
  if(!fields.verified.checked){feedback('Confirm the candidate facts before generating.',true);return;}
  const button=el('generate');
  button.disabled=true;
  try {
    const submitted=payload();
    const result=await request('/api/queue',submitted);
    watchedJob=result.name;
    rememberJob(result.name, JSON.stringify(submitted));
    el('result-success').hidden=true;
    // The previous candidate's result stays in history; hide it so it is not mistaken for this job.
    ++selection;
    shownRevision='';
    el('results').hidden=true;
    el('pdf-preview').removeAttribute('src');
    el('generation').hidden=false;
    if (mobileLayout.matches) el('generation').scrollIntoView({block:'start'});
    el('job-progress').value=0;
    el('job-stage').textContent='Waiting for the watcher';
    feedback(result.created ? 'Resume queued.' : 'This revision already exists.');
    refresh();
  } catch(error) {
    feedback(error.message,true);
  } finally {
    button.disabled=false;
  }
});

function openTab(id, focus = false) {
  for (const tab of document.querySelectorAll('[role=tab]')) {
    const selected=tab.id===id;
    tab.setAttribute('aria-selected',String(selected));
    tab.tabIndex=selected ? 0 : -1;
    el(tab.getAttribute('aria-controls')).hidden=!selected;
    if(selected && focus) tab.focus();
  }
}
const tabs=[...document.querySelectorAll('[role=tab]')];
tabs.forEach((tab,index) => {
  tab.addEventListener('click',() => openTab(tab.id));
  tab.addEventListener('keydown',event => {
    const options={ArrowRight:(index+1)%tabs.length,ArrowLeft:(index+tabs.length-1)%tabs.length,Home:0,End:tabs.length-1};
    if (!(event.key in options)) return;
    event.preventDefault();
    openTab(tabs[options[event.key]].id,true);
  });
});

function showAts(ats) {
  const pane=el('ats-result');
  if (!ats) {pane.replaceChildren(rows('p','No target-role analysis was saved for this revision.'));return;}
  const breakdown=rows('div','','score-grid');
  for(const part of ats.breakdown) breakdown.append(rows('p',part.label+' '+part.points+'/'+part.max));
  const suggestions=document.createElement('ul');
  ats.suggestions.forEach(tip => suggestions.append(rows('li',tip)));
  pane.replaceChildren(rows('p','ATS readiness '+ats.ats_readiness+'/100','score'),
    rows('p','Keyword coverage '+ats.score+'%'),rows('p',ats.method),breakdown,
    rows('h3','Matched keywords'),rows('p',ats.matched.join(' · ') || 'None'),
    rows('h3','Missing keywords'),rows('p',ats.missing.join(' · ') || 'None'),
    rows('h3','Improvement suggestions'),suggestions.children.length ? suggestions : rows('p','No rubric gaps detected.'));
}

async function showRevision(revision, scroll = false) {
  const ticket=++selection;
  shownRevision=revision.key;
  try {
    const detail=await request(params('/api/revision',{key:revision.key}));
    const source=await fetch(artifact(revision.key,'main.tex'));
    if(!source.ok) throw new Error('LaTeX source is unavailable.');
    const latex=await source.text();
    if(ticket!==selection) return;
    el('result-name').textContent=detail.profile.full_name;
    el('preview-text').replaceChildren(rows('p',profileText(detail.profile),'review-line'));
    el('latex-source').textContent=latex;
    el('download-latex').href=artifact(revision.key,'main.tex',true);
    el('copy-status').textContent='';
    const links=[];
    for(const name of ['resume.pdf','overleaf.pdf']){
      if(!revision.files.includes(name)) continue;
      const link=rows('a',name==='resume.pdf'?'Download PDF':'Download verified Overleaf PDF','button secondary');
      link.href=artifact(revision.key,name,true);
      links.push(link);
    }
    if(revision.url){
      const link=rows('a','Open Overleaf project');
      link.href=revision.url;
      link.target='_blank';
      link.rel='noopener noreferrer';
      links.push(link);
    }
    el('pdf-actions').replaceChildren(...links);
    const pdf=el('pdf-preview');
    pdf.hidden=!revision.files.includes('resume.pdf');
    if(!pdf.hidden) pdf.src=artifact(revision.key,'resume.pdf');
    else pdf.removeAttribute('src');
    showAts(detail.ats);
    openTab('tab-preview');
    el('results').hidden=false;
    if(scroll) el('results').scrollIntoView();
  } catch(error) {
    if(ticket===selection){shownRevision='';feedback(error.message,true);}
  }
}
el('copy-latex').addEventListener('click',async () => {
  try {
    await navigator.clipboard.writeText(el('latex-source').textContent);
    el('copy-status').textContent='LaTeX copied.';
  } catch {
    el('copy-status').textContent='Clipboard unavailable. Use Download LaTeX.';
  }
});
function clearForm(focus) {
  el('description').value='';
  el('experience-entries').replaceChildren();
  el('education-entries').replaceChildren();
  form.reset();
  // Reset the inline source upload whenever a new candidate starts.
  const importedFile = el('resume-upload');
  if (importedFile) {
    importedFile.value='';
    el('import-resume').disabled=true;
    el('resume-import-progress').hidden=true;
    el('resume-import-progress').value=0;
    el('resume-import-status').textContent='';
    el('import-source').value='';
    el('import-evidence').hidden=true;
  }
  fields.role.value='';
  roleChanged();
  updateExperience();
  showTemplate();
  feedback('');
  showStep(0, focus);
}

function stopWatching() {
  watchedJob='';
  lastJob=null;
  forgetJob();
  el('generation').hidden=true;
}

function activeJob() {
  return Boolean(watchedJob) && !(lastJob && lastJob.status==='ok' && lastJob.percent===100);
}

function resetBuilder(keepResults = false) {
  if (!keepResults) {
    ++selection;
    shownRevision='';
    el('results').hidden=true;
    el('pdf-preview').removeAttribute('src');
  }
  stopWatching();
  clearForm(!keepResults);
  if (!keepResults) form.scrollIntoView();
}
el('another').addEventListener('click', () => {
  if (!activeJob()) { resetBuilder(false); return; }
  // A job is still running: start a new candidate but keep tracking that job.
  ++selection;
  shownRevision='';
  el('results').hidden=true;
  el('pdf-preview').removeAttribute('src');
  clearForm(true);
  form.scrollIntoView();
});
el('job-next').addEventListener('click', () => {
  clearForm(true);
  feedback('New candidate started. The previous job keeps running and its result will appear when verified.');
  form.scrollIntoView();
});

function renderHistory() {
  const filter=el('history-filter').value.toLowerCase().trim();
  const items=revisions.filter(item => item.candidate.toLowerCase().includes(filter));
  el('revision-list').replaceChildren(...items.map(item => {
    const row=document.createElement('article');
    const info=document.createElement('div');
    info.append(rows('h3',item.candidate),rows('small',loc(item.modified)));
    const open=rows('button','View resume','secondary');
    open.type='button';
    open.addEventListener('click',() => showRevision(item,true));
    row.append(info,open);
    return row;
  }));
  if(!items.length) el('revision-list').append(rows('p',filter?'No matching resumes.':'No generated resumes yet.'));
}
el('history-filter').addEventListener('input',renderHistory);

async function refresh() {
  if(refreshPending) return;
  refreshPending=true;
  try {
    const snapshot=await request('/api/status');
    revisions=snapshot.revisions;
    const historyVersion=JSON.stringify(revisions);
    if(historyVersion!==historySnapshot){
      historySnapshot=historyVersion;
      renderHistory();
    }
    if (typeof snapshot.now === 'number') serverOffset = snapshot.now - Date.now() / 1000;
    const others=snapshot.jobs.filter(item => item.name!==watchedJob &&
      ['processing','queued'].includes(item.status));
    el('job-others').hidden=!others.length;
    el('job-others').textContent=others.length ? 'Also in the watcher: ' + others.map(item =>
      (item.candidate || 'Candidate') + ' (' + (item.status==='queued' ? 'waiting' : item.percent + '%') + ')').join(', ') : '';
    if (!watchedJob) return;
    const job=snapshot.jobs.find(item => item.name===watchedJob);
    if (!job) {
      el('job-stage').textContent='This job is no longer in the inbox.';
      return;
    }
    lastJob=job;
    el('generation').hidden=false;
    renderLive();
    const revision=revisions.find(item => item.key===job.revision);
    // Reset only after the watcher reports a verified 100% result whose outputs are listed.
    if (job.status==='ok' && job.percent===100 && revision) completeJob(job, revision);
  } catch(error) {
    if(watchedJob) el('job-status').textContent='Connection interrupted. Checking again; no work is restarted.';
  } finally {
    refreshPending=false;
  }
}

function renderLive() {
  if (!lastJob) return;
  const now=Date.now()/1000+serverOffset;
  const view=ResumeLive.describe(lastJob, now, shownPercent);
  el('job-title').textContent=view.title;
  el('job-state').textContent={waiting:'Waiting',running:'Running',processing:'Processing',stalled:'No heartbeat',
    retrying:'Retrying',failed:'Failed',completed:'Completed'}[view.state];
  el('job-state').dataset.state=view.state;
  el('job-progress').value=view.percent;
  el('job-stage').textContent=view.stage;
  el('job-elapsed').textContent=view.elapsed;
  el('job-status').textContent=view.status;
  el('job-detail').textContent=view.detail;
  el('job-detail').hidden=!view.detail;
  el('job-warnings').replaceChildren(...view.warnings.map(text => rows('li',text)));
  el('job-stages').replaceChildren(...view.stages.map(item => {
    const node=rows('li',item.label);
    node.dataset.state=item.state;
    if (item.state==='current') node.setAttribute('aria-current','step');
    return node;
  }));
  el('job-retry').hidden=!view.canRetry;
  el('job-next').hidden=view.complete;
  el('job-timestamp').textContent='Last update '+loc(lastJob.updated || lastJob.last);
}

function completeJob(job, revision) {
  const remembered=storedJob();
  // Clear the builder only if it still holds the candidate that was submitted, so a new
  // candidate typed while the previous job ran is never lost.
  const unchanged=!remembered || remembered.name!==job.name || remembered.submitted===JSON.stringify(payload());
  showRevision(revision,false).then(() => {
    el('result-success').hidden=false;
    if (unchanged) resetBuilder(true);
    else { watchedJob=''; lastJob=null; forgetJob(); el('generation').hidden=true; }
    el('results').scrollIntoView({block:'start'});
  });
  watchedJob='';
}

el('job-retry').addEventListener('click', async () => {
  if (!lastJob) return;
  const button=el('job-retry');
  button.disabled=true;
  try {
    await request('/api/retry',{name:lastJob.name});
    el('job-status').textContent='Retry requested. The watcher will start it at its next scan.';
  } catch(error) {
    el('job-status').textContent=error.message;
  } finally {
    button.disabled=false;
  }
});

async function init() {
  const restored=storedJob();
  if (restored && restored.name) {
    watchedJob=restored.name;
    el('generation').hidden=false;
    el('job-stage').textContent='Restoring the active job…';
  }
  try {
    const catalog=await request('/api/catalog');
    roles=catalog.roles;templates=catalog.templates;
    fields.role.replaceChildren(new Option('Select a role',''),...roles.map(role => new Option(role.name,role.id)));
    fields.template.replaceChildren(...Object.entries(templates).map(([key,name])=>new Option(name,key)));
    showTemplate();
  } catch(error) {
    feedback(error.message,true);
  }
  // Poll the authoritative status every second while a job is tracked, otherwise every 2 s.
  const poll=async () => { await refresh(); setTimeout(poll, activeJob() ? 1000 : 2000); };
  poll();
  setInterval(renderLive,1000);   // elapsed-time clock only; progress comes from the server
}
init();

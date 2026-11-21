/* The candidate's private page (/apply/status#c=<id>&k=<key>): where their application stands and, when the
   ResumeOps team has finished it, the resume downloads. Processing is the team's; nothing here changes it. */
'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.hash.slice(1));
const candidate = params.get('c') || '';
const key = params.get('k') || '';
const auth = {'X-Candidate-Id': candidate, 'X-Candidate-Key': key};
let latest = null;

function describe(job) {
  if (!job) return ['Received. Awaiting processing.', null];
  const percent = job.percent || 0;
  switch (job.status) {
    case 'complete':
      return ['Your resume is ready.' + (job.ats ? ` ATS readiness: ${job.ats} / 100.` : ''), 100];
    case 'queued':
    case 'retrying':
      return [job.queue_ahead ? `Queued for preparation (${job.queue_ahead} ahead of yours).` : 'Queued for preparation.', percent];
    case 'processing':
    case 'handed_off':
      return [`Being prepared: ${job.stage || 'in progress'}.`, percent];
    case 'needs_input':
      return ['Drafts under review.', percent];
    default:
      return ['Application under review.', percent];
  }
}

async function load() {
  if (!candidate || !key) {
    $('st-state').textContent = 'This link is incomplete. Open the private link from your application.';
    return false;
  }
  let response;
  try {
    response = await fetch('/apply/api/automation', {headers: auth, cache: 'no-store', credentials: 'same-origin'});
  } catch {
    $('st-state').textContent = 'Could not reach ResumeOps. Trying again…';
    return true;
  }
  if (response.status === 403) {
    $('st-state').textContent = 'Invalid private link. Open the saved link or request a replacement.';
    return false;
  }
  if (!response.ok) {
    $('st-state').textContent = 'Could not load your application. Trying again…';
    return true;
  }
  const data = await response.json();
  const who = data.candidate || {};
  $('st-who').textContent = [who.candidate || candidate, who.name, who.role].filter(Boolean).join(' · ');
  latest = (data.jobs || [])[0] || null;
  const [text, percent] = describe(latest);
  $('st-state').textContent = text;
  const done = Boolean(latest && latest.status === 'complete' && latest.revision);
  $('st-progress').hidden = percent === null || done;
  if (percent !== null) $('st-bar').style.width = `${Math.max(4, Math.min(100, percent))}%`;
  $('st-files').hidden = !done;
  return !done;
}

async function download(name, filename) {
  $('st-error').textContent = '';
  try {
    const response = await fetch('/apply/api/artifact?' + new URLSearchParams({key: latest.revision, name, download: '1'}),
      {headers: auth, cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error();
    const url = URL.createObjectURL(await response.blob());
    const anchor = Object.assign(document.createElement('a'), {href: url, download: filename});
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch {
    $('st-error').textContent = 'The download did not start. Try again in a moment.';
  }
}

async function init() {
  $('st-edit').href = `/apply#edit&c=${encodeURIComponent(candidate)}&k=${encodeURIComponent(key)}`;
  $('st-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); $('st-copy').textContent = 'Copied'; }
    catch { $('st-copy').textContent = 'Copy the address bar'; }
  });
  $('st-pdf').addEventListener('click', () => download('resume.pdf', `${candidate}-resume.pdf`));
  $('st-tex').addEventListener('click', () => download('main.tex', `${candidate}-resume.tex`));
  while (await load()) await new Promise(resolve => setTimeout(resolve, 10000));
}

init();

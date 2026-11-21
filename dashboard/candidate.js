/* The candidate's status page (/apply/status#c=<id>&k=<key>): the dashboard's resume list, review cards and
   downloads (automation.js, unchanged) signed in by the candidate's private key instead of an account. Only the
   candidate's own resumes are reachable: every request carries the key, and the server checks it. */
'use strict';
(function (root) {
  const STORE = 'resumeops-applications';
  const params = new URLSearchParams(location.hash.slice(1));
  let candidate = params.get('c') || '';
  let key = params.get('k') || '';
  function saved() { try { return JSON.parse(localStorage.getItem(STORE) || '[]'); } catch { return []; } }
  function remember() {
    try {
      const list = saved().filter(item => item.c !== candidate);
      list.unshift({c: candidate, k: key, at: Date.now()});
      localStorage.setItem(STORE, JSON.stringify(list.slice(0, 10)));
    } catch { /* private browsing: the link still works */ }
  }
  if (!candidate || !key) {  // the address without its private part: this device's latest application
    const latest = saved()[0];
    if (latest) {
      candidate = latest.c; key = latest.k;
      history.replaceState(null, '', '#c=' + encodeURIComponent(candidate) + '&k=' + encodeURIComponent(key));
    }
  } else remember();

  const ROUTES = [['/api/automation', '/apply/api/automation'], ['/api/artifact', '/apply/api/artifact'],
    ['/api/revision', '/apply/api/revision'], ['/api/catalog', '/apply/api/catalog']];
  function mapped(url) {
    for (const [from, to] of ROUTES) {
      if (url === from || url.startsWith(from + '/') || url.startsWith(from + '?')) return to + url.slice(from.length);
    }
    return null;
  }
  const headers = extra => ({...extra, 'X-Candidate-Id': candidate, 'X-Candidate-Key': key});
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function call(url, init = {}, timeoutMs = 22000) {
    const target = mapped(url);
    if (!target) {
      return new Response(JSON.stringify({error: 'Not available on this page.'}),
        {status: 404, headers: {'Content-Type': 'application/json'}});
    }
    for (let attempt = 0; ; attempt++) {
      try {
        const response = await fetch(target, {credentials: 'same-origin', cache: 'no-store',
          signal: AbortSignal.timeout(timeoutMs), ...init, headers: headers(init.headers)});
        if ((response.status === 503 || response.status === 429) && attempt < 6) {
          await pause(1000 * (attempt + 1) + Math.random() * 1000);
          continue;
        }
        if (response.status === 403) showInvalid();
        return response;
      } catch (error) {
        if (attempt < 3) { await pause(800 * (attempt + 1)); continue; }
        throw error;
      }
    }
  }
  const session = {owner: 'candidate:' + candidate, token: ''};
  root.ResumeAuth = {call, headers, ready: async () => session, session: async () => session,
    waitForSession: async () => session, expired: async () => false, setToken() {}, onBeforeRenewal() {},
    onToken() {}, get owner() { return session.owner; }, get token() { return ''; }};

  // A plain link cannot send the private key, so Preview opens the PDF fetched with it.
  document.addEventListener('click', async event => {
    const link = event.target.closest('a[target="_blank"][href^="/api/artifact"]');
    if (!link) return;
    event.preventDefault();
    const view = window.open('', '_blank');
    try {
      const response = await call(link.getAttribute('href'), {}, 120000);
      if (!response.ok) throw new Error('unavailable');
      const url = URL.createObjectURL(await response.blob());
      if (view) view.location = url; else location.assign(url);
      setTimeout(() => URL.revokeObjectURL(url), 120000);
    } catch { if (view) view.close(); }
  }, true);

  // The candidate's own header: their ID, the private link to keep, and corrections.
  const banner = document.createElement('section');
  banner.className = 'cand-banner';
  banner.setAttribute('aria-label', 'Your application');
  const title = document.createElement('div');
  const heading = document.createElement('h1');
  heading.textContent = 'Your application';
  const detail = document.createElement('p');
  detail.id = 'cand-detail';
  detail.textContent = candidate ? 'Application ' + candidate : 'Open the private link from your application.';
  title.append(heading, detail);
  const note = document.createElement('p');
  note.className = 'cand-note';
  note.textContent = 'Keep this page\'s link private: it is the only way to open your application. ' +
    'When AI drafts are ready, review them below; downloads appear when your resume is verified.';
  const actions = document.createElement('div');
  actions.className = 'cand-actions';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.textContent = 'Copy my private link';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); copy.textContent = 'Link copied'; }
    catch { window.prompt('Copy your private link:', location.href); }
  });
  const edit = document.createElement('a');
  edit.className = 'cand-edit';
  edit.textContent = 'Correct my details';
  edit.href = '/apply#edit&c=' + encodeURIComponent(candidate) + '&k=' + encodeURIComponent(key);
  const another = document.createElement('a');
  another.textContent = 'New application';
  another.href = '/apply';
  actions.append(copy, edit, another);
  banner.append(title, note, actions);
  function showInvalid() {
    detail.textContent = 'This private link is not valid. Use the link from your application, or ask the ' +
      'recruiter for a new one.';
    banner.classList.add('cand-invalid');
  }
  if (!candidate || !key) showInvalid();
  const main = document.querySelector('main');
  if (main) main.before(banner);

  // The application's name and the queue, from the same private request the list makes.
  call('/api/automation').then(async response => {
    if (!response.ok) return;
    const data = await response.json();
    const who = data.candidate || {};
    detail.textContent = ['Application ' + candidate, who.name, who.role].filter(Boolean).join(' · ');
  }).catch(() => {});
}(self));

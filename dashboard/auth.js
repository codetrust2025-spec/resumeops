/* ResumeOps never persists an Access credential. Renewal always goes through Cloudflare. */
(function (root) {
  'use strict';
  let token = '';
  let owner = '';
  let expires = null;
  let waiting;
  let initial;
  let banner;
  const listeners = [];
  let checkpoint = async () => {};
  const pause = delay => new Promise(resolve => setTimeout(resolve, delay));

  function setToken(value) {
    token = value;
    listeners.forEach(listener => listener(value));
  }
  function headers(extra) {
    return {...extra, 'X-Requested-With':'XMLHttpRequest','X-Dashboard-Token':token,
      ...(owner ? {'X-Resume-Owner':owner} : {})};
  }
  async function expired(response) {
    if (response.type === 'opaqueredirect' || response.redirected || response.status === 401) return true;
    if (response.status === 403) {
      try { return /access denied/i.test((await response.clone().json()).error || ''); }
      catch { return true; }
    }
    return response.ok && response.headers.get('Content-Type')?.includes('text/html');
  }
  async function session() {
    const response = await fetch('/api/session', {headers:{'X-Requested-With':'XMLHttpRequest'}, redirect:'manual', credentials:'same-origin',
      cache:'no-store', signal:AbortSignal.timeout(12000)});
    if (await expired(response)) return null;
    if (!response.ok) throw new Error('Could not check the ResumeOps session.');
    const body = await response.json();
    if (!body.owner || !body.token) throw new Error('Invalid session response.');
    if (owner && owner !== body.owner) return null;  // keep all paused work bound to its original identity
    owner = body.owner;
    expires = body.expires ? Date.now() + (body.expires - body.now) * 1000 : null;
    setToken(body.token);
    return body;
  }
  function showBanner() {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'auth-banner';
      banner.className = 'auth-banner';
      banner.setAttribute('role','alert');
      const text = document.createElement('p');
      text.textContent = 'Sign in again as the same user. ResumeOps keeps your work and resumes after sign-in.';
      const button = document.createElement('button');
      button.type = 'button';
      button.id = 'auth-renew';
      button.className = 'primary';
      button.textContent = 'Sign in again';
      const status = document.createElement('small');
      button.addEventListener('click', async () => {
        const tab = root.open('/auth/renewed','_blank');
        if (tab) {
          status.textContent = 'Waiting for sign-in in the other tab...';
          return;
        }
        button.disabled = true;
        status.textContent = 'Saving your work before signing in...';
        try {
          if (owner) await checkpoint();
          const path = root.location.pathname;
          const destination = path === '/automated' ? 'automated' :
            document.body.hasAttribute('data-ai-builder') ? 'ai-builder' :
            path === '/workflow' ? 'workflow' : 'builder';
          root.location.assign('/auth/renewed?return=' + destination);
        } catch {
          status.textContent = 'Could not save your work. Allow pop-ups and try again.';
          button.disabled = false;
        }
      });
      banner.append(text,button,status);
    }
    if (!banner.isConnected) document.body.prepend(banner);
  }
  function waitForSession() {
    if (waiting) return waiting;
    showBanner();
    waiting = new Promise(resolve => {
      let probing = false;
      let complete = false;
      const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('resumeops-auth');
      const probe = async () => {
        if (probing || complete) return;
        probing = true;
        try {
          const status = await session();
          if (!status) return;
          complete = true;
          clearInterval(timer);
          root.removeEventListener('focus',probe);
          root.removeEventListener('message',message);
          root.removeEventListener('storage',storage);
          channel?.close();
          banner?.remove();
          waiting = null;
          resolve(status);
        } catch {
          // An unreachable origin is not proof that the user signed out.
        } finally { probing = false; }
      };
      const message = event => {
        if (event.origin === root.location.origin && event.data === 'resumeops-auth-renewed') probe();
      };
      const storage = event => { if (event.key === 'resumeops.authRenewed') probe(); };
      const timer = setInterval(probe,3000);
      if (channel) channel.onmessage = probe;
      root.addEventListener('focus',probe);
      root.addEventListener('message',message);
      root.addEventListener('storage',storage);
      queueMicrotask(probe);
    });
    return waiting;
  }
  function ready() {
    if (!initial) initial = session().then(value => value || waitForSession()).catch(error => {
      initial = null;
      throw error;
    });
    return initial;
  }
  async function call(url, init = {}) {
    await ready();
    if (waiting) await waiting;
    if (expires && expires - Date.now() < 15000) {
      const current = await session();
      if (!current) await waitForSession();
    }
    let retriedToken = false;
    let failures = 0;
    for (;;) {
      if (waiting) await waiting;
      let response;
      try {
        response = await fetch(url,{credentials:'same-origin',redirect:'manual',cache:'no-store',
          signal:AbortSignal.timeout(22000),...init,headers:headers(init.headers)});
      } catch (error) {
        let current;
        try { current = await session(); } catch {}
        if (current === null) { await waitForSession(); continue; }
        if (++failures <= 2) { await pause(failures * 700); continue; }
        throw error;
      }
      if (await expired(response)) { await waitForSession(); continue; }
      if (response.status===403 && !retriedToken) {
        retriedToken=true;
        const before=token;
        const current=await session();
        if (!current) { await waitForSession(); continue; }
        if (before!==token) continue;
      }
      return response;
    }
  }
  root.ResumeAuth = {call,headers,expired,waitForSession,session,ready,setToken,
    onBeforeRenewal: fn => { checkpoint = fn; },
    get owner() { return owner; }, get token() { return token; }, onToken:fn => listeners.push(fn)};
}(self));

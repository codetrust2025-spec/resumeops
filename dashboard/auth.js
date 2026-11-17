/* Session resilience for ResumeOps behind Cloudflare Access.

   Cloudflare renews the application token only on a page navigation; background requests from an
   open page are blocked once it expires. Every API call goes through ResumeAuth.call(), which:
   - asks Access for a 401 (not a redirect) on expiry with X-Requested-With: XMLHttpRequest,
   - on expiry pauses the call, keeps everything on the page, and offers "Sign in again" in a
     separate tab (renewal is silent while the 1-month global session is valid),
   - continues the paused calls automatically once the session works again,
   - refreshes the request token once if the dashboard restarted (new token after a deploy).
   Authentication is never bypassed: calls are only retried after the server accepts the session. */
(function (root) {
  'use strict';
  const listeners = [];
  let token = '';
  let waiting = null;          // shared promise while the session is expired
  let banner = null;
  let probeTimer = null;

  function setToken(value) {
    token = value;
    listeners.forEach(fn => fn(value));
  }

  function headers(extra) {
    return Object.assign({'X-Requested-With': 'XMLHttpRequest', 'X-Dashboard-Token': token}, extra || {});
  }

  async function bodyOf(response) {
    try { return await response.clone().json(); } catch { return null; }
  }

  /** True when the response means "not signed in" (Access edge or this server's own check). */
  async function expired(response) {
    if (response.type === 'opaqueredirect' || response.status === 401) return true;
    if (response.status === 403) {
      const body = await bodyOf(response);
      return Boolean(body && /access denied/i.test(body.error || ''));
    }
    return false;
  }

  async function session() {
    const response = await fetch('/api/session', {headers: headers(), redirect: 'manual',
      credentials: 'same-origin', cache: 'no-store'});
    if (await expired(response) || !response.ok) return null;
    const body = await response.json();
    if (body && body.token) setToken(body.token);
    return body;
  }

  function showBanner() {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'auth-banner';
      banner.className = 'auth-banner';
      banner.setAttribute('role', 'alert');
      const text = document.createElement('p');
      text.textContent = 'Your ResumeOps sign-in has expired. Everything on this page is kept, and any ' +
        'running resume job continues on the server.';
      const button = document.createElement('button');
      button.type = 'button';
      button.id = 'auth-renew';
      button.className = 'primary';
      button.textContent = 'Sign in again';
      button.addEventListener('click', () => {
        // A separate tab keeps this page (and the unsaved candidate) intact.
        const tab = root.open('/auth/renewed', '_blank');
        // New tabs blocked: renew in this tab. Unsaved details and a running import are kept in
        // this tab's session storage and restored when it returns to the builder.
        if (!tab) {
          root.dispatchEvent(new Event('resumeops:before-renewal'));
          root.location.assign('/auth/renewed');
        }
        status.textContent = 'Waiting for sign-in in the other tab…';
      });
      const status = document.createElement('small');
      status.id = 'auth-status';
      banner.append(text, button, status);
    }
    if (!banner.isConnected) document.body.prepend(banner);
  }

  function hideBanner() {
    if (banner && banner.isConnected) banner.remove();
  }

  /** Resolves once the server accepts this browser's session again. */
  function waitForSession() {
    if (waiting) return waiting;
    showBanner();
    waiting = new Promise(resolve => {
      const done = async () => {
        if (!waiting) return;
        let ok = null;
        try { ok = await session(); } catch { ok = null; }
        if (!ok) return;
        clearInterval(probeTimer);
        probeTimer = null;
        waiting = null;
        hideBanner();
        resolve(ok);
      };
      try { new BroadcastChannel('resumeops-auth').onmessage = done; } catch {}
      root.addEventListener('message', event => {
        if (event.origin === root.location.origin && event.data === 'resumeops-auth-renewed') done();
      });
      root.addEventListener('storage', event => { if (event.key === 'resumeops.authRenewed') done(); });
      root.addEventListener('focus', done);
      probeTimer = setInterval(done, 5000);
    });
    return waiting;
  }

  /**
   * fetch() that survives session expiry and dashboard restarts. Requests are only retried when
   * repeating them is safe; ResumeOps POSTs are idempotent (queue by content hash, imports by id).
   */
  async function call(url, init) {
    let tokenRefreshed = false;
    for (let attempt = 0; ; attempt++) {
      if (waiting) await waiting;
      let response;
      try {
        const options = Object.assign({credentials: 'same-origin', redirect: 'manual'}, init || {});
        options.headers = headers(init && init.headers);
        response = await fetch(url, options);
      } catch (error) {
        // A blocked cross-origin redirect looks like a network error: ask the session endpoint.
        let state;
        try {
          const probe = await fetch('/api/session', {headers: headers(), redirect: 'manual',
            credentials: 'same-origin', cache: 'no-store'});
          state = await expired(probe) ? 'expired' : 'online';
        } catch { state = 'offline'; }
        if (state === 'expired') { await waitForSession(); continue; }
        if (state === 'online' && attempt < 2) continue;
        throw error;
      }
      if (await expired(response)) { await waitForSession(); continue; }
      if (response.status === 403 && !tokenRefreshed) {
        const body = await bodyOf(response);
        if (body && /not authorized/i.test(body.error || '')) {
          tokenRefreshed = true;
          if (await session()) continue;
        }
      }
      return response;
    }
  }

  root.ResumeAuth = {call, headers, expired, waitForSession, session,
    setToken, get token() { return token; }, onToken: fn => listeners.push(fn)};
}(typeof self !== 'undefined' ? self : this));

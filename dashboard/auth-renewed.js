'use strict';
try { new BroadcastChannel('resumeops-auth').postMessage('renewed'); } catch {}
try { localStorage.setItem('resumeops.authRenewed', String(Date.now())); } catch {}
try { window.opener?.postMessage('resumeops-auth-renewed', location.origin); } catch {}
if (window.opener) setTimeout(() => window.close(), 600);
const destination = new URLSearchParams(location.search).get('return');
if (destination === 'builder') location.replace('/');
if (destination === 'ai-builder') location.replace('/ai-builder');
if (destination === 'workflow') location.replace('/workflow');

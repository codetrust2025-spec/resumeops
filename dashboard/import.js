/* Import an existing resume into the single-page builder. This never queues or saves a job. */
(() => {
  'use strict';
  const fileInput = document.getElementById('resume-upload');
  const button = document.getElementById('import-resume');
  const status = document.getElementById('resume-import-status');
  const progress = document.getElementById('resume-import-progress');
  const allowed = new Set(['.pdf','.docx','.doc','.odt','.rtf','.txt','.md',
    '.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp','.json']);
  let busy = false;

  function message(value, isError = false) {
    status.textContent = value;
    status.classList.toggle('error', isError);
    status.classList.remove('done');
  }
  function stage(value, percent) {
    progress.hidden = false;
    progress.value = Math.max(progress.value, Math.min(100, percent));
    message(value);
  }
  function extensionOf(file) {
    const name = file.name.toLowerCase();
    return name.includes('.') ? '.' + name.split('.').pop() : '';
  }
  async function readDocument(file, extension) {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
    const url = '/api/import-progress?id=' + id;
    // Upload bytes are measured (0-25%). Reading and extraction start points are real boundaries;
    // Ollama extraction has no knowable length, so the bar holds at 35% and the measured output
    // and elapsed time are shown instead of an invented percentage.
    const stages = {'Reading document': 30, 'Extracting candidate details with Ollama': 35,
      'Draft ready for review': 95};
    // The upload response is immediate; expensive Ollama extraction happens on the RTX.
    // On session expiry the same file is sent again with the same import id after sign-in; if the
    // first upload already started, the server answers 409 and the import simply continues.
    const sendOnce = () => new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && event.total) {
          stage('Uploading resume', Math.floor(event.loaded / event.total * 25));
        }
      };
      xhr.upload.onload = () => stage('Upload complete. Starting extraction', 25);
      xhr.onerror = () => resolve({network: true});
      xhr.onabort = () => resolve({aborted: true});
      xhr.onload = () => resolve({status: xhr.status, body: xhr.response});
      xhr.open('POST', '/api/draft');
      xhr.responseType = 'json';
      xhr.withCredentials = true;
      const headers = ResumeAuth.headers({'X-Resume-Extension': extension, 'X-Resume-Import-ID': id,
        'X-Resume-Async': '1'});
      for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
      xhr.send(file);
    });
    let tokenRefreshed = false;
    for (;;) {
      const outcome = await sendOnce();
      if (outcome.aborted) throw new Error('Resume upload was cancelled.');
      if (outcome.status === 202 && outcome.body?.status === 'processing') break;
      if (outcome.status === 409) break;                 // this import id is already running
      const accessDenied = outcome.status === 401 || outcome.status === 0 ||
        (outcome.status === 403 && /access denied/i.test(outcome.body?.error || ''));
      if (outcome.network || accessDenied) {
        let signedOut = accessDenied;
        if (outcome.network) {
          try { signedOut = !(await ResumeAuth.session()); } catch { signedOut = false; }
        }
        if (signedOut) {
          message('Sign-in expired during upload. The file is kept; it continues after you sign in again.', true);
          await ResumeAuth.waitForSession();
          stage('Signed in again. Resuming upload', 1);
          continue;
        }
        // The browser lost the reply; the server may already be working on it.
        stage('Reconnecting to resume import', 25);
        break;
      }
      if (outcome.status === 403 && !tokenRefreshed && /not authorized/i.test(outcome.body?.error || '')) {
        tokenRefreshed = true;
        if (await ResumeAuth.session()) continue;
      }
      throw new Error(outcome.body?.error || 'Upload rejected (HTTP ' + outcome.status + '). Check the server logs.');
    }
    let budget = 10 * 60 * 1000;   // processing time only; time spent signing in again is added back
    const started = Date.now();
    let unknown = 0;
    let failures = 0;
    while (Date.now() - started < budget) {
      let result;
      const before = Date.now();
      try {
        const response = await ResumeAuth.call(url, {cache: 'no-store'});
        if (Date.now() - before > 5000) budget += Date.now() - before;
        if (!response.ok) throw new Error('Status check failed (HTTP ' + response.status + ')');
        result = await response.json();
        failures = 0;
      } catch (error) {
        if (++failures >= 6) {
          throw new Error('Could not reconnect to import status. Verify your connection and retry.');
        }
        stage('Reconnecting to import status', 35);
        await new Promise(resolve => setTimeout(resolve, 1500));
        continue;
      }
      if (result.status === 'complete' && result.profile) return result;
      if (result.status === 'error') throw new Error(result.error || 'Local extraction failed.');
      if (result.status === 'unknown') {
        if (++unknown >= 4) throw new Error('Import session not found. Upload the file again.');
      } else {
        unknown = 0;
        const pct = stages[result.stage];
        if (pct !== undefined) stage(result.stage, pct);
        if (result.started && result.now) {
          const clock = s => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
          const m = result.metrics;
          const parts = [result.stage + ' — processing', 'elapsed ' + clock(result.now - result.started)];
          if (m && m.phase === 'waiting') parts.push('waiting for the model to start');
          if (m && m.tokens) parts.push(m.chars.toLocaleString() + ' characters · ' +
            (m.elapsed ? (m.tokens / m.elapsed).toFixed(1) : '0') + ' tokens/s');
          const idle = result.now - (result.activity || result.now);
          if (idle > 30) parts.push('no new output for ' + Math.round(idle) + ' s');
          message(parts.join(' · '));
        }
      }
      await new Promise(resolve => setTimeout(resolve, 1100));
    }
    throw new Error('Extraction is taking unusually long. Check Ollama and RTX status before retrying.');
  }

  async function prefill(data) {
    const profile = data.profile;
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) {
      throw new Error('The uploaded file does not contain candidate details.');
    }
    // Clears only the builder draft, never the inbox, output history or saved results.
    const count = Object.values(profile).filter(v => v && (!Array.isArray(v) || v.length)).length;
    stage('Imported ' + count + ' candidate sections. Review each step before generating.', 100);
    status.classList.add('done');
    await applyImport(data);
    feedback('Imported ' + count + ' candidate sections from the resume. Choose the target role, then review each step.');
  }

  fileInput.addEventListener('change', () => {
    button.disabled = busy || !fileInput.files.length;
    progress.hidden = true;
    progress.value = 0;
    message('');
  });
  button.addEventListener('click', async () => {
    if (busy || !fileInput.files.length) return;
    const file = fileInput.files[0];
    const extension = extensionOf(file);
    const max = extension === '.json' ? 1024 * 1024 : 12 * 1024 * 1024;
    if (!allowed.has(extension)) {
      message('Unsupported file type. Choose PDF, Word, TXT, Markdown, image or candidate JSON.', true);
      return;
    }
    if (!file.size || file.size > max) {
      message('File must be non-empty and no larger than ' + (max / 1048576) + ' MB.', true);
      return;
    }
    busy = true;
    button.disabled = true;
    fileInput.disabled = true;
    progress.value = 0;
    stage('Reading resume', 1);
    try {
      let data;
      if (extension === '.json') {
        const parsed = JSON.parse(await file.text());
        data = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ?
          ('profile' in parsed ? parsed : {profile: parsed}) : null;
      } else {
        data = await readDocument(file, extension);
      }
      await prefill(data || {});
      fileInput.value = '';
    } catch (error) {
      message('Import failed: ' + error.message + ' Existing form details were not queued.', true);
    } finally {
      busy = false;
      fileInput.disabled = false;
      button.disabled = !fileInput.files.length;
    }
  });
})();

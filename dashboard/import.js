/* Import an existing resume into the single-page builder. This never queues or saves a job. */
(() => {
  'use strict';
  const fileInput = document.getElementById('resume-upload');
  const button = document.getElementById('import-resume');
  const status = document.getElementById('resume-import-status');
  const progress = document.getElementById('resume-import-progress');
  const evidence = document.getElementById('import-evidence');
  const sourceField = document.getElementById('import-source');
  const allowed = new Set(['.pdf','.docx','.doc','.odt','.rtf','.txt','.md',
    '.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp','.json']);
  const asText = value => typeof value === 'string' ? value : '';
  const joinLines = value => Array.isArray(value) ? value.filter(item => typeof item === 'string').join('\n') : '';
  let busy = false;

  function message(value, isError = false) {
    status.textContent = value;
    status.classList.toggle('error', isError);
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
    const upload = new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && event.total) {
          stage('Uploading resume', Math.floor(event.loaded / event.total * 25));
        }
      };
      xhr.upload.onload = () => stage('Upload complete. Starting extraction', 25);
      xhr.onerror = () => reject(new Error('Upload connection interrupted. Checking whether processing started.'));
      xhr.onabort = () => reject(new Error('Resume upload was cancelled.'));
      xhr.onload = () => {
        const response = xhr.response;
        if (xhr.status === 202 && response?.status === 'processing') {
          resolve(response);
        } else {
          reject(new Error(response?.error || (
            xhr.status === 403 ? 'Session expired. Sign in through Cloudflare Access and retry.' :
            'Upload rejected (HTTP ' + xhr.status + '). Check the server logs.')));
        }
      };
      xhr.open('POST', '/api/draft');
      xhr.responseType = 'json';
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-Dashboard-Token', token);
      xhr.setRequestHeader('X-Resume-Extension', extension);
      xhr.setRequestHeader('X-Resume-Import-ID', id);
      xhr.setRequestHeader('X-Resume-Async', '1');
      xhr.send(file);
    });
    try {
      await upload;
    } catch (error) {
      // If the browser lost the 202 response, the server might still be working.
      if (!error.message.includes('Upload connection interrupted')) throw error;
      stage('Reconnecting to resume import', 25);
    }
    const started = Date.now();
    let unknown = 0;
    let failures = 0;
    while (Date.now() - started < 10 * 60 * 1000) {
      let result;
      try {
        const response = await fetch(url, {
          headers: {'X-Dashboard-Token': token},
          credentials: 'same-origin',
          cache: 'no-store',
        });
        if (response.status === 403) {
          throw new Error('Session expired. Sign in through Cloudflare Access and retry.');
        }
        if (!response.ok) throw new Error('Status check failed (HTTP ' + response.status + ')');
        result = await response.json();
        failures = 0;
      } catch (error) {
        if (error.message.includes('Session expired')) throw error;
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

  function setEntries(kind, entries) {
    const holder = document.getElementById(kind + '-entries');
    holder.replaceChildren();
    if (!Array.isArray(entries)) return;
    for (const entry of entries.slice(0, 40)) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
      if (!Object.values(entry).some(Boolean)) continue;
      addEntry(kind, false);
      const row = holder.lastElementChild;
      for (const input of row.querySelectorAll('[data-field]')) {
        const value = entry[input.dataset.field];
        input.value = input.dataset.field === 'bullets' ? joinLines(value) : asText(value);
        input.dispatchEvent(new Event('input', {bubbles:true}));
      }
    }
  }

  async function prefill(data) {
    const profile = data.profile;
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) {
      throw new Error('The uploaded file does not contain candidate details.');
    }
    const target = data.target && typeof data.target === 'object' && !Array.isArray(data.target)
      ? data.target : {};
    // Clear only the builder draft, never the inbox, output history or saved results.
    for (const key of ['full_name','email','phone','location','current_company',
      'headline','linkedin','github','portfolio','summary']) {
      fields[key].value = asText(profile[key]);
    }
    for (const key of ['skills','certifications','projects']) {
      fields[key].value = joinLines(profile[key]);
    }
    setEntries('experience', profile.experience);
    setEntries('education', profile.education);
    // A new upload must not inherit role keywords or experience from a previous person.
    fields.role.value = '';
    fields.template.selectedIndex = 0;
    fields.keywords.value = '';
    fields.years.value = '1';
    document.getElementById('catalog-keywords').textContent = '';
    if (typeof target.role === 'string' &&
        [...fields.role.options].some(option => option.value === target.role)) {
      fields.role.value = target.role;
      await roleChanged();
    }
    if (typeof target.template === 'string' &&
        [...fields.template.options].some(option => option.value === target.template)) {
      fields.template.value = target.template;
    }
    if (Array.isArray(target.keywords)) fields.keywords.value = joinLines(target.keywords);
    if (Number.isInteger(target.experience_years) &&
        target.experience_years >= 0 && target.experience_years <= 25) {
      fields.years.value = String(target.experience_years);
    }
    updateExperience();
    showTemplate();
    fields.verified.checked = false;
    // Avoid showing the previous candidate's generated output after a new import.
    watchedJob = '';
    shownRevision = '';
    ++selection;
    document.getElementById('results').hidden = true;
    document.getElementById('generation').hidden = true;
    document.getElementById('pdf-preview').removeAttribute('src');
    sourceField.value = asText(data.source);
    evidence.hidden = !sourceField.value;
    evidence.open = false;
    feedback('');
    showStep(0, false); // Choose/review a role first, then inspect prefilled details.
    const count = Object.values(profile).filter(v => v && (!Array.isArray(v) || v.length)).length;
    stage('Imported ' + count + ' candidate sections. Select a role, then Continue to review and correct the prefilled details.', 100);
  }

  document.getElementById('template-import').addEventListener('click', () => {
    fileInput.click(); // Same-page picker: do not navigate to /operations.
  });
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

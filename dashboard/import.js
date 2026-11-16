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
  function readDocument(file, extension) {
    return new Promise((resolve, reject) => {
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2, '0')).join('');
      const xhr = new XMLHttpRequest();
      let active = true;
      let polling = false;
      const percentages = {'Reading document': 35, 'Extracting candidate details with Ollama': 70};
      const poll = async () => {
        if (!active || polling) return;
        polling = true;
        try {
          const response = await fetch('/api/import-progress?id=' + id,
            {headers: {'X-Dashboard-Token': token}});
          if (response.ok) {
            const result = await response.json();
            if (active && percentages[result.stage] !== undefined) {
              stage(result.stage, percentages[result.stage]);
            }
          }
        } catch {
          // The upload response remains authoritative even if progress polling fails.
        } finally {polling = false;}
      };
      const interval = setInterval(poll, 600);
      const finish = () => {active = false; clearInterval(interval);};
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && event.total) {
          stage('Uploading resume', Math.floor(event.loaded / event.total * 25));
        }
      };
      xhr.upload.onload = () => {stage('Reading document', 25);poll();};
      xhr.onerror = () => {finish();reject(new Error('Connection lost while importing. Retry the upload.'));};
      xhr.onabort = () => {finish();reject(new Error('Resume import cancelled.'));};
      xhr.onload = () => {
        finish();
        const result = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300 && result?.profile &&
            typeof result.profile === 'object' && !Array.isArray(result.profile)) {
          resolve(result);
        } else {
          reject(new Error(result?.error || 'Import failed (HTTP ' + xhr.status + ').'));
        }
      };
      xhr.open('POST','/api/draft');
      xhr.responseType = 'json';
      xhr.setRequestHeader('X-Dashboard-Token', token);
      xhr.setRequestHeader('X-Resume-Extension', extension);
      xhr.setRequestHeader('X-Resume-Import-ID', id);
      xhr.send(file);
    });
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

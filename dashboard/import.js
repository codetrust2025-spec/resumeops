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
  const pause = delay => new Promise(resolve => setTimeout(resolve, delay));

  function sendUpload(work) {
    return new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST','/api/draft');
      xhr.responseType = 'json';
      xhr.timeout = 65000;
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && event.total) {
          stage('Uploading resume', Math.floor(event.loaded / event.total * 25));
        }
      };
      xhr.onload = () => resolve({status:xhr.status,body:xhr.response});
      xhr.onerror = xhr.ontimeout = xhr.onabort = () => resolve({status:0});
      const headers = ResumeAuth.headers({'X-Resume-Extension':work.extension,
        'X-Resume-Import-ID':work.id,'X-Resume-Async':'1'});
      for (const [name,value] of Object.entries(headers)) xhr.setRequestHeader(name,value);
      xhr.send(work.file);
    });
  }

  async function readDocument(work) {
    const url='/api/import-progress?id='+work.id;
    const stages={'Reading document':25,'Extracting candidate details with Ollama':50,
      'Draft ready for review':100};
    let failures=0;
    let tokenRefresh=false;
    for (;;) {
      try {
        const response=await ResumeAuth.call(url);
        if (!response.ok) throw new Error('Import status could not be read.');
        const result=await response.json();
        if (result.status==='complete' && result.profile) return result;
        if (result.status==='error') {
          await ResumeRecovery.remove('import');
          return Promise.reject(new Error(result.error || 'Local extraction failed.'));
        }
        if(result.status==='unknown'){
          if(!work.file) {
            await ResumeRecovery.remove('import');
            return Promise.reject(new Error('The saved import expired. Select the source file again.'));
          }
          const outcome=await sendUpload(work);
          if(outcome.status===202){
            // Keep the file until the server has durably accepted it. Further recovery only needs the ID.
            work={id:work.id, extension:work.extension};
            await ResumeRecovery.put('import',work);
            stage('Upload accepted. Reading document',25);
          } else if(outcome.status===401 || outcome.status===0 ||
                    (outcome.status===403 && !outcome.body)){
            const current=await ResumeAuth.session().catch(() => undefined);
            if(current===null) await ResumeAuth.waitForSession();
            else stage('Reconnecting to upload',progress.value);
          } else if(outcome.status===403 && !tokenRefresh){
            tokenRefresh=true;
            const current=await ResumeAuth.session();
            if(!current) await ResumeAuth.waitForSession();
          } else if(outcome.status!==429 && outcome.status<500){
            await ResumeRecovery.remove('import');
            return Promise.reject(new Error(outcome.body?.error || 'The resume upload was rejected.'));
          }
        } else {
          if(work.file){
            work={id:work.id, extension:work.extension};
            await ResumeRecovery.put('import',work);
          }
          stage(result.stage,stages[result.stage] ?? progress.value);
        }
        failures=0;
      } catch(error) {
        ++failures;
        stage('Connection interrupted. ResumeOps will retry this import.',progress.value);
      }
      await pause(Math.min(10000,1000*(1+failures)));
    }
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

  fileInput.addEventListener('change', async () => {
    button.disabled = busy || !fileInput.files.length;
    progress.hidden = true;
    progress.value = 0;
    message('');
    try {
      const file = fileInput.files[0];
      if (file && allowed.has(extensionOf(file)) && file.size <= 12 * 1024 * 1024) {
        await ResumeRecovery.put('selected-import', file);
      } else {
        await ResumeRecovery.remove('selected-import');
      }
    } catch {
      message('The selected file could not be saved for reload recovery.', true);
    }
  });
  async function run(work, resumed = false) {
    if(busy) return;
    busy=true;
    button.disabled=true;
    fileInput.disabled=true;
    progress.value=0;
    if (resumed) showStep(STEP.INPUT, false);
    stage(resumed ? 'Resuming your import' : 'Reading resume',0);
    try {
      let data;
      if(work.extension==='.json'){
        const parsed=JSON.parse(await work.file.text());
        data=parsed && typeof parsed==='object' && !Array.isArray(parsed) ?
          ('profile' in parsed ? parsed : {profile:parsed}) : null;
      } else {
        data=await readDocument(work);
      }
      await prefill(data || {});
      await persistDraft();
      await ResumeRecovery.remove('import');
      await ResumeRecovery.remove('selected-import');
      fileInput.value='';
    } catch(error) {
      if (work.extension === '.json') await ResumeRecovery.remove('import').catch(() => {});
      message('Import could not complete: '+error.message,true);
    } finally {
      busy=false;
      fileInput.disabled=false;
      button.disabled=!fileInput.files.length;
    }
  }
  async function withImportLock(action, onBusy = () => {}) {
    await ResumeAuth.ready();
    if (!navigator.locks) throw new Error('Import recovery requires a browser with Web Locks support.');
    return navigator.locks.request('resumeops-import:' + ResumeAuth.owner, {ifAvailable:true},
      lock => lock ? action() : onBusy());
  }

  button.addEventListener('click',async () => {
    if(busy || !fileInput.files.length) return;
    const file=fileInput.files[0];
    const extension=extensionOf(file);
    const max=extension==='.json'?1024*1024:12*1024*1024;
    if(!allowed.has(extension)){
      message('Unsupported file type. Choose PDF, Word, TXT, Markdown, image or candidate JSON.',true);
      return;
    }
    if(!file.size || file.size>max){
      message('File must be non-empty and no larger than '+(max/1048576)+' MB.',true);
      return;
    }
    try {
      await withImportLock(async () => {
        const pending = await ResumeRecovery.read('import');
        if(pending) {
          await run(pending);
          return;
        }
        const work={id:crypto.randomUUID().replaceAll('-',''),extension,file};
        await ResumeRecovery.put('import',work);
        await ResumeRecovery.remove('selected-import');
        await run(work);
      }, () => message('Import open in another tab. Continue there or close it to resume here.', true));
    } catch(error) {
      message('Cannot save this upload for recovery. Check browser storage and retry.',true);
    }
  });
  document.addEventListener('resumeops-builder-ready',async () => {
    try {
      const work=await ResumeRecovery.read('import');
      if (work) {
        await withImportLock(async () => {
          const current = await ResumeRecovery.read('import');
          if (current) await run(current, true);
        });
      } else {
        const selected = await ResumeRecovery.read('selected-import');
        if (selected && !fileInput.files.length) {
          const files = new DataTransfer();
          files.items.add(selected);
          fileInput.files = files.files;
          button.disabled = false;
        }
      }
    } catch {
      message('Could not restore the saved upload. Check browser storage.',true);
    }
  },{once:true});
})();

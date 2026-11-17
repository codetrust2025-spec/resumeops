/* Recoverable work is bounded to the authenticated account and this browser. */
'use strict';
const ResumeRecovery = (() => {
  const TTL = 24 * 60 * 60 * 1000;
  let database;
  function db() {
    if (!database) database = new Promise((resolve,reject) => {
      const req = indexedDB.open('resumeops-recovery',1);
      req.onupgradeneeded = () => req.result.createObjectStore('work');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return database;
  }
  async function run(mode, action) {
    const owner = ResumeAuth.owner || (await ResumeAuth.ready()).owner;
    const connection = await db();
    return new Promise((resolve,reject) => {
      const tx = connection.transaction('work',mode);
      const req = action(tx.objectStore('work'),owner);
      tx.oncomplete = () => resolve(req.result);
      tx.onabort = tx.onerror = () => reject(tx.error || req.error);
    });
  }
  async function read(key) {
    const value=await run('readonly',(store,owner) => store.get(owner+':'+key));
    if(!value) return null;
    if(Date.now()-value.saved>TTL) {await remove(key);return null;}
    return value.value;
  }
  const put = (key,value) => run('readwrite',(store,owner) => store.put({saved:Date.now(),value},owner+':'+key));
  const remove = key => run('readwrite',(store,owner) => store.delete(owner+':'+key));
  async function download(action) {
    const result = await ResumeAuth.call(action.url);
    if (!result.ok) throw new Error('Download is unavailable. Try again after signing in.');
    const blob = await result.blob();
    if (!blob.size) throw new Error('The download was empty.');
    const url=URL.createObjectURL(blob);
    const link=document.createElement('a');
    link.href=url; link.download=action.name;
    document.body.append(link);link.click();link.remove();
    setTimeout(() => URL.revokeObjectURL(url),15000);
    await remove('download');
  }
  function protectDownload(link,name) {
    link.addEventListener('click',async event => {
      event.preventDefault();
      const action={url:link.getAttribute('href'),name};
      try { await put('download',action); await download(action); }
      catch(error) { document.getElementById('result-name').textContent=error.message; }
    });
  }
  return {read,put,remove,download,protectDownload};
})();

'use strict';

function setupModelSelector() {
  const get = name => document.getElementById('ollama-model-' + name);
  const panel = get('settings');
  if (!panel || panel.hidden) return;
  const select = document.getElementById('ollama-model');
  const save = get('save');
  const reload = get('refresh');
  const status = get('status');
  const summary = get('summary');
  let state;

  function size(bytes) {
    if (!Number.isFinite(bytes)) return 'size unavailable';
    const unit = bytes >= 1024 ** 3 ? 'GiB' : 'MiB';
    const scale = unit === 'GiB' ? 1024 ** 3 : 1024 ** 2;
    return `${(bytes / scale).toLocaleString(undefined, {maximumFractionDigits: 2})} ${unit}`;
  }

  function showModelInfo() {
    const name = select.value || state.default;
    const model = state.models?.find(item => item.name === name || item.name === `${name}:latest`);
    get('info').textContent = model ? `Installed size: ${size(model.size)}. Digest: ${model.digest || 'unavailable'}.` : '';
  }

  function render() {
    const {selected, default: fallback, models, loaded, selection_error: error} = state;
    const choices = [new Option(`Service default (${fallback})`, '')];
    for (const model of models || []) choices.push(new Option(`${model.name} · ${size(model.size)}`, model.name));
    if (selected && !models?.some(model => model.name === selected)) {
      const unavailable = new Option(`${selected} (unavailable)`, selected);
      unavailable.disabled = true;
      choices.push(unavailable);
    }
    select.replaceChildren(...choices);
    select.value = selected || '';
    const current = error ? 'Model preference needs attention' : `Current model: ${selected || fallback}`;
    if (summary) summary.textContent = current;
    status.textContent = error || `${current}.`;
    if (models === null) status.textContent += ' Installed model inventory is unavailable.';
    else if (!models.length) status.textContent += ' No local models are installed.';
    else if (selected && !models.some(model => model.name === selected)) status.textContent += ' The saved model is no longer installed.';
    get('loaded').textContent = loaded === null ? 'Loaded model status is unavailable.' : loaded.length ?
      'Loaded in Ollama: ' + loaded.map(model => `${model.name}${model.size_vram === null ? '' : ` (${size(model.size_vram)} in VRAM)`}`).join(', ') :
      'No local models are loaded in Ollama.';
    showModelInfo();
  }

  function setBusy(busy) {
    reload.disabled = busy;
    select.disabled = busy || !state || (!state.selection_error && !state.selected && !state.models?.length);
    save.disabled = select.disabled || (!state.selection_error && select.value === (state.selected || ''));
  }

  async function request(init) {
    const response = await ResumeAuth.call('/api/ollama-model', init);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not load Ollama model settings.');
    return result;
  }

  async function load() {
    setBusy(true);
    try {
      state = await request();
      render();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      setBusy(false);
    }
  }

  select.addEventListener('change', () => { showModelInfo(); setBusy(false); });
  reload.addEventListener('click', load);
  get('form').addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled) return;
    const model = select.value || null;
    setBusy(true);
    try {
      const saved = await request({method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model})});
      state.selected = saved.selected;
      state.selection_error = null;
      render();
      status.textContent = `Saved. ${status.textContent}`;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      setBusy(false);
    }
  });
  if (panel instanceof HTMLDetailsElement) {
    panel.addEventListener('toggle', function open() {
      if (!panel.open) return;
      panel.removeEventListener('toggle', open);
      load();
    });
  } else {
    load();
  }
}

setupModelSelector();

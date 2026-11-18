'use strict';

const $ = id => document.getElementById(id);
const EXAMPLE = {version: 3, aliases: {}, roles: [{id: 'kotlin-backend-developer', name: 'Kotlin Backend Developer',
  family: 'backend-kotlin', titleVariations: ['Kotlin Developer'],
  groups: {Languages: ['Kotlin', 'SQL'], Frameworks: ['Spring Boot', 'Ktor'], Databases: ['PostgreSQL'],
    Testing: ['JUnit', 'MockK'], Concepts: ['REST APIs', 'Coroutines']},
  projectThemes: ['Booking REST API with Ktor and PostgreSQL']}]};

async function load() {
  const response = await ResumeAuth.call('/api/roles');
  const result = await response.json();
  if (!response.ok) { $('roles-status').textContent = result.error || 'Unavailable.'; return; }
  $('roles-file').textContent = result.file;
  $('roles-status').textContent = 'Source: ' + result.status.source + (result.status.error ? ' — ' + result.status.error : '');
  $('roles-list').replaceChildren(...result.roles.map(role => {
    const item = document.createElement('li');
    item.textContent = role.name + ' (' + role.id + ')' + (role.hidden ? ' — hidden' : '') + ': ' + role.keywords.length + ' keywords';
    return item;
  }));
  if (!$('roles-json').value) $('roles-json').value = JSON.stringify(result.managed || EXAMPLE, null, 1);
}

async function save() {
  const error = $('roles-error');
  error.hidden = true;
  let catalog;
  try { catalog = JSON.parse($('roles-json').value); }
  catch (exc) { error.textContent = 'Not valid JSON: ' + exc.message; error.hidden = false; return; }
  const response = await ResumeAuth.call('/api/roles', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(catalog)});
  const result = await response.json();
  if (!response.ok) { error.textContent = result.error; error.hidden = false; return; }
  await load();
  $('roles-status').textContent += ' — saved ' + result.saved + ' managed roles.';
}

(async () => {
  await ResumeAuth.ready();
  $('roles-save').addEventListener('click', () => save().catch(exc => { $('roles-error').textContent = exc.message; $('roles-error').hidden = false; }));
  await load();
})();

/* ResumeOps UI logic shared by the browser and the Node component tests. No DOM access here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ResumeCore = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const PROFILE_FIELDS = ['full_name', 'headline', 'email', 'phone', 'location', 'linkedin', 'github', 'portfolio', 'summary'];
  const STEPS = ['Personal details', 'Experience', 'Target role', 'Review & ATS', 'Generate'];
  const PIPELINE = [
    [10, 'Candidate input validated'],
    [20, 'Role and keyword analysis'],
    [40, 'Ollama processing'],
    [50, 'Content checked against source'],
    [60, 'ATS coverage calculated'],
    [70, 'LaTeX created'],
    [80, 'PDF compiled and verified'],
    [90, 'Overleaf verification'],
    [100, 'Verified PDF ready'],
  ];
  const ACTION_VERBS = ['developed', 'implemented', 'designed', 'optimized', 'built', 'created', 'managed',
    'led', 'integrated', 'automated', 'improved', 'reduced', 'delivered', 'migrated', 'maintained',
    'reviewed', 'tested', 'deployed', 'worked', 'collaborated'];

  // Synthetic sample used for template previews. Not a real person.
  const SAMPLE = {
    full_name: 'Asha Example', headline: 'Frontend Software Engineer', email: 'asha.example@example.com',
    phone: '+91 90000 00000', location: 'Hyderabad, India', linkedin: '', github: '',
    summary: 'Frontend engineer with experience building accessible web interfaces and reusable React components.',
    skills: ['JavaScript', 'TypeScript', 'React', 'HTML', 'CSS', 'Git', 'REST APIs'],
    experience: [{company: 'Example Digital Labs', position: 'Software Engineer', start: 'Jun 2023', end: 'Present',
      location: 'Hyderabad', bullets: ['Developed reusable React components for internal web applications.',
        'Integrated REST APIs and fixed UI bugs reported during testing.']}],
    education: [{institution: 'Example Institute of Technology', degree: 'B.Tech in Information Technology',
      start: '2017', end: '2021', details: ''}],
    certifications: [],
  };

  const lines = value => String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  const list = value => Array.isArray(value) ? value.map(item => String(item).trim()).filter(Boolean)
    : String(value || '').split(/\r?\n|,/).map(item => item.trim()).filter(Boolean);

  function emptyState() {
    return {
      profile: Object.fromEntries(PROFILE_FIELDS.map(key => [key, ''])),
      skills: '', certifications: '', experience: [], education: [], projects: [],
      role: '', template: 'standard', keywords: [], jobDescription: '', verified: false,
      source: '', sourceKind: 'manual',
    };
  }

  function stateFromProfile(profile, source) {
    const state = emptyState();
    for (const key of PROFILE_FIELDS) state.profile[key] = profile[key] || '';
    state.skills = (profile.skills || []).join('\n');
    state.certifications = (profile.certifications || []).join('\n');
    state.experience = (profile.experience || []).map(item => ({...item, bullets: (item.bullets || []).join('\n')}));
    state.education = (profile.education || []).map(item => ({...item}));
    state.projects = (profile.projects || []).map(item => ({name: item.name || '', link: item.link || '',
      technologies: (item.technologies || []).join(', '), bullets: (item.bullets || []).join('\n')}));
    if (source) { state.source = source; state.sourceKind = 'import'; }
    return state;
  }

  function buildProfile(state) {
    const profile = {};
    for (const key of PROFILE_FIELDS) {
      const value = String(state.profile[key] || '').trim();
      if (key !== 'portfolio' || value) profile[key] = value;
    }
    profile.skills = list(state.skills);
    profile.certifications = lines(state.certifications);
    profile.experience = state.experience.map(item => ({
      company: (item.company || '').trim(), position: (item.position || '').trim(), start: (item.start || '').trim(),
      end: (item.end || '').trim(), location: (item.location || '').trim(), bullets: lines(item.bullets)}));
    profile.education = state.education.map(item => ({
      institution: (item.institution || '').trim(), degree: (item.degree || '').trim(), start: (item.start || '').trim(),
      end: (item.end || '').trim(), details: (item.details || '').trim()}));
    const projects = state.projects.map(item => ({name: (item.name || '').trim(), link: (item.link || '').trim(),
      technologies: list(item.technologies), bullets: lines(item.bullets)}));
    if (projects.length) profile.projects = projects;
    return profile;
  }

  function buildRequest(state) {
    return {profile: buildProfile(state),
      target: {role: state.role, template: state.template || 'standard', keywords: [...state.keywords]}};
  }

  const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  function parseMonth(value, now) {
    const text = String(value || '').trim().toLowerCase();
    if (!text) return null;
    if (['present', 'current', 'now', 'ongoing'].includes(text)) return now ? monthIndex(now) : null;
    let match = text.match(/^(\d{4})-(\d{1,2})(?:-\d{1,2})?$/);
    if (match) return Number(match[1]) * 12 + Number(match[2]) - 1;
    match = text.match(/^(?:\d{1,2}\s+)?([a-z]{3})[a-z]*\.?\s+(\d{4})$/);
    if (match && MONTHS.includes(match[1])) return Number(match[2]) * 12 + MONTHS.indexOf(match[1]);
    match = text.match(/^(\d{4})$/);
    if (match) return Number(match[1]) * 12;
    return null;
  }
  const monthIndex = date => date.getFullYear() * 12 + date.getMonth();

  /** Years of experience derived from the candidate's own dated roles (overlaps counted once). */
  function experienceYears(experience, now = new Date()) {
    const ranges = [];
    for (const item of experience || []) {
      const start = parseMonth(item.start);
      const end = item.end ? parseMonth(item.end, now) : parseMonth('present', now);
      if (start !== null && end !== null && end >= start) ranges.push([start, end + 1]);
    }
    ranges.sort((a, b) => a[0] - b[0]);
    let months = 0; let cursor = -Infinity;
    for (const [start, end] of ranges) {
      const from = Math.max(start, cursor);
      if (end > from) months += end - from;
      cursor = Math.max(cursor, end);
    }
    return Math.round(months / 12 * 10) / 10;
  }

  function experienceLevel(years) {
    if (years < 2) return 'Fresher / Junior';
    if (years < 5) return 'Mid-level';
    if (years < 10) return 'Senior';
    return 'Lead / Principal';
  }

  /** Mirrors resume_bot/profile.py so problems show before the server is asked. */
  function validate(state, step = 5) {
    const errors = [];
    const p = state.profile;
    const blank = value => !String(value || '').trim() || ['n/a', 'unknown', 'tbd', 'your name'].includes(String(value).trim().toLowerCase());
    if (step >= 1) {
      if (blank(p.full_name)) errors.push({field: 'full_name', message: 'Full name is required.'});
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(p.email || '').trim())) errors.push({field: 'email', message: 'Enter a valid email address.'});
      for (const key of ['linkedin', 'github', 'portfolio']) {
        if (p[key] && /\s/.test(p[key].trim())) errors.push({field: key, message: 'Links cannot contain spaces.'});
      }
    }
    if (step >= 2) {
      if (!state.experience.length && !state.education.length) errors.push({field: 'experience', message: 'Add at least one experience or education record.'});
      state.experience.forEach((item, index) => {
        for (const [key, label] of [['company', 'Employer'], ['position', 'Job title'], ['start', 'Start date']]) {
          if (blank(item[key])) errors.push({field: `experience.${index}.${key}`, message: `Experience ${index + 1}: ${label} is required.`});
        }
        const start = parseMonth(item.start); const end = parseMonth(item.end);
        if (start !== null && end !== null && end < start) errors.push({field: `experience.${index}.end`, message: `Experience ${index + 1}: end date is before the start date.`});
      });
      state.education.forEach((item, index) => {
        for (const [key, label] of [['institution', 'Institution'], ['degree', 'Degree']]) {
          if (blank(item[key])) errors.push({field: `education.${index}.${key}`, message: `Education ${index + 1}: ${label} is required.`});
        }
      });
      state.projects.forEach((item, index) => {
        if (blank(item.name)) errors.push({field: `projects.${index}.name`, message: `Project ${index + 1}: name is required.`});
      });
      const skills = list(state.skills).map(item => item.toLowerCase());
      const duplicate = skills.find((item, index) => skills.indexOf(item) !== index);
      if (duplicate) errors.push({field: 'skills', message: `Remove the duplicate skill "${duplicate}".`});
    }
    if (step >= 3) {
      if (!state.role) errors.push({field: 'role', message: 'Select a target role.'});
      if (state.keywords.length > 50) errors.push({field: 'keywords', message: 'Use up to 50 target keywords.'});
    }
    if (step >= 5 && !state.verified) errors.push({field: 'verified', message: 'Confirm the candidate facts are accurate before generating.'});
    return errors;
  }

  function addKeywords(current, incoming) {
    const seen = new Set(current.map(item => item.toLowerCase()));
    const out = [...current];
    for (const term of incoming) {
      const clean = String(term).trim();
      if (clean && clean.length <= 100 && !seen.has(clean.toLowerCase()) && out.length < 50) {
        seen.add(clean.toLowerCase());
        out.push(clean);
      }
    }
    return out;
  }

  function contentSignals(profile) {
    const bullets = [...profile.experience.flatMap(item => item.bullets),
      ...(profile.projects || []).flatMap(item => item.bullets)];
    if (!bullets.length) return {bullets: 0, quantified: 0, actionVerbs: 0};
    return {bullets: bullets.length,
      quantified: bullets.filter(item => /\d/.test(item)).length,
      actionVerbs: bullets.filter(item => ACTION_VERBS.includes(item.split(/\s+/)[0].toLowerCase().replace(/[,.]$/, ''))).length};
  }

  function scoreLabel(score) {
    if (score >= 90) return 'Excellent';
    if (score >= 80) return 'Good';
    if (score >= 70) return 'Fair';
    return 'Needs improvement';
  }

  function stageIndex(percent) {
    let index = -1;
    PIPELINE.forEach(([value], position) => { if (percent >= value) index = position; });
    return index;
  }

  function activeJob(jobs) {
    return jobs.find(item => item.status === 'processing') || jobs.find(item => item.status === 'failed') ||
      jobs.find(item => item.status === 'queued') || jobs.find(item => item.status === 'ok' && item.percent === 100) || null;
  }

  function jobProgress(job) {
    if (!job) return {percent: 0, label: 'Not queued', state: 'idle'};
    if (job.status === 'queued') return {percent: 0, label: 'Queued · waiting for the watcher', state: 'queued'};
    if (job.status === 'failed') return {percent: job.percent || 0,
      label: (job.blocked ? 'Blocked · retrying automatically' : 'Awaiting retry') + (job.error ? ' · ' + job.error : ''), state: 'failed'};
    if (job.status === 'ok') return {percent: 100, label: job.stage || 'Verified PDF ready to download', state: 'ok'};
    return {percent: job.percent || 0, label: job.stage || 'Processing', state: 'processing'};
  }

  function initials(name) {
    return String(name || '').split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0].toUpperCase()).join('') || '—';
  }

  return {PROFILE_FIELDS, STEPS, PIPELINE, SAMPLE, lines, list, emptyState, stateFromProfile, buildProfile,
    buildRequest, parseMonth, experienceYears, experienceLevel, validate, addKeywords, contentSignals,
    scoreLabel, stageIndex, activeJob, jobProgress, initials};
}));

const $ = (selector) => document.querySelector(selector);
const icons = {
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 13h8M8 17h5"/>',
  spark: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8.5 12 2.3 2.3 4.7-4.7"/>',
  git: '<circle cx="7" cy="6" r="2"/><circle cx="17" cy="6" r="2"/><circle cx="7" cy="18" r="2"/><path d="M7 8v8m10-8c0 6-10 2-10 6"/>',
  refresh: '<path d="M20 10a8 8 0 1 0-1 6M20 4v6h-6"/>',
  check: '<path d="m5 12 4 4L20 5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  list: '<path d="M9 5h12M9 12h12M9 19h12"/><rect x="3" y="4" width="2" height="2" rx=".5"/><rect x="3" y="11" width="2" height="2" rx=".5"/><rect x="3" y="18" width="2" height="2" rx=".5"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const humanize = (value) => value.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const relative = (date) => {
  const seconds = Math.max(0, (Date.now() - Date.parse(date)) / 1000);
  return seconds < 60 ? 'just now' : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : new Date(date).toLocaleDateString();
};
function savedLayout() {
  try { return localStorage.getItem('farm-result-layout') === 'list' ? 'list' : 'grid'; } catch { return 'grid'; }
}
const state = { view: 'discover', repositories: [], jobs: [], status: null, results: null, resultsTechnology: '', query: '', technology: '', searchVersion: 0, searchLoading: false, layout: savedLayout(), detailId: null, cloneId: null, lastSignature: null, jobsSignature: '', technologySignature: null, toastTimer: null };
const assistant = { status: null, response: null, error: '', notice: '', busy: false, checking: false, version: 0, controller: null, timer: null, startedAt: 0 };
const starters = [
  {repository:'oracle-livelabs/database', component:'ai-vector-rag'},
  {repository:'oracle-livelabs/developer', component:'db-operator-k8s'},
  {repository:'oracle-livelabs/adb', component:'rest-services-for-adb'},
  {repository:'oracle-livelabs/analytics-ai', component:'generative-ai-rag-llama-index'},
  {repository:'oracle-livelabs/aichatbot', component:''},
];

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `Request failed (${response.status}). Check the input and retry.`);
  }
  return response.json();
}
const post = (path, body = {}) => api(path, {method:'POST', body:JSON.stringify(body)});
function toast(message, error = false) {
  clearTimeout(state.toastTimer);
  $('#toast').textContent = message;
  $('#toast').className = `toast${error ? ' error' : ''}`;
  state.toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 6500);
}
function setView(view) {
  state.view = view;
  document.querySelectorAll('.view').forEach((item) => item.classList.toggle('hidden', item.id !== `${view}-view`));
  document.querySelectorAll('[data-view]').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === view);
    if (item.dataset.view === view) item.setAttribute('aria-current','page'); else item.removeAttribute('aria-current');
  });
  $('#page-name').textContent = {discover:'Discover',library:'Repository library',activity:'Index activity'}[view];
  if (view === 'library') renderLibrary();
  if (view === 'activity') renderJobs();
  window.scrollTo({top:0,behavior:'instant'});
}

function focusSearch() {
  if (state.view !== 'discover') setView('discover');
  $('#query').focus();
}
function renderStatus() {
  const status = state.status;
  if (!status) return;
  $('#stat-repos').textContent = status.repositories.toLocaleString();
  $('#stat-sections').textContent = status.sections.toLocaleString();
  $('#library-count').textContent = status.repositories;
  const pending = state.jobs.filter((job) => ['queued','running'].includes(job.status)).length;
  $('#activity-count').textContent = pending;
  $('#activity-count').classList.toggle('hidden', !pending);
  const modelJob = state.jobs.some((job) => job.kind === 'model' && ['queued','running'].includes(job.status));
  $('#stat-mode').textContent = status.model.ready ? 'Hybrid' : 'Keyword';
  $('#search-mode').textContent = status.model.ready ? 'Semantic + keyword search · evidence from your indexed files' : 'Keyword search · set up the local model for semantic matching';
  $('#engine-name').textContent = status.model.ready ? 'Semantic engine ready' : modelJob ? 'Preparing local model' : 'Keyword search ready';
  $('#engine-dot').classList.toggle('ready', status.model.ready);
  $('#engine-description').textContent = status.model.ready ? `${status.embedded_sections} sections embedded locally. Your queries stay on this machine.` : modelJob ? 'Loading the model and embedding your catalog. Follow Index activity for progress.' : 'Add the local embedding model to search by meaning as well as exact terms.';
  $('#prepare-model').textContent = status.model.ready ? 'Recheck document embeddings ↗' : modelJob ? 'Model setup in progress…' : 'Set up semantic search ↗';
  $('#prepare-model').disabled = modelJob;
  const technologySignature = JSON.stringify(status.technologies);
  if (technologySignature !== state.technologySignature) {
    $('#technology').innerHTML = '<option value="">All technologies</option>' + status.technologies.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
    $('#technology').value = state.technology;
    state.technologySignature = technologySignature;
  }
}

function renderCatalogScene() {
  $('#scene-count').textContent = state.repositories.length.toLocaleString();
  // A browsing constellation, using actual catalog entries rather than inferred relationships.
  const selected = [];
  const seen = new Set();
  for (const repository of state.repositories) {
    const topic = repository.manifest.technologies[0]?.name || repository.full_name;
    if (seen.has(topic)) continue;
    selected.push(repository); seen.add(topic);
    if (selected.length === 6) break;
  }
  for (const repository of state.repositories) {
    if (selected.length === 6) break;
    if (!selected.includes(repository)) selected.push(repository);
  }
  $('#catalog-nodes').innerHTML = selected.map((repository, index) => {
    const m = repository.manifest;
    const name = humanize(m.name);
    return `<button type="button" class="catalog-node node-${index + 1}" data-detail="${esc(repository.id)}" aria-label="Explore ${esc(repository.full_name)}${repository.component ? ' / ' + esc(repository.component) : ''}" title="${esc(repository.full_name)}"><span class="node-icon">${icon('git')}</span><span class="node-label">${esc(name)}</span><span class="node-subtitle">${esc(m.technologies[0]?.name || 'Source evidence')}</span></button>`;
  }).join('');
  $('#catalog-scene').classList.toggle('is-empty', !selected.length);
}

function renderTopics() {
  const counts = new Map();
  for (const repository of state.repositories) {
    for (const technology of new Set(repository.manifest.technologies.map((t) => t.name))) counts.set(technology, (counts.get(technology) || 0) + 1);
  }
  const popular = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 6);
  if (state.technology && !popular.some(([name]) => name === state.technology) && counts.has(state.technology)) popular.push([state.technology, counts.get(state.technology)]);
  $('#topic-filters').innerHTML = '<span class="topic-filter-label">Explore by technology</span>' + [['', state.repositories.length], ...popular].map(([name, count]) => `<button type="button" class="topic-chip${state.technology === name ? ' active' : ''}" data-technology="${esc(name)}" aria-pressed="${state.technology === name}">${esc(name || 'All sources')}<span>${count}</span></button>`).join('');
}

function applyLayout(layout) {
  state.layout = layout === 'list' ? 'list' : 'grid';
  $('#results').classList.toggle('is-list', state.layout === 'list');
  document.querySelectorAll('[data-layout]').forEach((button) => {
    const active = button.dataset.layout === state.layout;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  try { localStorage.setItem('farm-result-layout', state.layout); } catch { /* The view still works when storage is disabled. */ }
}

function renderSearchLoading() {
  $('#results').setAttribute('aria-busy', 'true');
  $('#search-summary').classList.remove('hidden');
  $('#search-summary').textContent = 'Finding relevant passages across your indexed sources…';
  $('#results').innerHTML = Array.from({length: 4}, () => '<article class="skeleton-card" aria-hidden="true"><div class="skeleton-icon"></div><div class="skeleton-line short"></div><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line medium"></div></article>').join('');
}
function empty(title, description, actions = '') {
  return `<div class="empty-state"><div class="empty-icon">${icon('layers')}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${actions}</div>`;
}
function card(repository, index = 0) {
  const m = repository.manifest;
  const proof = repository.evidence?.find((e) => e.kind === 'source') || repository.evidence?.[0];
  const capabilities = m.capabilities.slice(0, 2);
  return `<article class="result-card"><div class="card-top"><span class="repo-icon">${icon('git')}</span><div><div class="card-kicker">${m.component_path ? 'Reusable component' : 'Repository'}</div><div class="card-owner" title="${esc(repository.full_name)}">${esc(repository.full_name)}</div></div>${repository.match_type ? `<span class="result-rank" aria-label="Result ${index + 1}">${String(index + 1).padStart(2, '0')}</span>` : '<span class="catalog-label">INDEXED SOURCE</span>'}</div><h3><button data-detail="${esc(repository.id)}">${esc(humanize(m.name))}</button></h3><p class="card-description">${esc(m.description)}</p>${repository.match_type ? `<div class="card-signal">${icon('search')}<span>${esc(repository.match_type)} match</span><span class="signal-note">Review source fit</span></div>` : ''}${proof ? `<div class="evidence-preview"><span class="evidence-label">${icon('file')}${proof.kind === 'manifest' ? 'Indexed overview' : 'Source excerpt'}</span><p>${esc(proof.excerpt.slice(0,230))}${proof.excerpt.length > 230 ? '…' : ''}</p><a href="${esc(proof.url)}" target="_blank" rel="noopener noreferrer">${esc(proof.path === '@manifest' ? 'View source documentation' : proof.path)} <span>↗</span></a></div>` : capabilities.length ? `<div class="card-capabilities"><span class="evidence-label">Documented topics</span>${capabilities.map((c) => `<span class="capability-tag">${icon('check')}${esc(c.name)}</span>`).join('')}</div>` : ''}<div class="tags">${m.technologies.slice(0,5).map((t) => `<span class="tag">${esc(t.name)}</span>`).join('')}${!m.technologies.length ? '<span class="tag">Documentation indexed</span>' : ''}</div><div class="card-bottom"><span class="card-meta">${icon('file')}${repository.section_count} sections <span>·</span><span title="Indexed commit ${esc(repository.commit_sha)}">${esc(repository.commit_sha.slice(0,7))}</span></span><button class="text-button" data-detail="${esc(repository.id)}">Explore solution <span>↗</span></button></div></article>`;
}
function renderResults() {
  $('#results').setAttribute('aria-busy', 'false');
  applyLayout(state.layout);
  $('#clear-search').classList.toggle('hidden', !state.results);
  $('#search-summary').classList.toggle('hidden', !state.results);
  $('#result-eyebrow').textContent = state.results ? 'MATCHED TO YOUR REQUIREMENT' : 'EXPLORE YOUR CATALOG';
  $('#result-heading').textContent = state.results ? `${state.results.total} solution${state.results.total === 1 ? '' : 's'} to explore` : 'A few places to start';
  if (state.results) $('#search-summary').textContent = `“${state.results.query}” · ${state.resultsTechnology || 'All technologies'} · ${state.results.mode === 'hybrid' ? 'Semantic + keyword search' : 'Keyword search'} · ${state.results.duration_ms} ms. Ranked by relevance; review the evidence to confirm suitability.`;
  renderAssistant();
  let repositories = state.results ? state.results.results : state.repositories.filter((r) => !state.technology || r.manifest.technologies.some((t) => t.name === state.technology));
  if (!repositories.length) {
    const pending = state.jobs.some((job) => job.kind === 'index' && ['queued','running'].includes(job.status));
    $('#results').innerHTML = state.repositories.length ? empty('No strong matches in this scope.', 'Try broader terms, change the technology filter, or index a more relevant repository. Missing evidence does not prove a feature is absent.') : pending ? empty('Your catalog is growing.', 'The indexer is reading source files and creating local embeddings. You can follow each step in Index activity.', '<button class="primary" data-view="activity">View index activity ↗</button>') : empty('Plant the first seed.', 'Add a public GitHub repository, or start with five selected Oracle LiveLabs examples. Every result will come from real indexed documentation.', '<button class="primary" data-action="add">＋ Add repository</button><button class="secondary" data-action="starters">Try Oracle LiveLabs ↗</button>');
    return;
  }
  $('#results').innerHTML = repositories.map(card).join('');
}
function cancelAssistant(notice = '') {
  assistant.version++;
  assistant.controller?.abort();
  clearInterval(assistant.timer);
  assistant.controller = null;
  assistant.timer = null;
  assistant.busy = false;
  assistant.notice = notice;
}
function resetAssistant() {
  cancelAssistant();
  assistant.response = null;
  assistant.error = '';
}
function sourceLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}
function renderAssistant() {
  const panel = $('#assistant-panel');
  panel.classList.toggle('hidden', !state.results);
  panel.setAttribute('aria-busy', String(assistant.busy));
  if (!state.results) return;
  const dirty = $('#query').value.trim() !== state.query || state.results.query !== state.query;
  const available = assistant.status?.available && assistant.status?.model_available;
  const badge = $('#assistant-model');
  badge.textContent = assistant.status ? `${assistant.status.model || 'Ollama'} · ${available ? 'local model ready' : 'unavailable'}` : 'Ollama connection not checked';
  badge.classList.toggle('ready', Boolean(available));
  $('#analyze-reuse').disabled = assistant.busy || assistant.checking || dirty;
  $('#analyze-reuse').innerHTML = assistant.busy ? '<span class="spinner" aria-hidden="true"></span> Analyzing locally…' : `${assistant.response || assistant.error ? 'Analyze again' : 'Analyze reuse with Ollama'} <span>↗</span>`;
  $('#cancel-analysis').classList.toggle('hidden', !assistant.busy);
  $('#check-assistant').disabled = assistant.checking || assistant.busy;
  $('#check-assistant').textContent = assistant.checking ? 'Checking connection…' : 'Check connection';
  $('#assistant-status').classList.toggle('text-error', Boolean(assistant.error));
  if (assistant.busy) {
    const elapsed = Math.floor((Date.now() - assistant.startedAt) / 10000) * 10;
    $('#assistant-status').textContent = `Reading your request and assessing retrieved passages on this machine. ${elapsed}s elapsed. The model may take a minute or more; you can keep exploring the results below.`;
  } else if (dirty) {
    $('#assistant-status').textContent = 'Your idea has changed. Select Find solutions to search and analyze the updated request.';
  } else if (assistant.error || assistant.notice) {
    $('#assistant-status').textContent = assistant.error || assistant.notice;
  } else if (assistant.response) {
    const result = assistant.response;
    $('#assistant-status').textContent = result.message || (result.status === 'complete' ? 'Analysis complete. Check the interpretation and its supporting sources below.' : 'The analysis is incomplete. The source search results below are still available.');
  } else if (assistant.checking) {
    $('#assistant-status').textContent = 'Checking the local Ollama service and configured model…';
  } else if (assistant.status && !available) {
    $('#assistant-status').textContent = assistant.status.message || 'Start Ollama and make sure the configured model is installed, then check the connection again. Search remains available.';
  } else {
    $('#assistant-status').textContent = assistant.status?.busy ? 'Another local analysis is running. Try again after it finishes.' : 'Choose Analyze reuse to run the local LLM. Search results are already available below.';
  }
  renderAssistantOutput();
}
function renderAssistantOutput() {
  const output = $('#assistant-output');
  const result = assistant.response;
  if (!result || assistant.busy || $('#query').value.trim() !== state.query) {
    if (assistant.renderedResponse) output.innerHTML = '';
    assistant.renderedResponse = null;
    return;
  }
  if (assistant.renderedResponse === result) return;
  assistant.renderedResponse = result;
  const requirements = Array.isArray(result.requirements) ? result.requirements : [];
  const repositories = Array.isArray(result.retrieval?.results) ? result.retrieval.results : [];
  const assessments = Array.isArray(result.assessments) ? result.assessments : [];
  const requirementMap = new Map(requirements.map((requirement) => [requirement.id, requirement]));
  const interpretation = requirements.length ? `<div class="assistant-interpretation"><h4>Check the interpretation</h4><p>These needs were extracted from your request. Edit your idea above and search again if anything is missing or misread.</p><div class="requirement-list">${requirements.map((requirement, index) => `<div class="requirement-item"><span class="requirement-number">${index + 1}</span><div><strong>${esc(requirement.text)}</strong><span class="requirement-kind">${esc(requirement.kind)}</span><span class="requirement-origin">From your request: “${esc(requirement.quote)}”</span></div></div>`).join('')}</div></div>` : '';
  const evidence = assessments.map((assessment) => {
    const repository = repositories.find((item) => item.id === assessment.repository_id) || state.repositories.find((item) => item.id === assessment.repository_id);
    if (!repository) return '';
    const findings = (Array.isArray(assessment.findings) ? assessment.findings : []).filter((finding) => requirementMap.has(finding.requirement_id));
    const supported = findings.filter((finding) => finding.status === 'evidence_found' && finding.citations?.length).length;
    return `<details class="assessment-card" open><summary><div><strong>${esc(repository.full_name)}</strong>${repository.component ? `<span class="assessment-component">${esc(repository.component)}</span>` : ''}</div><span class="assessment-count">${supported} of ${requirements.length} needs with cited evidence</span></summary><div class="assessment-body"><p class="assessment-scope">Assessment of retrieved documentation only. This is not a compatibility or behavior test.</p>${findings.map((finding) => {
      const requirement = requirementMap.get(finding.requirement_id);
      const citations = Array.isArray(finding.citations) ? finding.citations : [];
      const hasEvidence = finding.status === 'evidence_found' && citations.length > 0;
      return `<div class="requirement-finding ${hasEvidence ? 'has-evidence' : 'evidence-gap'}"><div class="finding-heading"><strong>${esc(requirement.text)}</strong><span>${hasEvidence ? 'Cited evidence' : 'No evidence found in retrieved passages'}</span></div>${hasEvidence && finding.explanation ? `<p class="finding-explanation"><span>AI interpretation</span>${esc(finding.explanation)}</p>` : ''}${hasEvidence ? citations.map((citation) => {
        const url = sourceLink(citation.url);
        const location = `${citation.path || 'Source'}${citation.line_start ? ':' + citation.line_start + (citation.line_end > citation.line_start ? '–' + citation.line_end : '') : ''}`;
        return `<blockquote class="assistant-citation"><p>${esc(citation.quote)}</p><cite>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(location)} ↗</a>` : esc(location)}</cite></blockquote>`;
      }).join('') : '<p class="finding-gap-note">Review more source files or confirm this requirement with the repository owner.</p>'}</div>`;
    }).join('')}<button class="text-button" data-detail="${esc(repository.id)}">Explore indexed solution <span>↗</span></button></div></details>`;
  }).join('');
  output.innerHTML = `${interpretation}${assessments.length ? '<div class="assistant-evidence-heading"><h4>Compare the evidence</h4><span>Source quotes beside each interpretation</span></div>' + evidence : ''}`;
}
async function checkAssistant(forceRefresh = false) {
  if (assistant.checking) return;
  assistant.error = '';
  assistant.notice = '';
  if (assistant.response?.status === 'unavailable') assistant.response = null;
  assistant.checking = true;
  renderAssistant();
  try {
    assistant.status = await api(`/api/assistant/status${forceRefresh ? '?refresh=true' : ''}`);
  } catch {
    assistant.status = { available: false, model_available: false, model: 'Ollama', message: 'Could not check Ollama. Make sure the local app and Ollama are running, then check the connection again.' };
  } finally { assistant.checking = false; renderAssistant(); }
}
async function analyzeReuse() {
  if (!state.results || assistant.busy || $('#query').value.trim() !== state.query || state.results.query !== state.query) return;
  cancelAssistant();
  const version = assistant.version;
  const searchVersion = state.searchVersion;
  assistant.response = null;
  assistant.error = '';
  assistant.busy = true;
  assistant.startedAt = Date.now();
  assistant.controller = new AbortController();
  assistant.timer = setInterval(renderAssistant, 1000);
  const requestTimeout = Number(assistant.status?.request_timeout_seconds);
  const waitLimit = Math.max(180000, (Number.isFinite(requestTimeout) && requestTimeout > 0 ? requestTimeout * 2000 : 120000) + 15000);
  const timeout = setTimeout(() => {
    if (version === assistant.version) { cancelAssistant('The analysis took too long. Search is still available. Check Ollama and try again after the current model request finishes.'); renderAssistant(); }
  }, waitLimit);
  renderAssistant();
  try {
    const result = await api('/api/assistant', { method: 'POST', body: JSON.stringify({ query: state.query, technology: state.technology }), signal: assistant.controller.signal });
    if (version !== assistant.version || searchVersion !== state.searchVersion) return;
    assistant.response = result;
  } catch (error) {
    if (version !== assistant.version || error.name === 'AbortError') return;
    assistant.error = error.message;
  } finally {
    clearTimeout(timeout);
    if (version === assistant.version) { clearInterval(assistant.timer); assistant.timer = null; assistant.controller = null; assistant.busy = false; renderAssistant(); }
  }
}
function renderLibrary() {
  $('#library-list').innerHTML = state.repositories.length ? state.repositories.map((r) => `<article class="library-row"><span class="repo-icon">${icon('layers')}</span><div class="library-main"><h3>${esc(r.full_name)}${r.component ? ' / ' + esc(r.component) : ''}</h3><p>${r.section_count} sections · ${r.manifest.coverage.files_indexed} files · commit ${esc(r.commit_sha.slice(0,8))} · indexed ${esc(relative(r.indexed_at))}</p></div><div class="row-actions"><button class="text-button" data-sync="${r.id}">${icon('refresh')}Sync</button><button class="text-button" data-detail="${r.id}">Explore ↗</button></div></article>`).join('') : empty('Your library starts here.', 'Choose the repositories or components you want to make discoverable.', '<button class="primary" data-action="add">＋ Add repository</button>');
}
function renderJobs() {
  $('#jobs-list').innerHTML = state.jobs.length ? state.jobs.map((job) => `<article class="job-row"><span class="repo-icon">${icon(job.kind === 'model' ? 'spark' : job.kind === 'clone' ? 'git' : 'refresh')}</span><div class="job-main"><strong>${esc(job.kind === 'model' ? 'Local semantic model' : job.kind === 'clone' ? 'Clone indexed source' : job.target + (job.component ? ' / ' + job.component : ''))}</strong><p>${esc(job.message)}</p><time datetime="${esc(job.created_at)}">${esc(new Date(job.created_at).toLocaleString())}</time>${job.status === 'failed' && job.kind !== 'clone' ? `<br><button class="text-button" data-retry="${job.id}">Retry ↗</button>` : ''}</div><span class="job-state ${esc(job.status)}">${esc(humanize(job.status))}</span></article>`).join('') : empty('A little quiet here.', 'Index activity will appear when you add a source, sync a repository, or prepare the embedding model.');
}
async function refresh() {
  const [status, repositories, jobs] = await Promise.all([api('/api/status'),api('/api/repositories'),api('/api/jobs')]);
  const signature = repositories.map((r) => r.id + r.commit_sha + r.section_count + r.indexed_at).join('|');
  const changed = signature !== state.lastSignature;
  const jobsSignature = JSON.stringify(jobs);
  const jobsChanged = jobsSignature !== state.jobsSignature;
  state.status = status; state.repositories = repositories; state.jobs = jobs;
  state.lastSignature = signature; state.jobsSignature = jobsSignature;
  renderStatus();
  if (changed) { renderCatalogScene(); renderTopics(); }
  if (state.view === 'activity' && jobsChanged) renderJobs();
  if (state.view === 'library' && changed) renderLibrary();
  if (state.searchLoading) return;
  if (!state.results && changed) renderResults();
  else if (changed && state.query && $('#query').value.trim() === state.query) await runSearch({scroll: false});
}
async function runSearch({scroll = true} = {}) {
  const query = $('#query').value.trim();
  if (query.length < 2) { toast('Describe your requirement in at least two characters.', true); return; }
  const version = ++state.searchVersion;
  const previousQuery = state.results?.query || '';
  const requestedTechnology = state.technology;
  state.searchLoading = true;
  resetAssistant();
  state.query = query;
  $('#assistant-panel').classList.add('hidden');
  $('#search-button').disabled = true;
  $('#search-button').textContent = 'Finding matches…';
  renderSearchLoading();
  try {
    const result = await api(`/api/search?q=${encodeURIComponent(query)}&technology=${encodeURIComponent(requestedTechnology)}`);
    if (version !== state.searchVersion) return;
    state.results = result;
    state.resultsTechnology = requestedTechnology;
    renderResults();
    if (scroll) $('#result-heading').closest('.section-heading')?.scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  } catch (error) {
    if (version === state.searchVersion) {
      state.query = previousQuery;
      if (state.results) {
        state.technology = state.resultsTechnology;
        $('#technology').value = state.technology;
        renderTopics();
      }
      renderResults();
      toast(error.message + (state.results ? ' Previous results are still shown.' : ''), true);
    }
  }
  finally { if (version === state.searchVersion) { state.searchLoading = false; $('#search-button').disabled = false; $('#search-button').innerHTML = 'Find solutions <span>↗</span>'; } }
}
async function showDetails(identifier) {
  state.detailId = identifier;
  $('#detail-content').innerHTML = '<h2 id="detail-title">Reading source evidence</h2><div class="loading-state"><span class="spinner" aria-hidden="true"></span><p role="status">Loading the indexed revision…</p></div>';
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  try {
    const r = await api(`/api/repositories/${identifier}`);
    if (state.detailId !== identifier) return;
    const m = r.manifest;
    $('#detail-content').innerHTML = `<h2 id="detail-title">${esc(humanize(m.name))}</h2><div class="detail-origin">${esc(r.full_name)}${r.component ? ' / ' + esc(r.component) : ''}</div><p class="detail-summary">${esc(m.description)}</p><div class="detail-actions"><a class="primary" href="${esc(m.repository_url + (r.component ? '/tree/' + r.commit_sha + '/' + r.component : ''))}" target="_blank" rel="noopener noreferrer">Open repository ↗</a><button class="secondary" data-clone="${r.id}">Clone indexed revision ↓</button><a class="secondary" href="/api/repositories/${r.id}/manifest" download>Download manifest ↓</a></div><div class="tags">${m.technologies.map((t) => `<span class="tag">${esc(t.name)}</span>`).join('')}</div><div class="detail-facts"><div><span>Indexed revision</span>${esc(r.commit_sha.slice(0,12))}</div><div><span>Documentation coverage</span>${m.coverage.files_indexed} of ${m.coverage.eligible_files} eligible files</div><div><span>Repository license</span>${esc(m.license)}</div></div><div class="detail-section"><h3>Capabilities mentioned in documentation</h3><p>These are source-backed mentions. Feature behavior has not been executed or verified.</p>${m.capabilities.length ? m.capabilities.map((c) => `<div class="capability-row"><strong>${esc(c.name)}</strong><a href="${esc(c.evidence.url)}" target="_blank" rel="noopener noreferrer">${esc(c.evidence.path)}:${c.evidence.line} ↗</a><blockquote>${esc(c.evidence.quote)}</blockquote></div>`).join('') : '<p>No capabilities were extracted by the current rules. Explore the source sections below.</p>'}</div>${m.dependencies.length ? `<div class="detail-section"><h3>Declared dependencies</h3><div class="tags">${m.dependencies.map((d) => `<a class="tag" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.name)}</a>`).join('')}</div></div>` : ''}<div class="detail-section"><h3>Indexed source files</h3>${m.sources.map((s) => `<div class="source-row"><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.path)} ↗</a><span>${esc(s.hash.slice(0,8))}</span></div>`).join('')}</div><div class="detail-section"><h3>Explore documentation sections</h3>${r.sections.filter((s) => s.path !== '@manifest').slice(0,15).map((s) => `<details class="capability-row"><summary>${esc(s.title)} <span class="optional">${esc(s.path)}:${s.line_start}</span></summary><p class="section-excerpt">${esc(s.body.split('\n').slice(1).join('\n'))}</p><a class="text-button" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">View exact source lines ↗</a></details>`).join('')}</div><div class="detail-section"><h3>Scope and limitations</h3><ul class="detail-limitations">${m.limitations.map((l) => `<li>${esc(l)}</li>`).join('')}</ul><p>Generated by ${esc(m.generator)} · Indexed ${esc(new Date(r.indexed_at).toLocaleString())}. Manifest extraction uses documented text and dependency files.</p></div>`;
  } catch (error) { if (state.detailId === identifier) $('#detail-content').innerHTML = `<h2 id="detail-title">Could not load this source</h2><p class="text-error">${esc(error.message)}</p>`; }
}
async function addStarters() {
  $('#add-starters').disabled = true;
  try {
    for (const source of starters) await post('/api/repositories', source);
    $('#add-dialog').close();
    toast('Five Oracle LiveLabs sources queued. Watch Index activity for progress.');
    await refresh();
    setView('activity');
  } finally { $('#add-starters').disabled = false; }
}

document.querySelectorAll('[data-icon]').forEach((element) => element.innerHTML = icon(element.dataset.icon));
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  try {
    if (button.id === 'focus-search') return focusSearch();
    if (button.dataset.layout) return applyLayout(button.dataset.layout);
    if (Object.hasOwn(button.dataset, 'technology')) {
      state.technology = button.dataset.technology;
      $('#technology').value = state.technology;
      renderTopics();
      if (state.query) return await runSearch();
      return renderResults();
    }
    if (button.dataset.view) return setView(button.dataset.view);
    if (button.dataset.close) return $('#' + button.dataset.close).close();
    if (button.dataset.action === 'add') return $('#add-dialog').showModal();
    if (button.dataset.action === 'starters' || button.id === 'add-starters') return await addStarters();
    if (button.dataset.action === 'refresh') return await refresh();
    if (button.dataset.query) { $('#query').value = button.dataset.query; return await runSearch(); }
    if (button.dataset.detail) return await showDetails(button.dataset.detail);
    if (button.dataset.sync) { await post(`/api/repositories/${button.dataset.sync}/sync`); toast('Sync queued.'); return await refresh(); }
    if (button.dataset.retry) {
      const job = state.jobs.find((j) => j.id === button.dataset.retry);
      if (job.kind === 'model') await post('/api/model/prepare'); else await post('/api/repositories',{repository:job.target,component:job.component});
      toast('Retry queued.'); return await refresh();
    }
    if (button.dataset.clone) {
      state.cloneId = button.dataset.clone;
      const r = state.repositories.find((r) => r.id === state.cloneId);
      $('#clone-description').textContent = `${r.full_name} at commit ${r.commit_sha.slice(0,12)}. This downloads the repository; component folders remain within the checkout.`;
      return $('#clone-dialog').showModal();
    }
    if (button.id === 'confirm-clone') { await post(`/api/repositories/${state.cloneId}/clone`); $('#clone-dialog').close(); $('#detail-dialog').close(); toast('Clone queued. The destination will appear in Index activity.'); setView('activity'); return await refresh(); }
    if (button.id === 'prepare-model') { await post('/api/model/prepare'); toast('Local model setup queued. First download may take a few minutes.'); return await refresh(); }
    if (button.id === 'analyze-reuse') return await analyzeReuse();
    if (button.id === 'check-assistant') return await checkAssistant(true);
    if (button.id === 'cancel-analysis') { cancelAssistant('Stopped waiting for this analysis. Ollama may still be completing it. You can keep exploring and try again after it finishes.'); renderAssistant(); return; }
    if (button.id === 'clear-search') { resetAssistant(); state.searchVersion++; state.searchLoading = false; state.query = ''; state.results = null; $('#query').value = ''; $('#search-button').disabled=false; $('#search-button').innerHTML='Find solutions <span>↗</span>'; renderResults(); }
  } catch (error) { toast(error.message,true); }
});
$('#search-form').addEventListener('submit', (event) => { event.preventDefault(); runSearch(); });
$('#query').addEventListener('input', () => { if ($('#query').value.trim() !== state.query) resetAssistant(); renderAssistant(); });
$('#technology').addEventListener('change', () => { state.technology = $('#technology').value; renderTopics(); if (state.query) runSearch(); else renderResults(); });
$('#detail-dialog').addEventListener('close', () => { state.detailId = null; });
document.addEventListener('keydown', (event) => {
  if (event.isComposing || document.querySelector('dialog[open]')) return;
  const editing = event.target.closest('input, textarea, select, [contenteditable="true"]');
  if (((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') || (event.key === '/' && !editing && !event.ctrlKey && !event.metaKey && !event.altKey)) {
    event.preventDefault();
    focusSearch();
  }
});
$('#add-form').addEventListener('submit', async (event) => {
  event.preventDefault(); $('#add-submit').disabled = true;
  try {
    await post('/api/repositories',{repository:$('#repository-url').value.trim(),component:$('#component-path').value.trim()});
    $('#add-dialog').close(); $('#add-form').reset(); toast('Repository queued for indexing.'); await refresh(); setView('activity');
  } catch (error) { toast(error.message,true); }
  finally { $('#add-submit').disabled = false; }
});
applyLayout(state.layout);
refresh().catch((error) => { toast('Could not connect to the local app. Refresh after the server starts.',true); $('#results').innerHTML=empty('Waiting for the local server.',error.message,'<button class="primary" data-action="refresh">Retry connection ↗</button>'); });
checkAssistant();
setInterval(() => { if (!document.hidden) refresh().catch(() => {}); },3000);

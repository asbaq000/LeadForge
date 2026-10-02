const $ = id => document.getElementById(id);
let tools = [], jobs = [], selected = null, logId = null, filter = '';
async function api(route, options) { const response = await fetch(route, options); const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Request failed'); return result; }
const post = (route, data, method = 'POST') => api(route, { method, headers: { 'Content-Type':'application/json' }, body:JSON.stringify(data) });
function el(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; setTimeout(() => $('toast').hidden = true, 4000); }
function drawCards() {
  $('cards').replaceChildren();
  const search = $('search').value.toLowerCase();
  const visible = tools.filter(t => (!filter || t.platform === filter) && `${t.name} ${t.description}`.toLowerCase().includes(search));
  for (const t of visible) {
    const card = el('article', undefined, 'card'); card.style.setProperty('--color',t.color); card.style.setProperty('--tint',t.tint);
    const top = el('div',undefined,'card-top'); top.append(el('div',t.icon,'platform-icon'),el('span',t.category.toUpperCase(),'badge'));
    const bottom = el('div',undefined,'card-bottom'); const button = el('button','Open scraper ↗'); button.onclick = () => openTool(t); bottom.append(el('span',`● Python tool · ${t.creator}`),button);
    card.append(top,el('h3',t.name),el('p',t.description),bottom); $('cards').append(card);
  }
  if (!visible.length) $('cards').append(el('p','No scrapers match your search.','empty'));
  $('breadcrumb').textContent = filter || 'All scrapers';
}
function values() { return Object.fromEntries([...$('fields').querySelectorAll('input')].map(i => [i.name,i.type === 'checkbox' ? i.checked : i.value])); }
function preview() {
  const v = values(), args = [...selected.command];
  for (const f of selected.fields) if (v[f.key]) { if (f.flag) args.push(f.flag); if (f.type !== 'checkbox') args.push(v[f.key]); }
  try { args.push(...JSON.parse($('extra').value)); } catch { /* Validation occurs on submit. */ }
  $('command-preview').textContent = 'python ' + args.map(a => JSON.stringify(a)).join(' ');
}
function tab(name) { for (const n of ['run','config','docs']) $(`${n}-tab`).hidden = n !== name; document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('selected',b.dataset.tab === name)); }
async function openTool(t) {
  selected = t; $('tool-watermark').textContent = `crafted by ${t.creator}`; $('tool-name').textContent = t.name; $('tool-category').textContent = t.category.toUpperCase(); $('tool-description').textContent = t.description;
  $('fields').replaceChildren(); $('run-error').textContent = ''; $('extra').value = '[]'; $('config-message').textContent = ''; $('config-content').value = '';
  for (const f of t.fields) {
    const label = el('label',undefined,`field${f.type === 'checkbox' ? ' checkbox' : ''}`), input = el('input');
    input.name = f.key; input.type = f.type || 'text'; input.required = !!f.required; input.placeholder = f.placeholder || ''; if (f.type === 'number') input.min = f.min ?? 0;
    if (f.type === 'checkbox') input.checked = !!f.default; else input.value = f.default ?? '';
    label.append(el('span',f.label),input); $('fields').append(label);
  }
  $('config-file').replaceChildren(); for (const file of t.configs || []) { const opt = el('option',file); opt.value = file; $('config-file').append(opt); }
  $('save-config').disabled = !(t.configs || []).length; tab('run'); preview(); $('tool-dialog').showModal();
  $('readme').textContent = 'Loading…';
  try { const result = await api(`/api/readme?id=${encodeURIComponent(t.id)}`); if (selected.id === t.id) $('readme').textContent = result.content; } catch (e) { $('readme').textContent = e.message; }
  if (t.configs?.length) await loadConfig(); else $('config-content').value = 'This scraper uses CLI arguments. See its README for additional options.';
}
async function loadConfig() {
  const id = selected.id, file = $('config-file').value; $('config-message').textContent = '';
  try { const result = await api(`/api/config?id=${encodeURIComponent(id)}&file=${encodeURIComponent(file)}`); if (selected.id === id && $('config-file').value === file) $('config-content').value = result.content; }
  catch (e) { $('config-message').textContent = e.message; }
}
function drawJobs() {
  $('active-count').textContent = jobs.filter(j => ['running','stopping'].includes(j.status)).length; $('done-count').textContent = jobs.filter(j => j.status === 'completed').length; $('run-total').textContent = jobs.length;
  $('jobs').replaceChildren();
  if (!jobs.length) { const empty = el('div',undefined,'empty'); empty.append(el('strong','A clear runway.'),el('span','Start a scraper and its activity will appear here.')); $('jobs').append(empty); }
  for (const j of [...jobs].reverse()) {
    const row = el('div',undefined,'job'), info = el('div',undefined,'job-info'); info.append(el('strong',j.name),el('small',new Date(j.started).toLocaleString()));
    const logs = el('button','View output'); logs.onclick = () => { logId = j.id; $('log-title').textContent = `${j.name} · ${j.status}`; $('log-content').textContent = j.log || 'Waiting for output…'; $('log-dialog').showModal(); };
    row.append(info,el('span',j.status,`status ${j.status}`),logs);
    if (j.status === 'running') { const stop = el('button','Stop'); stop.onclick = async () => { try { await post('/api/stop',{id:j.id}); await refresh(); } catch (e) { toast(e.message); } }; row.append(stop); }
    $('jobs').append(row);
  }
  if (logId && $('log-dialog').open) { const j = jobs.find(j => j.id === logId); if (j) { $('log-title').textContent = `${j.name} · ${j.status}`; const p = $('log-content'), atBottom = p.scrollHeight - p.scrollTop - p.clientHeight < 60; p.textContent = j.log || 'Waiting for output…'; if (atBottom) p.scrollTop = p.scrollHeight; } }
}
async function refresh() { try { jobs = await api('/api/jobs'); drawJobs(); } catch (e) { toast(e.message); } }
$('run-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $('run-error').textContent = '';
  try { const extra = JSON.parse($('extra').value); if (!Array.isArray(extra) || extra.some(x => typeof x !== 'string')) throw new Error('Additional arguments must be a JSON array of strings'); await post('/api/run',{ id:selected.id, values:values(), extra }); $('tool-dialog').close(); toast('Run started. Follow its output below.'); await refresh(); }
  catch (e) { $('run-error').textContent = e.message; } finally { button.disabled = false; }
};
$('save-config').onclick = async () => { try { await post('/api/config',{id:selected.id,file:$('config-file').value,content:$('config-content').value},'PUT'); $('config-message').textContent = 'Saved. Applies to the next run.'; } catch (e) { $('config-message').textContent = e.message; } };
$('config-file').onchange = loadConfig; $('fields').oninput = preview; $('extra').oninput = preview;
$('close-dialog').onclick = () => $('tool-dialog').close(); $('close-log').onclick = () => $('log-dialog').close();
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => tab(b.dataset.tab)); $('search').oninput = drawCards;
$('overview').onclick = () => { filter = ''; document.querySelectorAll('.nav').forEach(b => b.classList.remove('active')); $('overview').classList.add('active'); drawCards(); };
$('download-log').onclick = () => { const j = jobs.find(j => j.id === logId); if (!j) return; const url = URL.createObjectURL(new Blob([j.log],{type:'text/plain'})), a = el('a'); a.href = url; a.download = `${j.scraper}-${j.id}.log`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); };
async function init() {
  try {
    tools = await api('/api/scrapers'); $('total').textContent = tools.length; $('nav-count').textContent = tools.length;
    for (const name of [...new Set(tools.map(t => t.platform))]) { const t = tools.find(t => t.platform === name), b = el('button',undefined,'nav'); b.append(el('span',t.icon),el('span',name)); b.onclick = () => { filter = name; document.querySelectorAll('.nav').forEach(x => x.classList.remove('active')); b.classList.add('active'); drawCards(); }; $('platforms').append(b); }
    drawCards(); await refresh(); setInterval(refresh,2000);
  } catch (e) { $('cards').append(el('p',`Unable to load workspace: ${e.message}`,'empty')); }
}
init();

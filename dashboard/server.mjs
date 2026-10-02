import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const publicDir = path.join(root, 'dashboard', 'public');
const jobs = new Map();
const registry = () => JSON.parse(fs.readFileSync(path.join(root, 'scrapers.json'), 'utf8'));
export function inside(base, relative) {
  if (typeof relative !== 'string' || path.isAbsolute(relative)) throw new Error('Use a relative path');
  const resolved = path.resolve(base, relative);
  if (!resolved.startsWith(base + path.sep)) throw new Error('Path leaves scraper folder');
  // Also reject links into other directories.
  if (fs.existsSync(resolved) && !fs.realpathSync(resolved).startsWith(fs.realpathSync(base) + path.sep)) throw new Error('Linked path leaves scraper folder');
  return resolved;
}
function scraper(id) {
  const item = registry().find(x => x.id === id);
  if (!item) throw new Error('Unknown scraper');
  return { ...item, cwd: inside(root, item.folder) };
}
function environment(item) {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  for (const file of [path.join(root, '.env'), path.join(item.cwd, '.env')]) {
    if (!fs.existsSync(file)) continue;
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (!match || Object.hasOwn(process.env, match[1])) continue;
      let value = match[2];
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1,-1);
      else value = value.replace(/\s+#.*$/, '');
      env[match[1]] = value;
    }
  }
  return env;
}
export function buildArgs(item, values = {}, extra = []) {
  if (!Array.isArray(extra) || extra.some(x => typeof x !== 'string' || x.length > 4096)) throw new Error('Extra arguments must be an array of strings');
  const args = [...item.command];
  for (const f of item.fields || []) {
    const value = values[f.key];
    if (value === undefined || value === '' || value === null || value === false) {
      if (f.required) throw new Error(`${f.label} is required`);
      continue;
    }
    if (f.type === 'checkbox') { if (value === true) args.push(f.flag); else throw new Error(`Invalid ${f.label}`); }
    else {
      if (!['string', 'number'].includes(typeof value)) throw new Error(`Invalid ${f.label}`);
      if (f.type === 'number' && (!Number.isFinite(Number(value)) || Number(value) < (f.min ?? 0))) throw new Error(`Invalid ${f.label}`);
      if (f.flag) args.push(f.flag);
      args.push(String(value));
    }
  }
  return [...args, ...extra];
}
function json(res, status, value) { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)); }
async function body(req) {
  let data = '';
  for await (const chunk of req) { data += chunk; if (data.length > 256000) throw new Error('Request too large'); }
  return JSON.parse(data || '{}');
}
function view(job) { const { child, ...safe } = job; return safe; }
function stop(job) {
  if (job.status !== 'running') return;
  job.status = 'stopping';
  if (process.platform === 'win32') spawn('taskkill', ['/PID', String(job.child.pid), '/T', '/F'], { windowsHide: true }).on('error', () => job.child.kill());
  else { try { process.kill(-job.child.pid, 'SIGTERM'); } catch { job.child.kill(); } }
}
export const server = http.createServer(async (req, res) => {
  try {
    // Local dashboard only. Reject cross-site requests and DNS rebinding.
    const host = req.headers.host || '';
    if (!/^(localhost|127\.0\.0\.1):\d+$/.test(host)) return json(res, 403, { error: 'Local access only' });
    if (req.headers.origin && req.headers.origin !== `http://${host}`) return json(res, 403, { error: 'Origin rejected' });
    const url = new URL(req.url, `http://${host}`);
    if (url.pathname === '/api/scrapers' && req.method === 'GET') return json(res, 200, registry());
    if (url.pathname === '/api/jobs' && req.method === 'GET') return json(res, 200, [...jobs.values()].map(view));
    if (url.pathname === '/api/run' && req.method === 'POST') {
      const input = await body(req), item = scraper(input.id);
      if ([...jobs.values()].some(j => j.scraper === item.id && ['running','stopping'].includes(j.status))) return json(res, 409, { error: 'This scraper already has an active run' });
      if ([...jobs.values()].filter(j => ['running','stopping'].includes(j.status)).length >= 4) return json(res, 409, { error: 'Four runs are already active' });
      const args = buildArgs(item, input.values, input.extra);
      const env = environment(item);
      const localPython = path.join(item.cwd, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
      const executable = fs.existsSync(localPython) ? localPython : env.LEADFORGE_PYTHON || 'python';
      const child = spawn(executable, ['-u', ...args], { cwd: item.cwd, env, shell: false, windowsHide: true, detached: process.platform !== 'win32' });
      const job = { id: randomUUID(), scraper: item.id, name: item.name, args, status: 'running', started: new Date().toISOString(), log: '', child };
      jobs.set(job.id, job);
      const append = text => { job.log = (job.log + text).slice(-150000); };
      child.stdout.on('data', x => append(x.toString())); child.stderr.on('data', x => append(x.toString()));
      child.on('error', error => { append(`\nUnable to start Python: ${error.message}\nSet LEADFORGE_PYTHON to your Python executable.\n`); job.status = 'failed'; job.ended = new Date().toISOString(); });
      child.on('close', code => { job.status = job.status === 'stopping' ? 'stopped' : code === 0 ? 'completed' : 'failed'; job.exitCode = code; job.ended = new Date().toISOString(); });
      // Retain a bounded history; logs deliberately stay in memory.
      if (jobs.size > 60) for (const [id, old] of jobs) { if (!['running','stopping'].includes(old.status)) { jobs.delete(id); break; } }
      return json(res, 202, view(job));
    }
    if (url.pathname === '/api/stop' && req.method === 'POST') {
      const input = await body(req), job = jobs.get(input.id);
      if (!job) return json(res, 404, { error: 'Run not found' });
      stop(job); return json(res, 200, view(job));
    }
    if (url.pathname === '/api/config' && ['GET','PUT'].includes(req.method)) {
      const input = req.method === 'PUT' ? await body(req) : Object.fromEntries(url.searchParams);
      const item = scraper(input.id);
      if (!(item.configs || []).includes(input.file)) throw new Error('Configuration file is not allowlisted');
      const target = inside(item.cwd, input.file);
      if (req.method === 'PUT') {
        if (typeof input.content !== 'string') throw new Error('Content must be text');
        if (input.file.endsWith('.json')) JSON.parse(input.content);
        fs.writeFileSync(target, input.content, 'utf8');
      }
      return json(res, 200, { content: fs.readFileSync(target, 'utf8') });
    }
    if (url.pathname === '/api/readme' && req.method === 'GET') { const item = scraper(url.searchParams.get('id')); return json(res, 200, { content: fs.readFileSync(inside(item.cwd, 'README.md'), 'utf8') }); }
    if (url.pathname.startsWith('/api/')) return json(res, 404, { error: 'Endpoint not found' });
    if (req.method !== 'GET') return json(res, 405, { error: 'Method not allowed' });
    const files = { '/': ['index.html','text/html'], '/app.js': ['app.js','text/javascript'], '/style.css': ['style.css','text/css'] };
    const file = files[url.pathname];
    if (!file) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, { 'Content-Type': `${file[1]}; charset=utf-8`, 'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff' });
    fs.createReadStream(path.join(publicDir, file[0])).pipe(res);
  } catch (error) { json(res, 400, { error: error.message }); }
});
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 3000);
  server.listen(port, '127.0.0.1', () => console.log(`LeadForge is ready at http://127.0.0.1:${port}`));
  server.on('error', error => { console.error(error.message); process.exitCode = 1; });
  for (const signal of ['SIGINT','SIGTERM']) process.on(signal, () => { for (const job of jobs.values()) stop(job); server.close(() => process.exit()); });
}

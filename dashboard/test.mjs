import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { server, root, inside, buildArgs } from './server.mjs';
let base;
before(async () => { await new Promise(resolve => server.listen(0,'127.0.0.1',resolve)); base = `http://127.0.0.1:${server.address().port}`; });
after(async () => { await new Promise(resolve => server.close(resolve)); });
test('all 11 registry commands and editable configurations exist', () => {
  const tools = JSON.parse(fs.readFileSync(path.join(root,'scrapers.json'),'utf8'));
  assert.equal(tools.length,11); assert.equal(new Set(tools.map(t => t.id)).size,11);
  for (const t of tools) {
    const cwd = inside(root,t.folder); assert.ok(fs.existsSync(path.join(cwd,'README.md')));
    if (t.command[0] === '-m') assert.ok(fs.existsSync(path.join(cwd,...t.command[1].split('.'))+'.py') || fs.existsSync(path.join(cwd,...t.command[1].split('.'),'__main__.py')));
    else assert.ok(fs.existsSync(path.join(cwd,t.command[0])));
    for (const config of t.configs) assert.ok(fs.existsSync(inside(cwd,config)));
  }
});
test('arguments preserve spaces and shell punctuation as literal values', () => {
  const item = { command:['run.py'], fields:[{key:'query',flag:'--query',label:'Query',required:true},{key:'dry',flag:'--dry-run',type:'checkbox'}] };
  assert.deepEqual(buildArgs(item,{query:'web design; echo test',dry:true},['--out','my leads.csv']),['run.py','--query','web design; echo test','--dry-run','--out','my leads.csv']);
  assert.throws(() => buildArgs(item,{},[]),/required/); assert.throws(() => buildArgs(item,{query:'x'},'--help'),/array/);
});
test('path traversal and absolute paths are rejected', () => {
  assert.throws(() => inside(root,'../outside'),/leaves/); assert.throws(() => inside(root,path.resolve(root,'README.md')), /relative/);
});
test('HTTP serves dashboard and registry, rejects cross-site writes and secret reads', async () => {
  const home = await fetch(base); assert.equal(home.status,200); assert.match(await home.text(),/LeadForge/);
  const tools = await (await fetch(base+'/api/scrapers')).json(); assert.equal(tools.length,11);
  const denied = await fetch(base+'/api/run',{method:'POST',headers:{Origin:'https://untrusted.example','Content-Type':'application/json'},body:'{}'}); assert.equal(denied.status,403);
  const secret = await fetch(base+'/api/config?id=facebook&file=service_account.json'); assert.equal(secret.status,400);
  const config = await fetch(base+'/api/config?id=linkedin&file=dashboard.config.json'); assert.equal(config.status,200); assert.match((await config.json()).content,/MAX_AGE_HOURS/);
  const unknown = await fetch(base+'/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'unknown'})}); assert.equal(unknown.status,400);
});

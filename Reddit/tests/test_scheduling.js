/**
 * Tests the append-only sheet behaviour and the twice-daily triggers by
 * simulating a spreadsheet in memory and running the job three times.
 *
 *   npm install @xmldom/xmldom && node tests/test_scheduling.js
 */
const { DOMParser } = require('@xmldom/xmldom');
function wrap(n){if(!n)return null;return{getText(){return n.textContent},getAttribute(a){const v=n.getAttribute&&n.getAttribute(a);return(v===null||v===undefined||v==='')?null:{getValue:()=>v}},getChildren(name,ns){const o=[];for(const c of Array.from(n.childNodes)){if(c.nodeType===1&&c.localName===name&&(!ns||c.namespaceURI===ns.uri))o.push(wrap(c))}return o},getChild(name,ns){return this.getChildren(name,ns)[0]||null}}}
global.XmlService={getNamespace:u=>({uri:u}),parse:x=>({getRootElement:()=>wrap(new DOMParser().parseFromString(x,'text/xml').documentElement)})};
global.Utilities={formatDate:(d)=>d.toISOString().slice(0,16).replace('T',' '),sleep:()=>{}};
global.Logger={log:()=>{}};
global.Session={getEffectiveUser:()=>({getEmail:()=>'x@y.z'}),getScriptTimeZone:()=>'UTC'};
global.MailApp={sendEmail:()=>{}};
const triggers=[];
global.ScriptApp={newTrigger(fn){const t={fn};return{timeBased:()=>({atHour(h){t.h=h;return this},everyDays(){return this},create(){triggers.push({getHandlerFunction:()=>fn,_h:t.h})}})}},
  getProjectTriggers:()=>triggers.slice(), deleteTrigger:(t)=>{triggers.splice(triggers.indexOf(t),1)}};

// --- fake spreadsheet ---
function FakeSheet(name){ this.name=name; this.grid=[]; this.formatCalls=[]; }
FakeSheet.prototype._ensure=function(r,c){ while(this.grid.length<r) this.grid.push([]); for(let i=0;i<r;i++){ while(this.grid[i].length<c) this.grid[i].push(''); } };
FakeSheet.prototype.clear=function(){ this.grid=[]; return this; };
FakeSheet.prototype.getLastRow=function(){ let last=0; this.grid.forEach((row,i)=>{ if(row.some(v=>v!==''&&v!==null&&v!==undefined)) last=i+1; }); return last; };
FakeSheet.prototype.getLastColumn=function(){ let m=0; this.grid.forEach(r=>{ for(let i=r.length-1;i>=0;i--){ if(r[i]!==''){ m=Math.max(m,i+1); break; } } }); return m; };
FakeSheet.prototype.setFrozenRows=function(){return this};
FakeSheet.prototype.autoResizeColumns=function(){return this};
FakeSheet.prototype.insertRowsAfter=function(after,n){ const blank=Array.from({length:n},()=>[]); this.grid.splice(after,0,...blank); return this; };
FakeSheet.prototype.insertRowAfter=function(after){ return this.insertRowsAfter(after,1); };
FakeSheet.prototype.deleteRow=function(r){ this.grid.splice(r-1,1); return this; };
FakeSheet.prototype.deleteRows=function(r,n){ this.grid.splice(r-1,n); return this; };
FakeSheet.prototype.getRange=function(row,col,numRows,numCols){
  const sheet=this; numRows=numRows||1; numCols=numCols||1;
  return {
    setValues(vals){ sheet._ensure(row+numRows-1, col+numCols-1);
      for(let i=0;i<vals.length;i++) for(let j=0;j<vals[i].length;j++) sheet.grid[row-1+i][col-1+j]=vals[i][j];
      return this; },
    getValues(){ sheet._ensure(row+numRows-1, col+numCols-1);
      const out=[]; for(let i=0;i<numRows;i++){ const r=[]; for(let j=0;j<numCols;j++) r.push(sheet.grid[row-1+i][col-1+j]); out.push(r); } return out; },
    setFontWeight(w){ sheet.formatCalls.push({op:'fontWeight', row, numRows, value:w}); return this },
    setBackground(b){ sheet.formatCalls.push({op:'background', row, numRows, value:b}); return this },
    setFontStyle(v){ sheet.formatCalls.push({op:'fontStyle', row, numRows, value:v}); return this },
    setFontColor(v){ sheet.formatCalls.push({op:'fontColor', row, numRows, value:v}); return this },
    setNumberFormat(){return this}, setNote(){return this},
  };
};
const sheets={};
const fakeSS={ getSheetByName:(n)=>sheets[n]||null, insertSheet:(n)=>(sheets[n]=new FakeSheet(n)), toast:()=>{} };
const mirrorSheets={};
const mirrorSS={ getSheetByName:(n)=>mirrorSheets[n]||null, insertSheet:(n)=>(mirrorSheets[n]=new FakeSheet(n)), toast:()=>{} };
global.SpreadsheetApp={
  getActiveSpreadsheet:()=>fakeSS,
  openById:(id)=>{ if(id!=='mirror-id') throw new Error('no such spreadsheet: '+id); return mirrorSS; },
  getUi:()=>{throw new Error('no ui')} };

let stubPosts = [];
eval(require('fs').readFileSync(__dirname + '/../google_apps_script/RedditLeads.gs','utf8'));
// Rebind the eval-scope function so no network is touched.
fetchAllPosts = function () { return stubPosts; };

// --- fixture posts ---
const now = Date.now()/1000;
const mk=(id,sub,author,title,body,days)=>({id,title,selftext:body,subreddit:sub,author,
  created_utc: now-days*86400, permalink:`/r/${sub}/comments/${id}/slug/`, num_comments:null, removed_by_category:null});
const batch1=[
  mk('aaa111','forhire','c1','[Hiring] Remote AI engineer for LangChain RAG chatbot','We are hiring a fully remote AI engineer. Budget $6,000. DM me. '+'detail '.repeat(60),2),
  mk('bbb222','n8n','c2','Looking for a remote n8n automation freelancer for AI workflow','Our budget is $1,200. Remote, worldwide. '+'detail '.repeat(60),3),
  mk('ccc333','forhire','c3','[Hiring] Remote computer vision contractor','Fully remote contract, we need OCR work. Paying $45/hr. '+'scope '.repeat(60),4),
];
const batch2=batch1.concat([
  mk('ddd444','forhire','c4','[Hiring] Remote LLM prompt engineer','Fully remote. Paying $70/hr. DM me. '+'more '.repeat(60),1),
]);

function runWith(posts){ stubPosts = posts; return generateLeads(); }

console.log('run 1:', runWith(batch1));
const s1=sheets['AI Remote Leads'];
console.log('  data rows:', s1.getLastRow()-1);

console.log('run 2 (same posts + 1 new):', runWith(batch2));
console.log('  data rows:', s1.getLastRow()-1);

const urlCol=SHEET_HEADERS.indexOf('post_url')+1;
const urls=s1.getRange(2,urlCol,s1.getLastRow()-1,1).getValues().map(r=>postIdFromUrl(r[0]));
console.log('  post ids in sheet:', JSON.stringify(urls));
console.log('  duplicates:', urls.length !== new Set(urls).size ? 'YES (BUG)' : 'none');
console.log('  newest on top:', urls[0]==='ddd444');
console.log('  headers:', s1.getRange(1,1,1,SHEET_HEADERS.length).getValues()[0].slice(0,4).join(' | '));

console.log('\nrun 3 (nothing new):', runWith(batch2));
console.log('  data rows:', s1.getLastRow()-1);

const afterRun3 = s1.getRange(2, urlCol, s1.getLastRow()-1, 1).getValues().map(r => postIdFromUrl(r[0]));

console.log('\nrun log rows:', sheets['Run log'].getLastRow()-1);
console.log('log latest:', JSON.stringify(sheets['Run log'].getRange(2,2,1,5).getValues()[0]));

console.log('\ninstallTriggers:', installTriggers());
console.log('  trigger count:', triggers.length, '| hours:', triggers.map(t=>t._h).join(','));
console.log('  idempotent re-install ->', (installTriggers(), triggers.length), 'triggers');
console.log('removeTriggers:', removeTriggers(), '| remaining:', triggers.length);

console.log('\n=== formatting (new rows must not inherit the bold header) ===');
const fmt = s1.formatCalls.filter(c => c.row >= 2);
const boldData = fmt.filter(c => c.op === 'fontWeight' && c.value === 'bold');
const normalData = fmt.filter(c => c.op === 'fontWeight' && c.value === 'normal');
console.log('  header bold calls on row 1:', s1.formatCalls.filter(c => c.row === 1 && c.value === 'bold').length);
console.log('  data rows set to normal:', normalData.length, '| data rows left bold:', boldData.length);
console.log('  backgrounds cleared on data rows:', fmt.filter(c => c.op === 'background' && c.value === null).length);

console.log('\n=== mirror spreadsheet ===');
CONFIG.MIRROR_SPREADSHEET_ID = 'mirror-id';
const batch3 = batch2.concat([
  mk('eee555','forhire','c5','[Hiring] Remote RAG engineer','Fully remote. Budget $9,000. DM me. '+'more '.repeat(60),1),
]);
// The mirror is switched on when the main sheet already holds 4 leads, so it
// must back-fill them, not just copy the one lead this run found.
console.log('main rows before mirror is enabled:', s1.getLastRow()-1);
console.log('run 4 (mirror on, 1 new):', runWith(batch3));
const m1 = mirrorSheets['AI Remote Leads'];
const mirrorRows = m1 ? m1.getLastRow()-1 : 0;
console.log('  mirror rows:', mirrorRows, '| main rows:', s1.getLastRow()-1);
console.log('  back-filled the pre-existing leads:', mirrorRows === s1.getLastRow()-1);

console.log('run 5 (nothing new):', runWith(batch3));
console.log('  mirror rows after re-run:', m1.getLastRow()-1, '(must not duplicate)');
const noDupes = m1.getLastRow()-1 === mirrorRows;

// first_seen must be carried across, not restamped, so the mirror reads the
// same as the source.
const srcFirstSeen = s1.getRange(2,1,1,1).getValues()[0][0];
const mirFirstSeen = m1.getRange(2,1,1,1).getValues()[0][0];
const stampsMatch = String(srcFirstSeen) === String(mirFirstSeen);
console.log('  first_seen carried across:', stampsMatch);

console.log('\n=== mirror failure must not break the run ===');
CONFIG.MIRROR_SPREADSHEET_ID = 'bad-id';
const batch4 = batch3.concat([
  mk('fff666','forhire','c6','[Hiring] Remote NLP contractor','Fully remote. Paying $85/hr. DM me. '+'more '.repeat(60),1),
]);
const msg = runWith(batch4);
console.log('  run still completed:', msg.indexOf('posts scanned') !== -1);
console.log('  failure reported:', msg.indexOf('Mirror failed') !== -1);
console.log('  lead still added to main sheet:', msg.indexOf('1 new added') !== -1);
CONFIG.MIRROR_SPREADSHEET_ID = '';

// Fail loudly rather than just printing, so this is usable in CI.
const finalUrls = s1.getRange(2, urlCol, s1.getLastRow() - 1, 1).getValues().map(r => postIdFromUrl(r[0]));
const problems = [];
if (afterRun3.length !== 4) { problems.push('expected 4 rows after run 3, got ' + afterRun3.length); }
if (afterRun3[0] !== 'ddd444') { problems.push('newest lead is not on top'); }
if (finalUrls.length !== new Set(finalUrls).size) { problems.push('duplicate rows in sheet'); }
if (finalUrls.length !== 6) { problems.push('expected 6 rows at the end, got ' + finalUrls.length); }
if (triggers.length !== 0) { problems.push('triggers not cleaned up'); }
if (boldData.length) { problems.push('data rows were left bold'); }
if (!normalData.length) { problems.push('data rows never reset to normal weight'); }
if (!m1 || mirrorRows !== 5) { problems.push('mirror did not back-fill all 5 leads, got ' + mirrorRows); }
if (!noDupes) { problems.push('mirror duplicated rows on re-run'); }
if (!stampsMatch) { problems.push('first_seen was restamped instead of carried across'); }
if (msg.indexOf('1 new added') === -1) { problems.push('a failing mirror blocked the main sheet write'); }
if (msg.indexOf('Mirror failed') === -1) { problems.push('mirror failure not reported'); }
if (problems.length) { console.error('\nFAILURES:\n - ' + problems.join('\n - ')); process.exitCode = 1; }
else { console.log('\nAll scheduling checks passed.'); }

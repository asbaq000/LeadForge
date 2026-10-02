/**
 * Tests the Apps Script's Atom parsing and lead filtering outside Google,
 * by shimming the handful of Google APIs it touches.
 *
 *   npm install @xmldom/xmldom && node tests/test_apps_script.js
 */
const { DOMParser } = require('@xmldom/xmldom');

// --- Minimal XmlService shim over a real XML parser -------------------------
function wrap(node) {
  if (!node) return null;
  return {
    _n: node,
    getText() { return node.textContent; },
    getAttribute(name) {
      const v = node.getAttribute && node.getAttribute(name);
      return (v === null || v === undefined || v === '') ? null : { getValue: () => v };
    },
    getChildren(name, ns) {
      const out = [];
      for (const c of Array.from(node.childNodes)) {
        if (c.nodeType === 1 && c.localName === name &&
            (!ns || c.namespaceURI === ns.uri)) out.push(wrap(c));
      }
      return out;
    },
    getChild(name, ns) { return this.getChildren(name, ns)[0] || null; },
  };
}
global.XmlService = {
  getNamespace: (uri) => ({ uri }),
  parse: (xml) => {
    const doc = new DOMParser().parseFromString(xml, 'text/xml');
    return { getRootElement: () => wrap(doc.documentElement) };
  },
};
global.Utilities = {
  formatDate: (d) => d.toISOString().slice(0, 16).replace('T', ' '),
  sleep: () => {},
};
global.SpreadsheetApp = { getActiveSpreadsheet: () => null };
global.Logger = { log: () => {} };
global.UrlFetchApp = { fetch: () => { throw new Error('no network'); } };

eval(require('fs').readFileSync(__dirname + '/../google_apps_script/RedditLeads.gs', 'utf8'));

// --- Realistic Reddit Atom fixture ------------------------------------------
const iso = (daysAgo) => new Date(Date.now() - daysAgo * 86400000).toISOString();
const entry = (id, sub, author, title, bodyHtml, daysAgo) => `
  <entry>
    <author><name>/u/${author}</name><uri>https://www.reddit.com/user/${author}</uri></author>
    <category term="${sub}" label="r/${sub}"/>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;${bodyHtml}&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; submitted by &lt;a href="https://www.reddit.com/user/${author}"&gt; /u/${author} &lt;/a&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/${sub}/comments/${id}/x/"&gt;[link]&lt;/a&gt;&lt;/span&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/${sub}/comments/${id}/x/"&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content>
    <id>t3_${id}</id>
    <link href="https://www.reddit.com/r/${sub}/comments/${id}/slug_here/"/>
    <updated>${iso(daysAgo)}</updated>
    <published>${iso(daysAgo)}</published>
    <title>${title}</title>
  </entry>`;

const feed = `<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <category term="forhire" label="r/forhire"/>
  <updated>${iso(0)}</updated>
  <title>newest submissions : forhire</title>
  ${entry('aaa111','forhire','clientone','[Hiring] Remote AI engineer - LangChain RAG chatbot',
    'We&amp;#39;re hiring a fully remote AI engineer to build a LangChain RAG chatbot. Budget $6,000. Work from home, any timezone. DM me. ' + 'Extra detail. '.repeat(30), 2)}
  ${entry('bbb222','forhire','clienttwo','[Hiring] ML engineer - hybrid, 3 days in office',
    'Must be based in Austin. Machine learning role, competitive salary.', 1)}
  ${entry('ccc333','forhire','devperson','[FOR HIRE] I build AI chatbots, fully remote',
    'Hire me! My portfolio is at example.com. I can build anything with GPT.', 1)}
  ${entry('ddd444','forhire','clientthree','[Hiring] Remote LLM developer needed',
    'Fully remote, no onsite. Paying $70/hr for prompt engineering and fine-tuning work. ' + 'More info. '.repeat(30), 20)}
  ${entry('eee555','n8n','clientfour','Looking for a remote n8n automation freelancer for AI workflow',
    'Remote, worldwide. Budget $1,200 for an AI automation workflow with OpenAI. ' + 'Details. '.repeat(30), 5)}
  ${entry('fff666','forhire','clientfive','[Hiring] Remote computer vision contractor',
    'Fully remote contract. We need OCR and computer vision work, no relocation required. ' + 'Scope. '.repeat(30), 3)}
  ${entry('ggg777','automation','coursebro','Learn AI Automation in 2026 - my Udemy course',
    'Enroll now with coupon code AI50. Remote learning, work from home. Budget friendly at $12.', 1)}
  ${entry('hhh888','LocalLLaMA','newsbot','OpenAI announces GPT-6 for remote enterprise teams',
    'The company said the model is available to contract customers worldwide. Paying customers get early access.', 1)}
  ${entry('iii999','LLMDevs','ranter','Rant: everyone claims to be a remote AI engineer now',
    'I am so tired of this. My budget for patience is $0. Work from home does not make you an ML engineer.', 2)}
  ${entry('jjj000','AI_Agents','builder','I built an AI agent that books meetings, fully remote team',
    'Check out my project. We are looking for feedback. Budget was $500 to build.', 1)}
  ${entry('kkk111','LLMDevs','learner','How do I become a remote AI engineer?',
    'Any advice? I am looking for a developer path and willing to pay for good courses.', 1)}
</feed>`;

const posts = parseFeed(feed);
console.log('=== parseFeed ===');
console.log('entries parsed:', posts.length);
const p0 = posts[0];
console.log('id:', p0.id, '| sub:', p0.subreddit, '| author:', p0.author);
console.log('permalink:', p0.permalink);
console.log('age(h):', ((Date.now()/1000 - p0.created_utc)/3600).toFixed(1));
console.log('title:', p0.title);
console.log('body starts:', JSON.stringify(p0.selftext.slice(0, 95)));
console.log('footer stripped:', !p0.selftext.includes('[comments]') && !p0.selftext.includes('submitted by'));
console.log('entities decoded:', p0.selftext.includes("We're hiring"));
console.log('tags stripped:', !p0.selftext.includes('<'));

console.log('\n=== buildLeads ===');
const out = buildLeads(posts);
console.log('leads:', out.rows.length, '| dupes:', out.stats.duplicates);
out.rows.forEach(r => console.log(` - ${r[2].padEnd(10)} | ${r[8].padEnd(10)} | score ${String(r[13]).padEnd(6)} | ${r[3].slice(0,50)}`));
console.log('\nintent evidence for top lead:', out.rows[0][5]);

console.log('\n=== noise rejected ===');
const noise = { ggg777: 'udemy course', hhh888: 'news', iii999: 'venting', jjj000: 'showcase', kkk111: 'advice' };
let allDropped = true;
for (const [id, label] of Object.entries(noise)) {
  const leaked = out.rows.some(r => r[10].includes(id));
  if (leaked) { allDropped = false; }
  const why = (out.rejected.find(x => x[3].includes(id)) || [])[2] || '(not in audit list)';
  console.log(` ${leaked ? 'LEAKED' : 'dropped'} | ${label.padEnd(13)} | ${why}`);
}
console.log('\nall noise dropped:', allDropped);
console.log('\nfilter rejects:', ['bbb222 (onsite)','ccc333 (for hire)','ddd444 (20 days old)']
  .filter((_, i) => !out.rows.some(r => r[10].includes(['bbb222','ccc333','ddd444'][i]))).join(', '));
console.log('\n=== AI mentioned only to rule it out ===');
const aiCases = [
  ['[task] help me find cool shopping in tokyo. $15. do not use chatgpt for this.','slavelabour', false],
  ['[task] find shopping spots. $10. no ai generated lists, real human only.','slavelabour', false],
  ['[task] python homework help $20','slavelabour', false],
  ['[task] build me a chatgpt bot for my store, paying $150','slavelabour', true],
  ['[hiring] remote ai engineer. do not send ai-generated cover letters.','forhire', true],
  ['need a model for a photoshoot, paying $200','forhire', false],
];
let aiOk = true;
for (const [t, sub, expected] of aiCases) {
  const r = classifyAiWork(t, sub);
  if (r.ok !== expected) { aiOk = false; }
  console.log(` ${r.ok === expected ? 'ok  ' : 'WRONG'} | ${(r.ok ? 'KEEP' : 'drop')} | ${t.slice(0, 46)}`);
}
console.log('  all AI classifications correct:', aiOk);

console.log('\nsample row url:', out.rows[0][10]);
console.log('comments col:', JSON.stringify(out.rows[0][12]));
if (!allDropped || !aiOk) { process.exitCode = 1; }

/**
 * Reddit AI lead generator for Google Sheets.
 *
 * Finds recent, remote, paid AI work on Reddit - jobs and one-off projects -
 * and appends them to a sheet. No Reddit account, API key or OAuth: it reads
 * Reddit's public Atom feeds, which are served normally to Google's servers
 * (the .json API answers 403 to datacenter IPs, which is where Apps Script
 * runs).
 *
 * A post becomes a lead only if it is within MAX_AGE_DAYS, is about AI work,
 * comes from someone hiring rather than advertising or discussing, and is
 * remote with no onsite or hybrid marker. There is no cap on how many leads
 * come back - the filters decide.
 *
 * Setup
 *   1. sheets.new -> Extensions -> Apps Script
 *   2. Replace everything in Code.gs with this file, then Save (Ctrl/Cmd+S).
 *      The function dropdown only refreshes once the file is saved.
 *   3. Run `generateLeads` and approve the permission prompt.
 *   4. Reload the sheet to get the "Reddit Leads" menu.
 *
 * Entry points
 *   generateLeads   fetch, filter and append new leads (also the scheduled run)
 *   installTriggers turn on the twice-daily schedule
 *   removeTriggers  turn it off
 *   syncMirrorNow   copy anything missing into the mirror spreadsheet
 *   testFetch       diagnose what Reddit is actually returning
 */

var CONFIG = {
  MAX_AGE_DAYS: 14,           // ignore posts older than this
  MAX_NEW_PER_RUN: 0,         // cap on leads added per run; 0 = no cap
  KEEP_DAYS: 30,              // drop rows added longer ago than this; 0 = keep all
  RUN_HOURS: [8, 20],         // twice daily, in the script's timezone
  SHEET_NAME: 'AI Remote Leads',
  AUDIT_SHEET_NAME: 'Rejected (audit)',
  LOG_SHEET_NAME: 'Run log',
  WRITE_AUDIT_SHEET: true,
  EMAIL_ON_NEW_LEADS: false,  // set true to be emailed when new leads land
  EMAIL_TO: '',               // blank = the account running the script
  SPREADSHEET_ID: '',         // only needed if the script is not bound to a Sheet
  MIRROR_SPREADSHEET_ID: '',  // optional: ID of a 2nd spreadsheet to copy leads into
  REQUEST_DELAY_MS: 1200,
  USER_AGENT: 'web:reddit-ai-leads:1.0 (public feed reader, no account)'
};

var GIG_SUBREDDITS = ['forhire', 'jobbit', 'hiring', 'freelance_forhire',
                      'RemoteJobs', 'remotejs', 'WorkOnline', 'b2bforhire',
                      'slavelabour', 'DoneDirtCheap'];
var AI_SUBREDDITS = ['AI_Agents', 'n8n', 'LLMDevs', 'MachineLearningJobs',
                     'automation', 'LocalLLaMA', 'PromptEngineering',
                     'MachineLearning', 'datascience', 'ChatGPTCoding',
                     'OpenAI', 'SaaS', 'Entrepreneur', 'smallbusiness',
                     'startups', 'webdev', 'freelance'];
var FEED_QUERY = 'AI OR LLM OR GPT OR chatbot OR machine learning OR automation';
var GLOBAL_QUERIES = ['hiring AI engineer remote',
                      'looking for AI developer remote paid',
                      'hiring LLM developer remote',
                      'AI automation developer needed remote',
                      'hiring machine learning engineer remote',
                      'need AI agent developer remote paid'];

var AI_STRONG = ['artificial intelligence','machine learning','deep learning','large language model','llm','llms','generative ai','genai','gpt','chatgpt','openai','anthropic','claude','gemini','llama','mistral','hugging face','huggingface','langchain','langgraph','llamaindex','crewai','autogen','rag','vector database','pinecone','embeddings','chatbot','chat bot','voice agent','conversational ai','nlp','natural language processing','computer vision','prompt engineer','prompt engineering','fine-tune','fine tune','fine-tuning','stable diffusion','comfyui','midjourney','pytorch','tensorflow','mlops','ml engineer','ai engineer','ai developer','ai agent','ai agents','agentic','whisper','text to speech','speech to text','ocr','data annotation','ai automation','ai workflow','n8n'];
var AI_WEAK = ['ai','automation','automate','zapier','scraper','scraping','python','algorithm'];
var HIRING = ['hiring','looking for','looking to hire','seeking','need someone','need a developer','need help','want to hire','job opening','open position','vacancy','recruiting','contract','contractor','freelance','freelancer','consultant','budget','paid','will pay','compensation','salary','rate','apply','dm me','pm me'];
var REMOTE = ['remote','fully remote','work from home','wfh','telecommute','anywhere','worldwide','global','any timezone','distributed team','async','online','virtual'];
var ONSITE = ['onsite','on-site','on site','in-person','in person','in office','in-office','on premise','on-premise','hybrid','must be located','must be based','must reside','must live in','must be local','local only','locals only','local candidates','relocate','relocation','commute','commutable','days in the office','days in office','come to the office','our office','office-based','office based'];
var NEGATIONS = ['no','not','non','never','zero','without','avoid'];
// Task subreddits are full of "no AI generated answers, I want a real person".
// Those posts name ChatGPT precisely because they are NOT AI work.
var ANTI_AI_MARKERS = ['no ai','no a.i.','not ai','without ai','no chatgpt','no gpt','no ai generated','not ai generated','no ai-generated','no ai written','human written','human-written','written by a human','real human','actual human','real person','no bots','no bot','no automation','no scripts','manually only','by hand only','not generated','no llm'];
// Tokens that cancel an AI mention when they appear shortly before it.
var AI_NEGATIONS = ['no','not','non','never','zero','without','avoid','dont',"don't",'doesnt',"doesn't",'wont',"won't",'isnt',"isn't",'arent',"aren't","cant","can't"];
var AI_NEGATION_WINDOW = 30;
// Micro-task subreddits, where a couple of weak words means nothing.
var LOW_SIGNAL_SUBS = ['slavelabour','donedirtcheap'];
var REMOTE_SUBS = ['remotejobs','remotejs','workonline','jobbit'];
var SELF_PROMO_PREFIX = ['[for hire]','[forhire]','(for hire)','for hire','[available]','available for hire','hire me','[offer]','[advert]'];
var JOB_TERMS = ['full-time','full time','part-time','part time','salary','salaried','benefits','position','role','employee','annual','per year','/yr'];
var STOPWORDS = ['a','an','the','for','to','of','and','or','in','on','at','with','we','our','you','your','is','are','be','need','needed','looking','hiring','hire','job','role','remote','usd','hour','hr','week','month','paid','pay','help','please','new','up'];

// The demand gate. Generic job words ("paid", "contract", "rate", "apply")
// appear in course ads, news posts and rants, so a lead has to show that the
// poster THEMSELVES is hiring or commissioning the work.
var DEMAND_STRONG = ['[hiring]','(hiring)','hiring:','we are hiring',"we're hiring",'i am hiring',"i'm hiring",'now hiring','is hiring','currently hiring','looking to hire','want to hire','wanting to hire','need to hire','ready to hire','hiring a','hiring an','hiring for','we are looking for',"we're looking for",'i am looking for',"i'm looking for",'looking for someone','looking for a developer','looking for a dev','looking for an engineer','looking for a freelancer','looking for a contractor','looking for an ai','looking for help building','our team is looking','my team is looking','i need someone','we need someone','need someone to','need someone who','i need a developer','we need a developer','need a developer','need an engineer','need an ai','need help building','need built','seeking a','seeking an','seeking someone','in search of someone','developer needed','dev needed','engineer needed','freelancer needed','contractor needed','consultant needed','help wanted','wanted:','job description','job opening','open position','position available','open role','we have an opening','join our team','role available','willing to pay','i will pay','we will pay','happy to pay','can pay','ready to pay','will compensate','my budget','our budget','budget is','budget of','budget:','paying $','pay $','offering $','paid gig','paid project','paid opportunity','paid role','paid work','freelance opportunity','contract opportunity','contract role','contract position'];

// Fixed phrases only match contiguous text, but real posts write "looking for
// a remote n8n automation freelancer". So a seek verb followed within a short
// window by a role noun also counts as demand.
var DEMAND_SEEK_VERBS = ['looking for','look for','looking to','seeking','searching for','in search of','need','needs','needed','want','wanting','hiring','recruiting','recruit','after','require','requires'];
var DEMAND_ROLE_NOUNS = ['developer','dev','devs','engineer','engineers','freelancer','freelancers','contractor','consultant','programmer','coder','expert','experts','specialist','agency','someone','somebody','person','team','builder','architect','scientist','analyst','professional','pro','talent','candidate','partner'];
var DEMAND_WINDOW_CHARS = 60;

var ACTIONABLE_CONTACT = ['dm me','pm me','send me a dm','message me','email me','contact me','reach out','get in touch','hit me up','apply here','apply now','to apply','send your','share your','send me your','comment below','leave a comment','if interested','let me know if','happy to discuss','more details on request'];

// Rejected wherever they appear. Deliberately narrow - "newsletter",
// "youtube" and "bootcamp" are NOT here, because "automate my newsletter"
// is a real lead.
var VETO_PROMO = ['udemy','coursera','skillshare','free course','my course','join my course','enroll now','enrollment','coupon code','discount code','promo code','100% off','limited time offer','masterclass','webinar','affiliate link','referral link','giveaway','sign up for my','link in bio','dm for the link'];

// Rejected only in the TITLE - the title is what says which kind of post it
// is. The same words in a body are often incidental ("we just launched, now
// we need an AI dev" is a real lead).
var TITLE_NOISE = [
  { label: 'news/announcement', terms: ['announced','announces','announcing','has released','releases','launches','launched today','study finds','research shows','report says','according to','breaking','just dropped','is now available','new model','comparison'] },
  { label: 'discussion/venting', terms: ['what do you think','thoughts','your thoughts','discussion','unpopular opinion','am i the only one','rant','venting','vent','change my mind','hot take','poll','survey','eli5','does anyone else','why does everyone','is it just me'] },
  { label: 'showcase', terms: ['i built','i made','i created','i developed','we built','we made','just launched','check out my','feedback on my','roast my','review my','sharing my','showcase','show off','my first','i open sourced','open sourced my'] },
  { label: 'advice-seeking', terms: ['how do i','how can i','how to','any advice','need advice','recommendations','recommend','which tool','what tool','best way to','is it worth','should i','worth learning','career advice','beginner question','noob question','getting started','roadmap','learning path','help me understand'] },
  { label: 'seeking work', terms: ['looking for work','looking for a job','seeking opportunities','open to work','my resume','my cv','my portfolio','years of experience','available for hire'] }
];

var LEAD_HEADERS = ['date_posted_utc','days_ago','subreddit','title','lead_type','intent_evidence','work_location',
                    'remote_evidence','pay_or_budget','author','post_url','contact','comments',
                    'lead_score','why_it_matches','snippet'];

// The sheet also records when each lead was first picked up, which is what
// makes an append-only, twice-daily sheet readable.
var SHEET_HEADERS = ['first_seen'].concat(LEAD_HEADERS);
var URL_INDEX = LEAD_HEADERS.indexOf('post_url');

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Reddit Leads')
    .addItem('Check for new leads now', 'generateLeads')
    .addSeparator()
    .addItem('Turn on twice-daily auto-update', 'installTriggers')
    .addItem('Turn off auto-update', 'removeTriggers')
    .addItem('Auto-update status', 'autoUpdateStatus')
    .addSeparator()
    .addItem('Sync mirror sheet now', 'syncMirrorNow')
    .addToUi();
}

/**
 * Entry point, for both the menu and the scheduled runs.
 *
 * Appends only leads the sheet has not seen before, so running it twice a day
 * accumulates new posts instead of rewriting the sheet.
 */
function generateLeads() {
  var startedAt = new Date();
  var posts = fetchAllPosts();
  var result = buildLeads(posts);

  var sheet = ensureLeadsSheet();
  var seen = existingPostIds(sheet);

  var cap = CONFIG.MAX_NEW_PER_RUN > 0 ? CONFIG.MAX_NEW_PER_RUN : Infinity;
  var fresh = [];
  for (var i = 0; i < result.rows.length && fresh.length < cap; i++) {
    var id = postIdFromUrl(result.rows[i][URL_INDEX]);
    if (!id || seen[id]) { continue; }
    seen[id] = true;
    fresh.push(result.rows[i]);
  }

  prependLeads(sheet, fresh, startedAt);
  var pruned = pruneOldLeads(sheet);
  if (CONFIG.WRITE_AUDIT_SHEET) { writeAuditSheet(result.rejected); }

  var notes = '';
  var mirrored = 0;
  if (CONFIG.MIRROR_SPREADSHEET_ID) {
    try {
      mirrored = syncMirror();
    } catch (e) {
      // A broken mirror must not lose the run's real work.
      notes = 'Mirror failed: ' + e.message + '. ';
    }
  }

  if (posts.length === 0) {
    notes += 'No posts fetched -- HTTP codes: ' + JSON.stringify(FETCH_LOG.byCode) +
            ' | first body: ' + (FETCH_LOG.sample || '(empty)');
  } else if (!fresh.length) {
    notes += 'No new leads; every match was already in the sheet.';
  }

  logRun([startedAt, posts.length, result.rows.length, fresh.length, mirrored, pruned, notes]);
  notifyNewLeads(fresh);

  var message = posts.length + ' posts scanned, ' + result.rows.length + ' matched, ' +
                fresh.length + ' new added' +
                (mirrored ? ', ' + mirrored + ' mirrored' : '') +
                (pruned ? ', ' + pruned + ' pruned' : '') +
                '.' + (notes ? ' ' + notes : '');
  Logger.log(message);
  toast(message);
  return message;
}

// --- Fetching (Atom feeds, not JSON) ----------------------------------------
//
// Reddit answers .json with HTTP 403 and a block page when the request comes
// from a datacenter IP range, which is where Apps Script runs. The .rss feeds
// are served normally from the same addresses, so everything here reads Atom.
// The trade-off: feeds carry no comment count or score, and search feeds
// sometimes omit the post body.

var ATOM_NS = 'http://www.w3.org/2005/Atom';

/** Per-run record of what Reddit actually answered. */
var FETCH_LOG = { ok: 0, failed: 0, entries: 0, byCode: {}, sample: '' };

function noteCode(code) {
  FETCH_LOG.byCode[code] = (FETCH_LOG.byCode[code] || 0) + 1;
}

function noteSample(body) {
  if (!FETCH_LOG.sample) {
    FETCH_LOG.sample = String(body || '').replace(/\s+/g, ' ').slice(0, 300);
  }
}

function httpGet(url) {
  return UrlFetchApp.fetch(url, {
    muteHttpExceptions: true,
    followRedirects: true,
    headers: {
      'User-Agent': CONFIG.USER_AGENT,
      'Accept': 'application/atom+xml, application/xml, text/xml, */*',
      'Accept-Language': 'en-US,en;q=0.9'
    }
  });
}

/** Which feeds to read. */
function buildFeeds() {
  var feeds = [];
  var q = encodeURIComponent;
  var i, j;

  // Backbone: the newest posts in every gig and AI subreddit. Filtering
  // happens locally, so a broad feed beats a narrow query.
  for (i = 0; i < GIG_SUBREDDITS.length; i++) {
    feeds.push('https://www.reddit.com/r/' + GIG_SUBREDDITS[i] + '/new.rss?limit=100');
  }
  for (i = 0; i < AI_SUBREDDITS.length; i++) {
    feeds.push('https://www.reddit.com/r/' + AI_SUBREDDITS[i] + '/new.rss?limit=100');
  }

  // Targeted: AI terms inside the gig subreddits, reaching past the newest 100.
  for (i = 0; i < GIG_SUBREDDITS.length; i++) {
    feeds.push('https://www.reddit.com/r/' + GIG_SUBREDDITS[i] + '/search.rss?q=' +
               q(FEED_QUERY) + '&restrict_sr=1&sort=new&t=month&limit=100');
  }

  // Sitewide, to catch subreddits that are not on either list.
  for (j = 0; j < GLOBAL_QUERIES.length; j++) {
    feeds.push('https://www.reddit.com/search.rss?q=' + q(GLOBAL_QUERIES[j]) +
               '&sort=new&t=month&limit=100');
  }
  return feeds;
}

function decodeEntities(text) {
  return String(text || '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&apos;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/&#(\d+);/g, function (m, code) { return String.fromCharCode(Number(code)); })
    .replace(/&amp;/g, '&');
}

/** Feed bodies are HTML. Reduce them to the plain text the filters expect. */
function htmlToText(html) {
  var text = decodeEntities(html);
  text = text.replace(/<[^>]*>/g, ' ');
  text = decodeEntities(text).replace(/\s+/g, ' ').trim();
  // Every feed entry ends with Reddit's own "submitted by /u/x ... [comments]".
  var footer = text.lastIndexOf('submitted by');
  if (footer > 0 && text.length - footer < 200) {
    text = text.slice(0, footer).trim();
  }
  return text;
}

function childText(element, name, ns) {
  var child = element.getChild(name, ns);
  return child ? child.getText() : '';
}

function attr(element, name) {
  if (!element) { return ''; }
  var found = element.getAttribute(name);
  return found ? found.getValue() : '';
}

/** Turn one Atom feed into post objects shaped like the JSON API's. */
function parseFeed(xml) {
  var ns = XmlService.getNamespace(ATOM_NS);
  var entries = XmlService.parse(xml).getRootElement().getChildren('entry', ns);
  var posts = [];

  for (var i = 0; i < entries.length; i++) {
    var entry = entries[i];

    var id = childText(entry, 'id', ns).replace('t3_', '');
    var link = attr(entry.getChild('link', ns), 'href');
    var published = childText(entry, 'published', ns) || childText(entry, 'updated', ns);
    var stamp = published ? new Date(published).getTime() / 1000 : 0;

    var author = childText(entry.getChild('author', ns) || entry, 'name', ns);
    var subreddit = attr(entry.getChild('category', ns), 'term');

    // "https://www.reddit.com/r/x/comments/id/slug/" -> "/r/x/comments/id/slug/"
    var permalink = link.replace(/^https?:\/\/[^\/]+/, '');

    if (!id || !stamp) { continue; }

    posts.push({
      id: id,
      title: decodeEntities(childText(entry, 'title', ns)),
      selftext: htmlToText(childText(entry, 'content', ns)),
      subreddit: subreddit || (permalink.split('/')[2] || ''),
      author: String(author || '').replace('/u/', ''),
      created_utc: stamp,
      permalink: permalink,
      num_comments: null,
      removed_by_category: null
    });
  }
  return posts;
}

function fetchFeed(url) {
  for (var attempt = 0; attempt < 3; attempt++) {
    var response;
    try {
      response = httpGet(url);
    } catch (e) {
      noteCode('exception');
      noteSample(e.message);
      return [];
    }

    var code = response.getResponseCode();
    noteCode(code);

    if (code === 200) {
      var body = response.getContentText();
      try {
        var posts = parseFeed(body);
        FETCH_LOG.ok++;
        FETCH_LOG.entries += posts.length;
        return posts;
      } catch (e) {
        noteSample(body);
        return [];
      }
    }

    if (code === 429 || code >= 500) {
      Utilities.sleep(CONFIG.REQUEST_DELAY_MS * Math.pow(2, attempt + 1));
      continue;
    }

    noteSample(response.getContentText());
    return [];
  }
  return [];
}

function fetchAllPosts() {
  var feeds = buildFeeds();
  var posts = [];
  var seen = {};
  var consecutiveFailures = 0;

  FETCH_LOG = { ok: 0, failed: 0, entries: 0, byCode: {}, sample: '' };

  for (var i = 0; i < feeds.length; i++) {
    var batch = fetchFeed(feeds[i]);

    if (!batch.length) {
      FETCH_LOG.failed++;
      consecutiveFailures++;
      // Only bail if nothing at all has worked. A search feed returning
      // nothing is normal; every feed failing from the start is a block.
      if (FETCH_LOG.ok === 0 && consecutiveFailures >= 8) {
        throw new Error(
          'Reddit returned nothing usable for the first ' + consecutiveFailures +
          ' feeds. Response codes: ' + JSON.stringify(FETCH_LOG.byCode) +
          ' | first body: ' + (FETCH_LOG.sample || '(empty)'));
      }
      Utilities.sleep(CONFIG.REQUEST_DELAY_MS);
      continue;
    }

    consecutiveFailures = 0;
    for (var b = 0; b < batch.length; b++) {
      if (!seen[batch[b].id]) {
        seen[batch[b].id] = true;
        posts.push(batch[b]);
      }
    }
    Utilities.sleep(CONFIG.REQUEST_DELAY_MS);
  }
  return posts;
}

/**
 * Diagnostic: run this alone to see what Reddit answers for each feed type.
 * Check the execution log afterwards.
 */
function testFetch() {
  var urls = [
    'https://www.reddit.com/r/forhire/new.rss?limit=25',
    'https://www.reddit.com/r/forhire/search.rss?q=' + encodeURIComponent(FEED_QUERY) +
      '&restrict_sr=1&sort=new&t=month&limit=25',
    'https://www.reddit.com/search.rss?q=' + encodeURIComponent('hiring AI engineer remote') +
      '&sort=new&t=month&limit=25'
  ];
  var lines = [];
  for (var i = 0; i < urls.length; i++) {
    var line;
    try {
      var response = httpGet(urls[i]);
      var body = response.getContentText();
      var posts = [];
      var parseError = '';
      try {
        posts = parseFeed(body);
      } catch (e) {
        parseError = e.message;
      }
      line = urls[i] + '\n    HTTP ' + response.getResponseCode() +
             ' | bytes ' + body.length + ' | entries parsed ' + posts.length +
             (parseError ? ' | parse error: ' + parseError : '');
      if (posts.length) {
        line += '\n    newest: "' + posts[0].title.slice(0, 70) + '"' +
                ' by u/' + posts[0].author +
                ' in r/' + posts[0].subreddit +
                ' | body chars ' + posts[0].selftext.length +
                ' | ' + ((Date.now() / 1000 - posts[0].created_utc) / 3600).toFixed(1) + 'h ago';
      } else {
        line += '\n    body: ' + body.replace(/\s+/g, ' ').slice(0, 200);
      }
    } catch (e) {
      line = urls[i] + '\n    EXCEPTION: ' + e.message;
    }
    lines.push(line);
    Utilities.sleep(1500);
  }
  var report = lines.join('\n\n');
  Logger.log(report);
  return report;
}

// --- Text matching ----------------------------------------------------------

function collapse(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function isWordChar(ch) {
  return /[a-z0-9]/.test(ch);
}

function findTerm(text, term, startAt) {
  var from = startAt || 0;
  while (from <= text.length) {
    var i = text.indexOf(term, from);
    if (i === -1) { return -1; }
    var before = i > 0 ? text.charAt(i - 1) : ' ';
    var after = i + term.length < text.length ? text.charAt(i + term.length) : ' ';
    var leftOk = !isWordChar(term.charAt(0)) || !isWordChar(before);
    var rightOk = !isWordChar(term.charAt(term.length - 1)) || !isWordChar(after);
    if (leftOk && rightOk) { return i; }
    from = i + 1;
  }
  return -1;
}

function hits(text, terms) {
  var found = [];
  for (var i = 0; i < terms.length; i++) {
    if (findTerm(text, terms[i]) !== -1) { found.push(terms[i]); }
  }
  found.sort(function (a, b) { return b.length - a.length; });
  return found;
}

/** Onsite terms that are not cancelled by a preceding negation. */
function onsiteHits(text) {
  var found = [];
  for (var i = 0; i < ONSITE.length; i++) {
    var at = findTerm(text, ONSITE[i]);
    if (at === -1) { continue; }
    var before = text.slice(Math.max(0, at - 30), at);
    var words = before.replace(/[^a-z0-9 ]/g, ' ').split(' ').filter(function (w) { return w; });
    var last = words.length ? words[words.length - 1] : '';
    if (NEGATIONS.indexOf(last) === -1) { found.push(ONSITE[i]); }
  }
  return found;
}

function extractPay(text) {
  var match = String(text || '').match(
    /(\$\s?\d[\d,.]*\s*(?:k|,000)?(?:\s*(?:-|–|to)\s*\$?\s?\d[\d,.]*\s*k?)?(?:\s*(?:\/|per\s+)(?:hr|hour|h|day|week|month|mo|year|yr|project))?|\d[\d,.]*\s*(?:usd|eur|gbp|cad|aud)\b|\b\d{2,3}\s*(?:\/|per\s+)(?:hr|hour)\b)/i);
  return match ? collapse(match[0]).replace(/[.,]+$/, '') : '';
}

function contactHint(post, body) {
  var email = String(body || '').match(/[\w.+-]+@[\w-]+\.[\w.]+/);
  if (email) { return email[0]; }
  var handle = String(body || '').match(/(telegram|discord|whatsapp|calendly|t\.me\/\S+)/i);
  if (handle) { return handle[0]; }
  return 'Comment on post / DM u/' + post.author;
}

// --- Lead building ----------------------------------------------------------

/** A seek verb followed closely by a role noun. */
function demandPatterns(text) {
  var found = [];
  for (var v = 0; v < DEMAND_SEEK_VERBS.length; v++) {
    var verb = DEMAND_SEEK_VERBS[v];
    var at = 0;
    while (true) {
      var i = findTerm(text, verb, at);
      if (i === -1) { break; }
      var after = i + verb.length;
      var roles = hits(text.slice(after, after + DEMAND_WINDOW_CHARS), DEMAND_ROLE_NOUNS);
      if (roles.length) { found.push(verb + ' ... ' + roles[0]); break; }
      at = i + 1;
    }
  }
  return found;
}

/**
 * True when a negation word appears shortly before this position.
 *
 * Looser than the onsite check, which only inspects the immediately preceding
 * word: "do not use ChatGPT" and "without using AI" both put words between
 * the negation and the term.
 */
function negatedNearby(text, at) {
  var before = text.slice(Math.max(0, at - AI_NEGATION_WINDOW), at);
  var words = before.replace(/[^a-z0-9' ]/g, ' ').split(' ');
  for (var i = 0; i < words.length; i++) {
    if (words[i] && AI_NEGATIONS.indexOf(words[i]) !== -1) { return true; }
  }
  return false;
}

/** Terms that appear without a negation just before them. */
function hitsUnnegated(text, terms) {
  var found = [];
  for (var i = 0; i < terms.length; i++) {
    var term = terms[i];
    var at = 0;
    while (true) {
      var idx = findTerm(text, term, at);
      if (idx === -1) { break; }
      if (!negatedNearby(text, idx)) { found.push(term); break; }
      at = idx + 1;
    }
  }
  found.sort(function (a, b) { return b.length - a.length; });
  return found;
}

/**
 * Is the post genuinely about AI work?
 *
 * A post can legitimately say "no AI-written cover letters" while still being
 * an AI job, so an anti-AI phrase only rejects when nothing real is named.
 */
function classifyAiWork(text, subreddit) {
  var strong = hitsUnnegated(text, AI_STRONG);
  var weak = hitsUnnegated(text, AI_WEAK);

  if (!strong.length) {
    var excluded = hits(text, ANTI_AI_MARKERS);
    if (excluded.length) {
      return { ok: false, reason: 'post rules AI out (' + excluded[0] + ')' };
    }
    if (LOW_SIGNAL_SUBS.indexOf(String(subreddit || '').toLowerCase()) !== -1) {
      return { ok: false, reason: 'micro-task subreddit with no specific AI work named' };
    }
    if (weak.length < 2) { return { ok: false, reason: 'not AI-related' }; }
  }

  return { ok: true, strong: strong, weak: weak };
}

function hasHiringTag(titleLower) {
  return /^\s*[\[\(]?\s*hiring\b/.test(titleLower) ||
         titleLower.indexOf('[hiring]') !== -1 ||
         titleLower.indexOf('(hiring)') !== -1;
}

function titleNoise(titleLower) {
  for (var i = 0; i < TITLE_NOISE.length; i++) {
    var found = hits(titleLower, TITLE_NOISE[i].terms);
    if (found.length) { return { label: TITLE_NOISE[i].label, term: found[0] }; }
  }
  return null;
}

/**
 * Is this a real gig from the person offering it?
 *
 * Gates, in order: no course/referral promotion anywhere; the title must not
 * announce a news, venting, showcase, advice or job-seeking post; the poster
 * must show first-person hiring intent; there must be an actionable hook
 * (money, a contact route, or an explicit [Hiring] tag); and outside the gig
 * subreddits the bar rises to money or a tag, since that is where courses,
 * news and rants come from.
 */
function classifyIntent(titleLower, text, subreddit, pay) {
  var promo = hits(text, VETO_PROMO);
  if (promo.length) {
    return { ok: false, reason: 'promotional/course content (' + promo[0] + ')' };
  }

  var noise = titleNoise(titleLower);
  if (noise) {
    return { ok: false, reason: noise.label + ' post, not a gig (title: ' + noise.term + ')' };
  }

  var demand = hits(text, DEMAND_STRONG).concat(demandPatterns(text));
  if (!demand.length) {
    return { ok: false, reason: 'no first-person hiring intent' };
  }

  var tagged = hasHiringTag(titleLower);
  var contact = hits(text, ACTIONABLE_CONTACT);
  if (!pay && !tagged && !contact.length) {
    return { ok: false, reason: 'hiring intent but no budget, contact route or [Hiring] tag' };
  }

  var sub = String(subreddit || '').toLowerCase();
  var isGigSub = false;
  for (var g = 0; g < GIG_SUBREDDITS.length; g++) {
    if (GIG_SUBREDDITS[g].toLowerCase() === sub) { isGigSub = true; break; }
  }
  if (!isGigSub && !pay && !tagged) {
    return { ok: false, reason: 'discussion subreddit without a stated budget or [Hiring] tag' };
  }

  var evidence = demand.slice(0, 3);
  if (pay) { evidence.push('pays ' + pay); }
  else if (contact.length) { evidence.push(contact[0]); }

  return { ok: true, evidence: evidence.join('; '), demand: demand, tagged: tagged };
}

function titleSignature(title) {
  var words = String(title || '').toLowerCase().match(/[a-z0-9]+/g) || [];
  var kept = [];
  for (var i = 0; i < words.length; i++) {
    if (words[i].length > 2 && STOPWORDS.indexOf(words[i]) === -1 && kept.indexOf(words[i]) === -1) {
      kept.push(words[i]);
    }
  }
  return kept.sort().join(' ');
}

function buildLeads(posts) {
  var nowSec = Date.now() / 1000;
  var leads = [];
  var rejected = [];
  var reasonCounts = {};

  function drop(post, title, reason) {
    reasonCounts[reason] = (reasonCounts[reason] || 0) + 1;
    // Keep the near-misses so the filter can be audited and tuned.
    if (rejected.length < 300) {
      rejected.push([
        'r/' + post.subreddit,
        title,
        reason,
        'https://www.reddit.com' + post.permalink
      ]);
    }
  }

  for (var i = 0; i < posts.length; i++) {
    var post = posts[i];
    var title = collapse(post.title);
    var titleLower = title.toLowerCase();

    var ageDays = (nowSec - (post.created_utc || 0)) / 86400;
    if (ageDays < 0 || ageDays > CONFIG.MAX_AGE_DAYS) { continue; }
    if (post.removed_by_category || post.selftext === '[removed]' ||
        post.selftext === '[deleted]') { continue; }

    var isSelfPromo = false;
    for (var s = 0; s < SELF_PROMO_PREFIX.length; s++) {
      if (titleLower.indexOf(SELF_PROMO_PREFIX[s]) === 0) { isSelfPromo = true; break; }
    }
    if (isSelfPromo) { drop(post, title, 'author is offering services, not hiring'); continue; }

    var body = collapse(post.selftext);
    var text = titleLower + ' . ' + body.toLowerCase();

    var ai = classifyAiWork(text, post.subreddit);
    if (!ai.ok) {
      // Worth auditing: these are posts that mention AI but are not AI work.
      if (ai.reason !== 'not AI-related') { drop(post, title, ai.reason); }
      continue;
    }
    var strong = ai.strong;
    var weak = ai.weak;

    var pay = extractPay(title + ' ' + body);

    var intent = classifyIntent(titleLower, text, post.subreddit, pay);
    if (!intent.ok) { drop(post, title, intent.reason); continue; }

    if (onsiteHits(text).length > 0) {
      drop(post, title, 'onsite/hybrid marker: ' + onsiteHits(text).slice(0, 2).join(', '));
      continue;
    }

    var remote = hits(text, REMOTE);
    var sub = String(post.subreddit || '').toLowerCase();
    var remoteByRule = REMOTE_SUBS.indexOf(sub) !== -1;
    if (remote.length === 0 && !remoteByRule) {
      drop(post, title, 'no explicit remote statement');
      continue;
    }

    // --- score ---
    var reasons = [];
    var score = Math.min(strong.length * 3 + (strong.length ? Math.min(weak.length, 4) : 2), 12);
    reasons.push(strong.length ? 'AI: ' + strong.slice(0, 4).join(', ')
                               : 'AI-adjacent: ' + weak.slice(0, 3).join(', '));

    if (intent.tagged) {
      score += 5; reasons.push('[Hiring]-tagged title');
    } else if (hits(titleLower, DEMAND_STRONG).length) {
      score += 4; reasons.push('hiring intent in title');
    } else {
      score += 2; reasons.push('hiring intent in body');
    }

    // Several independent ways of saying "I am hiring" beats one stray phrase.
    score += Math.min(intent.demand.length, 3);
    if (intent.demand.length > 1) {
      reasons.push(intent.demand.length + ' demand signals');
    }

    if (pay) { score += 4; reasons.push('budget stated (' + pay + ')'); }

    score += Math.max(0, (CONFIG.MAX_AGE_DAYS - ageDays) / CONFIG.MAX_AGE_DAYS) * 5;
    reasons.push(ageDays.toFixed(1) + ' days old');

    if (hits(titleLower, REMOTE).length) { score += 2; reasons.push('remote stated in title'); }
    if (body.length > 400) { score += 2; reasons.push('detailed post'); }
    else if (body.length < 80) { score -= 2; reasons.push('very short post'); }

    leads.push({
      score: Math.round(score * 100) / 100,
      id: post.id,
      author: String(post.author || '').toLowerCase(),
      signature: titleSignature(title),
      row: [
        Utilities.formatDate(new Date(post.created_utc * 1000), 'UTC', 'yyyy-MM-dd HH:mm'),
        Math.round(ageDays * 10) / 10,
        'r/' + post.subreddit,
        title,
        hits(text, JOB_TERMS).length ? 'job' : 'project/gig',
        intent.evidence,
        'Remote',
        remote.length ? remote.slice(0, 3).join(', ')
                      : 'r/' + post.subreddit + ' is remote-only by subreddit rule',
        pay || 'not stated',
        'u/' + post.author,
        'https://www.reddit.com' + post.permalink,
        contactHint(post, body),
        post.num_comments === null ? 'n/a' : post.num_comments,
        Math.round(score * 100) / 100,
        reasons.join('; '),
        body.slice(0, 280)
      ]
    });
  }

  leads.sort(function (a, b) { return b.score - a.score; });

  // Deduplicate: same post id, same author re-posting, or an identical
  // distinctive title from any author.
  var seen = {};
  var unique = [];
  var duplicates = 0;
  for (var k = 0; k < leads.length; k++) {
    var lead = leads[k];
    var keys = ['id:' + lead.id];
    if (lead.signature) {
      keys.push('at:' + lead.author + '|' + lead.signature);
      if (lead.signature.split(' ').length >= 4) { keys.push('t:' + lead.signature); }
    }
    var clash = false;
    for (var x = 0; x < keys.length; x++) { if (seen[keys[x]]) { clash = true; } }
    if (clash) { duplicates++; continue; }
    for (var y = 0; y < keys.length; y++) { seen[keys[y]] = true; }
    unique.push(lead);
  }

  // Return every lead, best first. The caller decides how many are new and
  // how many to keep - in append mode that is not knowable here.
  var rows = [];
  for (var r = 0; r < unique.length; r++) {
    rows.push(unique[r].row);
  }
  return {
    rows: rows,
    rejected: rejected,
    stats: { unique: unique.length, duplicates: duplicates, reasons: reasonCounts }
  };
}

// --- Spreadsheet access -----------------------------------------------------

function getSpreadsheet() {
  var spreadsheet = null;
  try {
    spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  } catch (e) {
    spreadsheet = null;
  }
  if (!spreadsheet && CONFIG.SPREADSHEET_ID) {
    spreadsheet = SpreadsheetApp.openById(CONFIG.SPREADSHEET_ID);
  }
  if (!spreadsheet) {
    throw new Error('No spreadsheet available. Open this script from a Google ' +
                    'Sheet (Extensions > Apps Script), or set CONFIG.SPREADSHEET_ID.');
  }
  return spreadsheet;
}

function sheetNamed(name, spreadsheet) {
  var target = spreadsheet || getSpreadsheet();
  return target.getSheetByName(name) || target.insertSheet(name);
}

function toast(message) {
  // Time-driven runs have no UI; a failed toast must not fail the run.
  try {
    getSpreadsheet().toast(message, 'Reddit Leads', 10);
  } catch (e) {
    // no-op
  }
}

// --- Leads sheet (append-only) ----------------------------------------------

function ensureLeadsSheet(spreadsheet) {
  var sheet = sheetNamed(CONFIG.SHEET_NAME, spreadsheet);
  var headerMatches = false;

  if (sheet.getLastRow() >= 1 && sheet.getLastColumn() === SHEET_HEADERS.length) {
    var current = sheet.getRange(1, 1, 1, SHEET_HEADERS.length).getValues()[0];
    headerMatches = current.join('|') === SHEET_HEADERS.join('|');
  }

  // A sheet written by an older version has different columns; rebuilding it
  // is the only safe option, since rows would otherwise be misaligned.
  if (!headerMatches) {
    sheet.clear();
    sheet.getRange(1, 1, 1, SHEET_HEADERS.length).setValues([SHEET_HEADERS])
         .setFontWeight('bold').setBackground('#d9e8fb');
    sheet.setFrozenRows(1);
    sheet.autoResizeColumns(1, SHEET_HEADERS.length);
  }
  return sheet;
}

/** "https://www.reddit.com/r/x/comments/abc123/slug/" -> "abc123" */
function postIdFromUrl(url) {
  var match = String(url || '').match(/\/comments\/([a-z0-9]+)/i);
  return match ? match[1] : '';
}

/**
 * Which posts the sheet already holds.
 *
 * Read back from the sheet rather than kept in script properties, so it stays
 * correct when rows are deleted by hand and cannot drift out of sync.
 */
function existingPostIds(sheet) {
  var ids = {};
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) { return ids; }

  var column = SHEET_HEADERS.indexOf('post_url') + 1;
  var values = sheet.getRange(2, column, lastRow - 1, 1).getValues();
  for (var i = 0; i < values.length; i++) {
    var id = postIdFromUrl(values[i][0]);
    if (id) { ids[id] = true; }
  }
  return ids;
}

/** Write whole sheet rows under the header, newest on top. */
function prependRows(sheet, fullRows) {
  if (!fullRows.length) { return; }
  sheet.insertRowsAfter(1, fullRows.length);

  var range = sheet.getRange(2, 1, fullRows.length, SHEET_HEADERS.length);
  range.setValues(fullRows);

  // A row inserted directly beneath the header inherits the header's
  // formatting, which is why new leads arrived bold and shaded. Reset it.
  range.setFontWeight('normal')
       .setFontStyle('normal')
       .setBackground(null)
       .setFontColor(null);

  sheet.getRange(2, 1, fullRows.length, 1).setNumberFormat('yyyy-mm-dd hh:mm');
}

/** New leads go directly under the header, stamped with when we found them. */
function prependLeads(sheet, rows, stamp) {
  var full = [];
  for (var i = 0; i < rows.length; i++) {
    full.push([stamp].concat(rows[i]));
  }
  prependRows(sheet, full);
}

/**
 * Bring the mirror spreadsheet up to date with the main sheet.
 *
 * This compares the two sheets rather than copying whatever the current run
 * happened to find. Copying only the run's new leads left the mirror
 * permanently empty whenever the main sheet was already up to date - which is
 * the normal case, since most runs find nothing new.
 *
 * Rows are copied verbatim, keeping their original first_seen, so the mirror
 * reads the same as the source. It is never pruned, and notes added in spare
 * columns to the right travel with their row as later leads push it down.
 *
 * Because it reconciles against the source, a row deleted from the mirror
 * comes back on the next run. To set a lead aside, mark it in a spare column
 * instead of deleting it.
 */
function syncMirror() {
  if (!CONFIG.MIRROR_SPREADSHEET_ID) { return 0; }

  var source = ensureLeadsSheet();
  var lastRow = source.getLastRow();
  if (lastRow < 2) { return 0; }

  var mirror = ensureLeadsSheet(SpreadsheetApp.openById(CONFIG.MIRROR_SPREADSHEET_ID));
  var alreadyThere = existingPostIds(mirror);
  var urlColumn = SHEET_HEADERS.indexOf('post_url');

  var rows = source.getRange(2, 1, lastRow - 1, SHEET_HEADERS.length).getValues();
  var missing = [];
  for (var i = 0; i < rows.length; i++) {
    var id = postIdFromUrl(rows[i][urlColumn]);
    if (id && !alreadyThere[id]) { missing.push(rows[i]); }
  }

  prependRows(mirror, missing);
  return missing.length;
}

/** Menu action: copy anything the mirror is missing, without fetching. */
function syncMirrorNow() {
  if (!CONFIG.MIRROR_SPREADSHEET_ID) {
    var warning = 'No mirror configured. Set CONFIG.MIRROR_SPREADSHEET_ID first.';
    Logger.log(warning);
    toast(warning);
    return warning;
  }
  var copied = syncMirror();
  var message = copied ? copied + ' lead(s) copied to the mirror.'
                       : 'Mirror already had every lead.';
  Logger.log(message);
  toast(message);
  return message;
}

/** Stop the sheet growing without bound. */
function pruneOldLeads(sheet) {
  if (!CONFIG.KEEP_DAYS) { return 0; }
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) { return 0; }

  var stamps = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  var cutoff = Date.now() - CONFIG.KEEP_DAYS * 86400000;
  var removed = 0;

  // Bottom-up, so deleting a row does not shift the ones still to check.
  for (var i = stamps.length - 1; i >= 0; i--) {
    var value = stamps[i][0];
    var ms = (value instanceof Date) ? value.getTime() : Date.parse(value);
    if (ms && ms < cutoff) {
      sheet.deleteRow(i + 2);
      removed++;
    }
  }
  return removed;
}

// --- Audit and log sheets ---------------------------------------------------

/**
 * Everything that was filtered out, with the reason. Use it to check the
 * filter is not throwing away good leads - if it is, the reason column says
 * which rule to loosen.
 */
function writeAuditSheet(rejected) {
  var sheet = sheetNamed(CONFIG.AUDIT_SHEET_NAME);
  sheet.clear();
  var headers = ['subreddit', 'title', 'rejected_because', 'post_url'];
  sheet.getRange(1, 1, 1, headers.length).setValues([headers])
       .setFontWeight('bold').setBackground('#fbe4e4');
  if (rejected.length) {
    sheet.getRange(2, 1, rejected.length, headers.length).setValues(rejected);
  }
  sheet.setFrozenRows(1);
  sheet.autoResizeColumns(1, headers.length);
  return sheet;
}

/** One row per run, so an unattended job cannot fail silently. */
function logRun(entry) {
  var sheet = sheetNamed(CONFIG.LOG_SHEET_NAME);
  var headers = ['when', 'posts_scanned', 'leads_matched', 'new_added', 'mirrored', 'pruned', 'notes'];

  if (sheet.getLastRow() < 1 || sheet.getLastColumn() !== headers.length) {
    sheet.clear();
    sheet.getRange(1, 1, 1, headers.length).setValues([headers])
         .setFontWeight('bold').setBackground('#e4f0e4');
    sheet.setFrozenRows(1);
  }

  sheet.insertRowAfter(1);
  sheet.getRange(2, 1, 1, headers.length).setValues([entry]);
  sheet.getRange(2, 1, 1, 1).setNumberFormat('yyyy-mm-dd hh:mm');

  var lastRow = sheet.getLastRow();
  if (lastRow > 201) { sheet.deleteRows(202, lastRow - 201); }
  return sheet;
}

function notifyNewLeads(rows) {
  if (!CONFIG.EMAIL_ON_NEW_LEADS || !rows.length) { return; }
  var to = CONFIG.EMAIL_TO || Session.getEffectiveUser().getEmail();
  if (!to) { return; }

  var titleAt = LEAD_HEADERS.indexOf('title');
  var subAt = LEAD_HEADERS.indexOf('subreddit');
  var payAt = LEAD_HEADERS.indexOf('pay_or_budget');

  var lines = [];
  for (var i = 0; i < rows.length; i++) {
    lines.push('- ' + rows[i][titleAt] +
               ' (' + rows[i][subAt] + ', ' + rows[i][payAt] + ')\n  ' +
               rows[i][URL_INDEX]);
  }

  MailApp.sendEmail(to,
    rows.length + ' new AI remote lead' + (rows.length === 1 ? '' : 's'),
    lines.join('\n\n'));
}

// --- Scheduling -------------------------------------------------------------

/** Install the twice-daily runs. Safe to call again; it replaces its own. */
function installTriggers() {
  removeTriggers();
  for (var i = 0; i < CONFIG.RUN_HOURS.length; i++) {
    ScriptApp.newTrigger('generateLeads')
             .timeBased()
             .atHour(CONFIG.RUN_HOURS[i])
             .everyDays(1)
             .create();
  }
  var message = 'Auto-update on: runs daily near ' + CONFIG.RUN_HOURS.join(':00 and ') +
                ':00 (' + Session.getScriptTimeZone() + ').';
  Logger.log(message);
  toast(message);
  return message;
}

function removeTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  var removed = 0;
  // Backwards, so removal can never disturb the iteration.
  for (var i = triggers.length - 1; i >= 0; i--) {
    if (triggers[i].getHandlerFunction() === 'generateLeads') {
      ScriptApp.deleteTrigger(triggers[i]);
      removed++;
    }
  }
  var message = removed ? 'Auto-update off (' + removed + ' trigger(s) removed).'
                        : 'Auto-update was not on.';
  Logger.log(message);
  return message;
}

function autoUpdateStatus() {
  var triggers = ScriptApp.getProjectTriggers();
  var count = 0;
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'generateLeads') { count++; }
  }
  var message = count
    ? count + ' scheduled run(s) installed, near ' + CONFIG.RUN_HOURS.join(':00 and ') +
      ':00 ' + Session.getScriptTimeZone() + '. See the "' + CONFIG.LOG_SHEET_NAME + '" sheet.'
    : 'Auto-update is off. Use "Turn on twice-daily auto-update".';
  Logger.log(message);
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (e) {
    toast(message);
  }
  return message;
}

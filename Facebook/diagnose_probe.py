#!/usr/bin/env python3
"""
diagnose_probe.py <group_url>

Final probe: find message-bearing elements in the LIVE rendered feed and
identify the correct post-container ancestor + how to reach a permalink.
"""

import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

group_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.facebook.com/share/g/17sYQhzdR4/"

driver = collector.attach_to_user_chrome()
driver.get(group_url)

# Wait until the feed genuinely has substantive text painted.
for _ in range(25):
    time.sleep(1.5)
    n = driver.execute_script("""
      const f = document.querySelector('[role="feed"]');
      return f ? (f.innerText || '').length : 0;
    """)
    if n > 500:
        break
print("Feed innerText length after wait:", n)

print("\n=== A. Elements carrying message/story data attributes ===")
attrs = driver.execute_script("""
const sel = ['[data-ad-preview="message"]', '[data-ad-rendering-role="story_message"]',
             '[data-ad-comet-preview="message"]', '[data-ad-rendering-role]'];
const out = {};
sel.forEach(s => {
  out[s] = Array.from(document.querySelectorAll(s)).slice(0, 8).map(el => ({
    role: el.getAttribute('data-ad-rendering-role'),
    textLen: (el.innerText || '').length,
    preview: (el.innerText || '').slice(0, 150).replace(/\\n/g, ' | '),
  }));
});
return out;
""")
print(json.dumps(attrs, indent=2)[:5000])

print("\n=== B. Feed descendants with substantive text (depth<=3 under feed) ===")
kids = driver.execute_script("""
const f = document.querySelector('[role="feed"]');
if (!f) return [];
const out = [];
function walk(el, d) {
  if (d > 3) return;
  for (const c of el.children) {
    const t = (c.innerText || '').trim();
    if (t.length > 60) {
      out.push({depth: d, tag: c.tagName, role: c.getAttribute('role'),
                textLen: t.length, preview: t.slice(0, 180).replace(/\\n/g, ' | ')});
    }
    walk(c, d + 1);
  }
}
walk(f, 0);
return out.slice(0, 20);
""")
print(json.dumps(kids, indent=2)[:6000])

print("\n=== C. All links whose href looks like a post permalink ===")
links = driver.execute_script("""
return Array.from(document.querySelectorAll('a[href]'))
  .map(a => a.href)
  .filter(h => /\\/posts\\/|\\/permalink\\/|story_fbid|\\/share\\/p\\//.test(h))
  .slice(0, 25);
""")
print(json.dumps(links, indent=2)[:3000])

print("\n=== D. Whole-feed innerText (what a human sees) ===")
feed_text = driver.execute_script("""
const f = document.querySelector('[role="feed"]');
return f ? (f.innerText || '') : '';
""")
print(feed_text[:3000])

print("=== DONE ===")

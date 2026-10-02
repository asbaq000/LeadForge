#!/usr/bin/env python3
"""
diagnose_feed.py <group_url>

Dumps the [role="feed"] children (the real post containers) so we can
confirm the correct selector and see actual post text/timestamps.
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

for _ in range(20):
    time.sleep(1.5)
    if driver.execute_script("return document.body.innerText.length;") > 3000:
        break

print("=== Feed children (candidate post containers) ===")
feeds = driver.execute_script("""
return Array.from(document.querySelectorAll('[role="feed"]')).map((f, fi) => ({
  feedIndex: fi,
  childCount: f.children.length,
  children: Array.from(f.children).map((c, i) => ({
    i: i,
    tag: c.tagName,
    role: c.getAttribute('role'),
    ariaLabel: c.getAttribute('aria-label'),
    hasArticleDescendant: !!c.querySelector('div[role="article"]'),
    isLoading: !!c.querySelector('[data-visualcompletion="loading-state"]'),
    textLen: (c.innerText || '').length,
    preview: (c.innerText || '').slice(0, 200).replace(/\\n/g, ' | '),
  })),
}));
""")
print(json.dumps(feeds, indent=2)[:9000])

print("\n=== Sort control / current ordering ===")
sort = driver.execute_script("""
const el = Array.from(document.querySelectorAll('div,span'))
  .find(e => /sort group feed by/i.test(e.innerText || '') && e.children.length < 6);
return el ? (el.innerText || '').slice(0, 200) : 'not found';
""")
print(sort)

print("\n=== Scroll down and re-check how many feed children appear ===")
for r in range(6):
    driver.execute_script("window.scrollBy(0, window.innerHeight * 0.85);")
    time.sleep(2.5)
    stats = driver.execute_script("""
    const f = document.querySelector('[role="feed"]');
    if (!f) return {children: 0, real: 0};
    const kids = Array.from(f.children);
    return {
      children: kids.length,
      real: kids.filter(c => (c.innerText || '').trim().length > 40).length,
    };
    """)
    print(f"  after gentle scroll {r+1}: feed children={stats['children']}, with real text={stats['real']}")

print("\n=== Final: all feed children with substantive text ===")
final = driver.execute_script("""
const f = document.querySelector('[role="feed"]');
if (!f) return [];
return Array.from(f.children)
  .map((c, i) => ({i: i, textLen: (c.innerText||'').length,
                   preview: (c.innerText||'').slice(0, 300).replace(/\\n/g, ' | ')}))
  .filter(x => x.textLen > 40);
""")
print(json.dumps(final, indent=2)[:9000])
print("=== DONE ===")

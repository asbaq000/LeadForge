#!/usr/bin/env python3
"""
diagnose_dom.py <group_url>

Inspects the LIVE rendered DOM of a group feed to determine the correct
post-container selector: what roles exist, where known post text actually
lives, and which ancestor element represents a whole post.
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

# Wait for feed content to actually paint.
for _ in range(20):
    time.sleep(1.5)
    n = driver.execute_script(
        "return document.querySelectorAll('div[role=\"article\"]').length;")
    body_text = driver.execute_script("return document.body.innerText.length;")
    if body_text > 3000:
        break
print("Waited for render. role=article count:", n)

print("\n=== 1. Count of elements by [role] in the feed area ===")
roles = driver.execute_script("""
const counts = {};
document.querySelectorAll('[role]').forEach(el => {
  const r = el.getAttribute('role');
  counts[r] = (counts[r] || 0) + 1;
});
return counts;
""")
print(json.dumps(roles, indent=2))

print("\n=== 2. Locate known post text and describe its ancestors ===")
info = driver.execute_script("""
const needle = 'looking for a video editor';
const all = Array.from(document.querySelectorAll('div,span'));
const hit = all.find(el =>
  el.children.length === 0 &&
  (el.innerText || '').toLowerCase().includes(needle));
if (!hit) return {found: false};

const chain = [];
let cur = hit;
for (let i = 0; i < 18 && cur; i++) {
  chain.push({
    depth: i,
    tag: cur.tagName,
    role: cur.getAttribute('role'),
    ariaLabel: cur.getAttribute('aria-label'),
    dataAdPreview: cur.getAttribute('data-ad-preview'),
    dataVisualcompletion: cur.getAttribute('data-visualcompletion'),
    textLen: (cur.innerText || '').length,
    textPreview: (cur.innerText || '').slice(0, 70).replace(/\\n/g, ' '),
  });
  cur = cur.parentElement;
}
return {found: true, chain: chain};
""")
print(json.dumps(info, indent=2)[:4000])

print("\n=== 3. All role=article elements: text length + preview ===")
arts = driver.execute_script("""
return Array.from(document.querySelectorAll('div[role="article"]')).map((el, i) => ({
  i: i,
  ariaLabel: el.getAttribute('aria-label'),
  isLoading: !!el.querySelector('[data-visualcompletion="loading-state"]'),
  textLen: (el.innerText || '').length,
  preview: (el.innerText || '').slice(0, 100).replace(/\\n/g, ' '),
}));
""")
print(json.dumps(arts, indent=2)[:4000])

print("\n=== 4. Feed-level structure: what wraps the post list? ===")
feed = driver.execute_script("""
const feeds = Array.from(document.querySelectorAll('[role="feed"]'));
return feeds.map(f => ({
  role: f.getAttribute('role'),
  childCount: f.children.length,
  childRoles: Array.from(f.children).slice(0, 25).map(c => ({
    tag: c.tagName,
    role: c.getAttribute('role'),
    textLen: (c.innerText || '').length,
    preview: (c.innerText || '').slice(0, 80).replace(/\\n/g, ' '),
  })),
}));
""")
print(json.dumps(feed, indent=2)[:6000])

print("=== DONE ===")

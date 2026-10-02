#!/usr/bin/env python3
"""diagnose_window.py -- does a taller window render more posts?"""

import importlib.util
import sys
import time

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.facebook.com/share/g/17sYQhzdR4/"
driver = collector.attach_to_user_chrome()

print("Current window size:", driver.get_window_size())
driver.set_window_size(1500, 1200)
try:
    driver.maximize_window()
except Exception as e:
    print("maximize failed:", e)
print("New window size:", driver.get_window_size())

driver.get(url)
time.sleep(5)

print("viewport:", driver.execute_script(
    "return {w: window.innerWidth, h: window.innerHeight, "
    "sh: document.body.scrollHeight};"))

for i in range(12):
    n = driver.execute_script(
        "return document.querySelectorAll('[data-ad-rendering-role=\"story_message\"]').length;")
    conts = len(collector.find_post_containers(driver))
    sh = driver.execute_script("return document.body.scrollHeight;")
    print(f"  round {i}: story_messages={n:3d} containers={conts:3d} scrollHeight={sh}")
    driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
    time.sleep(3)

print("\nFinal unique post texts:")
texts = driver.execute_script("""
return Array.from(document.querySelectorAll('[data-ad-rendering-role="story_message"]'))
  .map(e => (e.innerText || '').slice(0, 90).replace(/\\n/g, ' '));
""")
for t in texts:
    print("  -", t.encode("ascii", "replace").decode("ascii"))

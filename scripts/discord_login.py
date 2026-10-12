import sys
sys.path.insert(0, sys.argv[1]); prof = sys.argv[2]
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(prof, headless=False, viewport={"width": 1400, "height": 1000})
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://discord.com/channels/726870908568076380")
    print("window open; waiting for user login / search box", flush=True)
    try:
        pg.wait_for_selector('div[role="combobox"][aria-label^="Search"]', timeout=600000)
        print("READY: search box found, url", pg.url, flush=True)
    except Exception as e:
        print("TIMEOUT waiting for search box; url", pg.url, flush=True)
    ctx.close()

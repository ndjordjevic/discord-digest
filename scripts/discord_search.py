import sys
sys.path.insert(0, sys.argv[1]); prof = sys.argv[2]; out = sys.argv[3]; queries = sys.argv[4:]
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(prof, headless=False, viewport={"width": 1400, "height": 1000})
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://discord.com/channels/726870908568076380")
    pg.wait_for_selector('div[role="combobox"][aria-label^="Search"]', timeout=30000)
    box = pg.locator('div[role="combobox"][aria-label^="Search"]').first
    with open(out, "w") as f:
        for q in queries:
            box.click()
            pg.keyboard.press("ControlOrMeta+a")
            pg.keyboard.press("Backspace")
            pg.keyboard.type(q, delay=30)
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(4000)
            txt = pg.evaluate("() => { const r = document.querySelector('[aria-label=\"Search Results\"]') || document.querySelector('section[class*=searchResults]'); return r ? r.innerText : 'NO RESULTS PANEL'; }")
            f.write(f"\n\n######## QUERY: {q}\n{txt}\n")
            print(q, "->", len(txt), "chars", flush=True)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(1500)
    ctx.close()

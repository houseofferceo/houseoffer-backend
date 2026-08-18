"""Cookie-consent acceptance checks (17 Aug brief, item 3) — headless browser.

Drives templates/_consent_ga.html (pure static — no Jinja) in Chromium and
asserts the brief's acceptance criteria mechanically:

  C1  BEFORE any choice: banner visible, gtag.js NOT loaded, no request to
      googletagmanager.com attempted, no cookies set;
  C2  Accept: gtag.js load attempted, choice persisted, banner gone;
  C3  persistence: on reload GA re-applies and the banner stays gone;
  C4  Reject: as easy as Accept — one click, persisted, gtag.js never
      requested, banner stays gone on reload.

Requests to googletagmanager.com are intercepted and aborted, so the test is
hermetic (no real Google traffic) while still observing the attempt.

Run:  python3 tests/test_consent_banner.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
INCLUDE = os.path.join(HERE, "..", "templates", "_consent_ga.html")

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


with open(INCLUDE) as f:
    block = f.read()
assert "{%" not in block and "{{" not in block, "include must stay Jinja-free (pasteable to the marketing site)"

page_html = f"<!DOCTYPE html><html><head><title>consent test</title></head><body><h1>Page</h1>{block}</body></html>"
test_page = os.path.join(HERE, ".consent_test_page.html")
with open(test_page, "w") as f:
    f.write(page_html)
url = "file://" + os.path.abspath(test_page)

gtm_attempts = []


def make_page(ctx):
    p = ctx.new_page()
    p.route("**://www.googletagmanager.com/**",
            lambda route: (gtm_attempts.append(route.request.url), route.abort()))
    return p


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium"
                                 if os.path.exists("/opt/pw-browsers/chromium") else None)

    print("[1] Before any choice (C1)")
    ctx = browser.new_context()
    page = make_page(ctx)
    page.goto(url)
    page.wait_for_timeout(400)
    check("banner visible", page.is_visible("#ho-consent"))
    check("gtag.js not in DOM",
          page.evaluate("document.querySelectorAll('script[src*=googletagmanager]').length") == 0)
    check("no googletagmanager request attempted", len(gtm_attempts) == 0)
    check("no cookies set", len(ctx.cookies()) == 0)
    check("Accept and Reject both single visible buttons",
          page.is_visible("#ho-consent-accept") and page.is_visible("#ho-consent-reject"))

    print("[2] Accept (C2)")
    page.click("#ho-consent-accept")
    page.wait_for_timeout(400)
    check("gtag.js load attempted after accept",
          any("G-JXRNYV3HF4" in u for u in gtm_attempts))
    check("choice persisted as granted",
          page.evaluate("localStorage.getItem('ho_consent_v1')") == "granted")
    check("banner dismissed", not page.is_visible("#ho-consent"))

    print("[3] Persistence on reload (C3)")
    gtm_attempts.clear()
    page.reload()
    page.wait_for_timeout(400)
    check("GA re-applies on next page load", any("G-JXRNYV3HF4" in u for u in gtm_attempts))
    check("banner stays gone", not page.is_visible("#ho-consent"))
    ctx.close()

    print("[4] Reject (C4)")
    gtm_attempts.clear()
    ctx = browser.new_context()
    page = make_page(ctx)
    page.goto(url)
    page.wait_for_timeout(200)
    page.click("#ho-consent-reject")
    page.wait_for_timeout(400)
    check("choice persisted as denied",
          page.evaluate("localStorage.getItem('ho_consent_v1')") == "denied")
    check("banner dismissed", not page.is_visible("#ho-consent"))
    page.reload()
    page.wait_for_timeout(400)
    check("no gtag request ever attempted", len(gtm_attempts) == 0)
    check("banner stays gone after reject + reload", not page.is_visible("#ho-consent"))
    check("still no cookies", len(ctx.cookies()) == 0)
    ctx.close()
    browser.close()

os.unlink(test_page)
print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

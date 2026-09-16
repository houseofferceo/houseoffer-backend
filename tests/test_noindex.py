"""Backend host stays out of search results — no network, no credits.

houseoffer-backend.onrender.com serves personal reports, vote links and internal
tooling. The canonical public content lives on houseoffer.uk (Netlify).

  N1  every response from this app carries X-Robots-Tag: noindex, nofollow —
      HTML report page, JSON, plain text, a 301 and a 404 alike
  N2  /robots.txt: 200, text/plain, crawling ALLOWED (a Disallow would stop
      Google re-crawling already-indexed URLs and seeing the header; held)
  N3  /white-paper and /white-paper/ 301 to the canonical houseoffer.uk page
  N4  the Netlify SEO pages are not routes on this host at all (404 here), so a
      header emitted by this app cannot reach them
  N5  the header is an indexing control only: report pages still render 200
      and the JSON/API responses are unchanged apart from the header

Run:  python3 tests/test_noindex.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-noindex-test-")

import app as ho  # noqa: E402

ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-noindex-votes-")
ho.app.testing = True

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


HEADER = "X-Robots-Tag"
VALUE = "noindex, nofollow"
NETLIFY_SEO_PAGES = ("/property-value/", "/offer-strategy/", "/negotiation/",
                     "/faq/", "/buying-hub/", "/oiro-meaning/")

FAKE_REPORT = {
    "asking_price": 650_000, "asking_price_formatted": "£650,000",
    "local_avg_sold": 500_000, "local_avg_sold_formatted": "£500,000", "sold_diff_pct": 30.0,
    "postcode": "RG1 5BX", "postcode_used": "RG1 5BX", "bedrooms": 3,
    "property_type": "terraced", "property_type_label": "terraced", "verdict": "overpriced",
    "weighted_low": 560_000, "weighted_high": 640_000, "weighted_midpoint": 600_000,
    "weighted_low_formatted": "£560,000", "weighted_high_formatted": "£640,000",
    "weighted_midpoint_formatted": "£600,000",
    "open_offer": 575_000, "target_price": 600_000, "walk_away": 620_000,
    "confidence_score": "high", "comparables_count": 12, "comparables": [],
    "football_field": [], "country": "England", "days_on_market": None,
    "address_resolution": "exact", "generated": "16 September 2026",
}
ho.build_report_data = lambda **kw: dict(FAKE_REPORT)
ho.post_to_sheets = lambda payload: None
ho.log_event = lambda rid, event_type, extra=None: None
ho.send_report_email = lambda *a, **k: True
ho.notify_owner = lambda *a, **k: None

rid = "0b0b0b0b00000001"
ho._run_free_build(rid, {
    "property_url": "https://www.rightmove.co.uk/properties/87654321",
    "email": "buyer@example.com", "buyer_estimate": "",
    "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
    "asking_price": 650_000, "postcode": "RG1 5BX", "bedrooms": "3", "property_type": "terraced",
})
client = ho.app.test_client()

print("[N1] X-Robots-Tag on every kind of response")
cases = [
    (f"/r/{rid}", "HTML report page", 200),
    ("/version", "JSON endpoint", 200),
    ("/health", "health endpoint", 200),
    ("/robots.txt", "plain text", 200),
    ("/white-paper", "301 redirect", 301),
    ("/no-such-route-zz9", "404", 404),
]
for path, label, expected in cases:
    r = client.get(path)
    check(f"{label} {path}: status {expected}", r.status_code == expected, str(r.status_code))
    check(f"{label} {path}: {HEADER} = '{VALUE}'", r.headers.get(HEADER) == VALUE, str(r.headers.get(HEADER)))

print("[N2] robots.txt keeps crawling ALLOWED so the noindex header can be seen (Disallow held)")
r = client.get("/robots.txt")
check("text/plain", r.mimetype == "text/plain", str(r.mimetype))
check("User-agent: * / Allow: /", r.get_data(as_text=True) == "User-agent: *\nAllow: /\n", repr(r.get_data(as_text=True)))
check("no Disallow rule yet", "Disallow" not in r.get_data(as_text=True))

print("[N3] legacy backend white paper redirects to the canonical page")
for path in ("/white-paper", "/white-paper/"):
    r = client.get(path)
    check(f"{path} -> 301 https://houseoffer.uk/white-paper/",
          r.status_code == 301 and r.headers.get("Location") == "https://houseoffer.uk/white-paper/",
          f"{r.status_code} {r.headers.get('Location')}")

print("[N4] the Netlify SEO pages are not served by this host")
for path in NETLIFY_SEO_PAGES:
    r = client.get(path)
    check(f"{path} is not a backend route (404 here)", r.status_code == 404, str(r.status_code))

print("[N5] indexing control only: the report still renders and JSON is unchanged")
r = client.get(f"/r/{rid}")
check("report page renders 200 with the header", r.status_code == 200 and r.headers.get(HEADER) == VALUE)
check("report page still carries the valuation", "£600,000" in r.get_data(as_text=True))
r = client.get("/version")
check("/version still JSON", r.mimetype == "application/json", str(r.mimetype))

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

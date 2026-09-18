"""ADMIN_KEY: no fallback default (CEO 18 Sep) — no network, no credits.

Until now every admin gate read os.environ.get("ADMIN_KEY", "set-an-admin-key"),
so with the variable unset on Render the literal default in this public repo
opened every /admin/*, /batch-* and gated /debug-* route. Now one helper,
_admin_ok(), refuses everything unless ADMIN_KEY is configured and matches.

  K1  ADMIN_KEY unset: the old literal default, an empty key and no key are all
      refused (401, or 403 on the preview/batch routes) on a spread of gated
      routes — before any build or credit is spent
  K2  ADMIN_KEY set: wrong key 401, right key passes
  K3  POST /report with tier=paid and the old default no longer buys a paid
      build: it is built as the free tier
  K4  the literal default is gone from app.py and every gate uses _admin_ok
  K5  _admin_ok tolerates None and is constant-time (hmac.compare_digest)

Run:  python3 tests/test_admin_key.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-adminkey-test-")
os.environ.pop("ADMIN_KEY", None)

import app as ho  # noqa: E402

ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-adminkey-votes-")
ho.app.testing = True
client = ho.app.test_client()

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


OLD_DEFAULT = "set-an-admin-key"
GATED = ["/admin/recent", "/admin/events/abcdef1234", "/admin/sheets-probe", "/admin/unlock/abcdef1234",
         "/admin/clear-profile/abcdef1234", "/preview-free", "/preview-paid", "/batch-resolve-test",
         "/batch-valuation-test"]

print("[K1] ADMIN_KEY unset: nothing gets through")
for path in GATED:
    for key in (OLD_DEFAULT, "", None):
        url = path if key is None else f"{path}?key={key}"
        r = client.get(url)
        check(f"{url!s:48s} -> refused (401/403)", r.status_code in (401, 403), str(r.status_code))
check("_admin_ok(old default) is False when unset", ho._admin_ok(OLD_DEFAULT) is False)

print("[K2] ADMIN_KEY set")
os.environ["ADMIN_KEY"] = "test-key-4f9c1c2e7b8a"
r = client.get("/admin/recent?key=wrong-key")
check("wrong key -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/admin/recent?key=test-key-4f9c1c2e7b8a")
check("right key -> 200 JSON", r.status_code == 200 and r.is_json, f"{r.status_code} {r.mimetype}")
check("_admin_ok(right) True / _admin_ok(wrong) False", ho._admin_ok("test-key-4f9c1c2e7b8a") is True and ho._admin_ok("wrong-key") is False)
os.environ.pop("ADMIN_KEY", None)

print("[K3] POST /report with tier=paid and the old default builds the FREE tier")
captured = {}


def fake_build(**kw):
    captured.update(kw)
    return {"postcode": "RG1 5BX", "asking_price": 650_000, "asking_price_formatted": "£650,000",
            "verdict": "fair", "comparables_count": 0, "comparables": [], "football_field": [],
            "weighted_midpoint": None, "confidence_score": "low", "country": "England",
            "property_type": "terraced", "property_type_label": "terraced", "low_gap_tier": None,
            "low_gap_pct": None, "low_reasons": ["the local sold evidence diverges sharply from the asking price"]}


ho.build_report_data = fake_build
ho.scrape_property_url = lambda *a, **k: {}
r = client.post("/report", json={"property_url": "https://www.rightmove.co.uk/properties/1", "asking_price": 650000,
                                 "postcode": "RG1 5BX", "bedrooms": 3, "property_type": "terraced",
                                 "tier": "paid", "key": OLD_DEFAULT})
check("request handled (not a 5xx)", r.status_code < 500, str(r.status_code))
check("built as the free tier, not paid", captured.get("tier") == "free", str(captured.get("tier")))

print("[K4] no literal default left; every gate uses _admin_ok")
src = open(os.path.join(os.path.dirname(__file__), "..", "app.py"), encoding="utf-8").read()
check("'set-an-admin-key' absent from app.py", OLD_DEFAULT not in src)
check("no os.environ.get('ADMIN_KEY', ...) with a default", not re.search(r'os\.environ\.get\("ADMIN_KEY",\s*"', src))
n = src.count("_admin_ok(")
check(f"_admin_ok used at every gate ({n} uses, expected >= 23 incl. the definition)", n >= 23, str(n))

print("[K5] _admin_ok is None-tolerant and constant-time")
check("None -> False", ho._admin_ok(None) is False)
check("uses hmac.compare_digest", "hmac.compare_digest" in src[src.find("def _admin_ok"):src.find("def _admin_ok") + 600])

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

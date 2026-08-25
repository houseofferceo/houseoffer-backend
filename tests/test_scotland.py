"""Scotland handling (CEO decision, 25 Aug) — no network, no credits.

  C1  _postcode_country resolves via postcodes.io and caches on disk
      (second lookup makes NO network call);
  C2  failures return None (caller falls back to England & Wales default);
  C3  the payload attribution strings never claim HM Land Registry for
      Scotland (string-level check of the build_report_data source).

Run:  python3 tests/test_scotland.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-scot-test-")

import app as ho  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


calls = []


class FakeResp:
    def __init__(self, country):
        self.status_code = 200
        self._c = country

    def json(self):
        return {"result": {"country": self._c}}


real_get = ho.requests.get


def fake_get(url, **kw):
    calls.append(url)
    if "postcodes.io" not in url:
        raise AssertionError("unexpected outbound call: " + url)
    if "G40SZ" in url:
        return FakeResp("Scotland")
    if "BN435DL" in url:
        return FakeResp("England")
    raise ConnectionError("simulated outage")


ho.requests.get = fake_get

print("[1] Lookup + disk cache (C1)")
check("Scottish postcode resolves", ho._postcode_country("G4 0SZ") == "Scotland")
n = len(calls)
check("second lookup served from cache", ho._postcode_country("g4 0sz") == "Scotland"
      and len(calls) == n)
check("English postcode resolves", ho._postcode_country("BN43 5DL") == "England")
check("border-safe: result comes from lookup, not prefix", len(calls) == n + 1)

print("[2] Failure falls back to None (C2)")
check("outage returns None", ho._postcode_country("ZZ99 9ZZ") is None)
check("failure is not cached as a value",
      "ZZ999ZZ" not in json.load(open(ho.COUNTRY_CACHE_PATH)))

ho.requests.get = real_get

print("[3] Attribution strings (C3)")
src = open(os.path.join(os.path.dirname(__file__), "..", "app.py")).read()
i = src.find('"sold_data_attribution"')
block = src[i:i + 700]
check("Scottish branch cites Registers of Scotland",
      "Registers of Scotland" in block)
check("Scottish branch does not claim HM Land Registry",
      "HM Land Registry" not in block.split("if country")[0])
check("trio suppression + scotland anchor present in build",
      'trio_anchor = "scotland"' in src)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

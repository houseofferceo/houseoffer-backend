"""Sheets webhook contract — no network, no credits.

sheets/schema.json is the contract between app.py's post_to_sheets() payloads
and the Google Sheet. This test fails when app.py sends something the contract
does not list (or stops sending something it does), so the Apps Script and the
backend cannot drift apart silently again.

  S1  static: every `"type": "<t>"` payload literal in app.py has exactly the
      keys the schema lists for <t> (depth-1 keys of the dict literal)
  S2  dynamic: a real build posts a 'submission' and an owner-seed 'vote' whose
      keys match the schema, with the six audit fields populated
  S3  dynamic: log_event posts an 'event' matching the schema
  S4  the sheet's current header map only references keys the payload sends,
      and the keys with no column today are reported (the 24 Aug gap) —
      informational until the header-name-writing script ships
  S5  every type in the schema exists in app.py and vice versa

Run:  python3 tests/test_sheets_contract.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-sheets-test-")

import app as ho  # noqa: E402

ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-sheets-votes-")
REAL_LOG_EVENT = ho.log_event  # S3 drives the real function; S2 stubs the module attribute for the build

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


SCHEMA = json.load(open(os.path.join(ROOT, "sheets", "schema.json"), encoding="utf-8"))
TYPES = SCHEMA["types"]
SRC = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()


def payload_literals(src):
    """(type, line, depth-1 keys) for every `"type": "<t>"` dict literal in app.py."""
    out = []
    for m in re.finditer(r'"type": "([a-z_]+)"', src):
        start = src.rfind("{", 0, m.start())
        depth, i = 0, start
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = src[start:i + 1]
        keys, d = [], 0
        for t in re.finditer(r'[{}]|"([a-z_]+)":', body):
            if t.group(0) == "{":
                d += 1
            elif t.group(0) == "}":
                d -= 1
            elif d == 1:
                keys.append(t.group(1))
        out.append((m.group(1), src[:start].count("\n") + 1, keys))
    return out


print("[S1] static: every payload literal in app.py matches the schema")
literals = payload_literals(SRC)
check("found payload literals", len(literals) >= 7, str(len(literals)))
for ptype, line, keys in literals:
    expected = TYPES.get(ptype, {}).get("keys")
    check(f"line {line} type={ptype}: keys match schema", expected is not None and keys == expected,
          f"app.py {keys}\n         schema {expected}" if keys != expected else "")

print("[S2] dynamic: a real build posts submission + owner-seed vote per the schema")
SHEET = []
ho.post_to_sheets = lambda payload: SHEET.append(dict(payload))
ho.log_event = lambda rid, event_type, extra=None: None
ho.send_report_email = lambda *a, **k: True
ho.notify_owner = lambda *a, **k: None
REPORT = {
    "asking_price": 650_000, "asking_price_formatted": "£650,000", "postcode": "RG1 5BX",
    "property_type": "terraced", "property_type_label": "terraced", "verdict": "overpriced",
    "weighted_midpoint": 600_000, "weighted_low": 560_000, "weighted_high": 640_000,
    "weighted_midpoint_formatted": "£600,000", "local_avg_sold": 500_000, "local_avg_sold_formatted": "£500,000",
    "sold_diff_pct": 30.0, "confidence_score": "medium", "comparables_count": 12, "comparables": [],
    "comps_match_tier": "postcode", "valuation_asking_divergence_pct": -7.7, "country": "England",
    "football_field": [], "days_on_market": None, "generated": "18 September 2026", "bedrooms": 3,
}
ho.build_report_data = lambda **kw: dict(REPORT)
rid = "5e11e75c0000001"
ho._run_free_build(rid, {
    "property_url": "https://www.rightmove.co.uk/properties/87654321", "email": "buyer@example.com",
    "buyer_estimate": "620000", "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
    "asking_price": 650_000, "postcode": "RG1 5BX", "bedrooms": "3", "property_type": "terraced",
    "attribution": {"referrer": "https://www.google.com/", "utm_source": "google", "utm_medium": "cpc",
                    "utm_campaign": "sept", "utm_term": "", "utm_content": ""},
})
subs = [p for p in SHEET if p.get("type") == "submission"]
votes = [p for p in SHEET if p.get("type") == "vote"]
check("one submission posted", len(subs) == 1, str(len(subs)))
check("submission keys == schema (order included)", subs and list(subs[0]) == TYPES["submission"]["keys"],
      str(list(subs[0]) if subs else None))
audit = {"our_valuation": 600_000, "gap_vs_asking_pct": -7.7, "confidence": "MEDIUM", "comps_count": 12,
         "comps_tier": "postcode", "country": "England"}
check("six audit fields populated from the report", subs and all(subs[0].get(k) == v for k, v in audit.items()),
      str({k: subs[0].get(k) for k in audit} if subs else None))
check("attribution fields carried", subs and subs[0].get("utm_source") == "google" and subs[0].get("referrer") == "https://www.google.com/")
check("one owner-seed vote posted", len(votes) == 1 and votes[0].get("source") == "owner_seed", str(len(votes)))
check("vote keys == schema", votes and list(votes[0]) == TYPES["vote"]["keys"], str(list(votes[0]) if votes else None))

print("[S3] dynamic: log_event posts an event per the schema")
del SHEET[:]
REAL_LOG_EVENT(rid, "report_viewed", {"user_agent": "test"})  # the real function, captured before stubbing
events = [p for p in SHEET if p.get("type") == "event"]
check("event posted with schema keys", events and list(events[0]) == TYPES["event"]["keys"], str(list(events[0]) if events else "no event posted"))
check("event carries uuid + event_type + extra", events and events[0].get("uuid") == rid and events[0].get("event_type") == "report_viewed" and events[0].get("extra") == {"user_agent": "test"})

print("[S4] sheet header map vs payload keys (the 24 Aug gap, informational)")
for tab, header_map in SCHEMA["sheet_today"].items():
    if tab == "note":
        continue
    ptype = {"Submissions": "submission", "Votes": "vote", "Events": "event"}[tab]
    keys = set(TYPES[ptype]["keys"])
    mapped = set(header_map.values())
    check(f"{tab}: every mapped column is a key the payload sends", mapped <= keys, str(sorted(mapped - keys)))
    missing = [k for k in TYPES[ptype]["keys"] if k not in mapped and k != "type"]
    print(f"        {tab}: payload keys with NO column today ({len(missing)}): {missing}")
    if tab == "Submissions":
        check("Submissions gap is exactly the 6 audit + 6 attribution keys",
              missing == ["referrer", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                          "our_valuation", "gap_vs_asking_pct", "confidence", "comps_count", "comps_tier", "country"], str(missing))

print("[S5] the schema and app.py list the same set of types")
in_src = {t for t, _, _ in literals}
check("same types", in_src == set(TYPES), f"app.py {sorted(in_src)} vs schema {sorted(TYPES)}")

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

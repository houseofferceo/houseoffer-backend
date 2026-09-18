"""tools/backfill_sheet_audit_columns.py — read-only backfill extractor, no network, no credits.

  F1  a ready report on/after --since yields the twelve columns exactly as the
      submission payload derives them (schema order)
  F2  a report before --since is skipped; '' means all
  F3  a record without a report / not ready is skipped, not emitted
  F4  missing attribution -> blank cells, never a crash
  F5  read-only: every file byte-identical afterwards, only --out created
  F6  no network client in the tool

Run:  python3 tests/test_backfill_sheet_audit_columns.py
"""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOOL = os.path.join(ROOT, "tools", "backfill_sheet_audit_columns.py")
SCHEMA = json.load(open(os.path.join(ROOT, "sheets", "schema.json"), encoding="utf-8"))

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


tmp = tempfile.mkdtemp(prefix="houseoffer-backfill-test-")
reports = os.path.join(tmp, "houseoffer_reports")
os.makedirs(reports)


def rec(created, status="ready", report=True, attribution=None):
    r = {"status": status, "created_at": created, "email": "x@example.com"}
    if report:
        r["report"] = {"weighted_midpoint": 600_000, "valuation_asking_divergence_pct": -7.7,
                       "confidence_score": "medium", "comparables_count": 12, "comps_match_tier": "postcode",
                       "country": "England", "postcode": "RG1 5BX"}
    if attribution is not None:
        r["confirm_inputs"] = {"attribution": attribution}
    return r


FIX = {
    "aaaa000000000001": rec("2026-09-01T10:00:00Z", attribution={"referrer": "https://www.google.com/", "utm_source": "google",
                                                                  "utm_medium": "cpc", "utm_campaign": "sept", "utm_term": "", "utm_content": ""}),
    "aaaa000000000002": rec("2026-08-01T10:00:00Z"),                      # before --since
    "aaaa000000000003": rec("2026-09-02T10:00:00Z", status="failed", report=False),
    "aaaa000000000004": rec("2026-09-03T10:00:00Z"),                      # no attribution stored
}
for uuid, r in FIX.items():
    with open(os.path.join(reports, f"{uuid}.json"), "w") as fh:
        json.dump(r, fh)


def digest(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


before = {f: digest(os.path.join(reports, f)) for f in os.listdir(reports)}
listing = sorted(os.listdir(tmp))
out = os.path.join(tmp, "backfill.csv")
proc = subprocess.run([sys.executable, TOOL, "--reports-dir", reports, "--since", "2026-08-24", "--out", out], capture_output=True, text=True)
print("  tool stderr:", proc.stderr.strip())
check("exits 0", proc.returncode == 0, proc.stderr[-200:])
rows = {r["uuid"]: r for r in csv.DictReader(open(out, newline="", encoding="utf-8"))}

print("[F1] the twelve columns, schema order")
expected_cols = ["uuid", "timestamp", "our_valuation", "gap_vs_asking_pct", "confidence", "comps_count", "comps_tier",
                 "country", "referrer", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]
hdr = open(out, encoding="utf-8").readline().strip().split(",")
check("header", hdr == expected_cols, str(hdr))
sub_keys = SCHEMA["types"]["submission"]["keys"]
check("every column is a submission payload key", all(c in sub_keys for c in expected_cols if c != "uuid" and c != "timestamp"))
r = rows.get("aaaa000000000001", {})
check("values derived like the payload", r.get("our_valuation") == "600000" and r.get("gap_vs_asking_pct") == "-7.7"
      and r.get("confidence") == "MEDIUM" and r.get("comps_count") == "12" and r.get("comps_tier") == "postcode"
      and r.get("country") == "England", str(r))
check("attribution carried", r.get("utm_source") == "google" and r.get("referrer") == "https://www.google.com/" and r.get("utm_term") == "")

print("[F2] --since")
check("report before --since skipped", "aaaa000000000002" not in rows, str(sorted(rows)))
proc2 = subprocess.run([sys.executable, TOOL, "--reports-dir", reports, "--since", ""], capture_output=True, text=True)
rows_all = {r["uuid"] for r in csv.DictReader(proc2.stdout.splitlines())}
check("'' means all ready reports", rows_all == {"aaaa000000000001", "aaaa000000000002", "aaaa000000000004"}, str(sorted(rows_all)))

print("[F3] not-ready / no-report records skipped")
check("failed record absent", "aaaa000000000003" not in rows and "aaaa000000000003" not in rows_all)

print("[F4] missing attribution -> blanks")
r4 = rows.get("aaaa000000000004", {})
check("present with blank attribution", r4 and all(r4.get(k) == "" for k in ("referrer", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")), str(r4))

print("[F5] read-only")
after = {f: digest(os.path.join(reports, f)) for f in os.listdir(reports)}
check("every report byte-identical", after == before)
check("only --out created", sorted(set(os.listdir(tmp)) - set(listing)) == ["backfill.csv"])
check("stderr says DRY RUN", "DRY RUN" in proc.stderr)

print("[F6] no network client")
src = open(TOOL, encoding="utf-8").read()
check("no requests/urllib/http/socket imports", not any(f"import {m}" in src or f"from {m}" in src for m in ("requests", "urllib", "http", "socket")))

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

"""tools/restate_anchor_bias.py — dry-run restatement, no network, no credits.

Builds a temp reports directory shaped exactly like DATA_DIR/houseoffer_reports
(<uuid>.json records as _run_free_build stores them) plus a Submissions-tab CSV,
runs the tool as a subprocess and checks:

  R1  a row computed against local_avg_sold is restated against the stored
      weighted midpoint (32.0 -> 10.0 with local £500k / midpoint £600k)
  R2  no weighted midpoint stored -> blank, with the reason
  R3  estimate above 3x asking -> blank (PR #39 rule), with the reason
  R4  unparseable estimate -> blank, with the reason
  R5  a record with anchor_bias None is not a populated row: excluded
  R6  a sheet row whose UUID has no report on disk (pre-14-July) -> blank,
      reason names it, source sheet-only
  R7  a sheet/disk old-value mismatch is called out in the reason
  R8  the tool is read-only: every report file is byte-identical afterwards
      and the only file it creates is --out
  R9  disk-only mode (no --sheet-csv) lists every populated record
  R10 the tool contains no network client at all (no requests/urllib/http)

Run:  python3 tests/test_restate_anchor_bias.py
"""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOOL = os.path.join(ROOT, "tools", "restate_anchor_bias.py")

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


def record(buyer_estimate, anchor_bias, midpoint=600_000, asking=650_000, local=500_000, created="2026-08-01T10:00:00Z"):
    return {"status": "ready", "email": "buyer@example.com", "created_at": created,
            "buyer_estimate": buyer_estimate, "anchor_bias": anchor_bias,
            "report": {"postcode": "RG1 5BX", "asking_price": asking, "weighted_midpoint": midpoint,
                       "local_avg_sold": local, "verdict": "overpriced"}}


tmp = tempfile.mkdtemp(prefix="houseoffer-restate-test-")
reports = os.path.join(tmp, "houseoffer_reports")
os.makedirs(reports)
FIX = {
    "aaaa000000000001": record("660,000", 32.0),                       # R1: old vs local avg -> 10.0
    "aaaa000000000002": record("660000", 32.0, midpoint=None),          # R2: no midpoint
    "aaaa000000000003": record("5,850,000", 1070.0, asking=625_000),    # R3: > 3x asking
    "aaaa000000000004": record("five hundred k", 4.0),                  # R4: unparseable
    "aaaa000000000005": record("", None),                               # R5: not populated
    "aaaa000000000006": record("540000", 8.0, created="2026-09-05T09:00:00Z"),  # R7: sheet says 8.5
}
for uuid, rec in FIX.items():
    with open(os.path.join(reports, f"{uuid}.json"), "w") as fh:
        json.dump(rec, fh)

sheet_path = os.path.join(tmp, "submissions.csv")
with open(sheet_path, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Timestamp", "UUID", "Email", "Postcode", "Property Type", "Asking Price", "Verdict",
                "Buyer Estimate", "Anchor Bias %", "Property URL", "Report URL"])
    w.writerow(["2026-06-12T10:02:00Z", "bbbb000000000001", "x@example.com", "BA2 7HY", "flat", 300000, "fair",
                320000, 6.7, "https://www.rightmove.co.uk/properties/1", "https://houseoffer-backend.onrender.com/r/bbbb000000000001"])  # R6: no disk
    w.writerow(["2026-08-01T10:00:00Z", "aaaa000000000001", "x@example.com", "RG1 5BX", "terraced", 650000, "overpriced",
                660000, 32.0, "", ""])
    w.writerow(["2026-09-05T09:00:00Z", "aaaa000000000006", "x@example.com", "RG1 5BX", "terraced", 650000, "overpriced",
                540000, 8.5, "", ""])  # R7: mismatch with disk (8.0)
    w.writerow(["2026-09-06T09:00:00Z", "aaaa000000000005", "x@example.com", "RG1 5BX", "terraced", 650000, "overpriced",
                "", "", "", ""])  # blank anchor bias in the sheet: not a populated row


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


before = {f: digest(os.path.join(reports, f)) for f in os.listdir(reports)}
before_listing = sorted(os.listdir(tmp))

out_path = os.path.join(tmp, "restate.csv")
proc = subprocess.run([sys.executable, TOOL, "--reports-dir", reports, "--sheet-csv", sheet_path, "--out", out_path],
                      capture_output=True, text=True)
print("  tool stderr:", proc.stderr.strip())
check("tool exits 0", proc.returncode == 0, proc.stderr[-300:])
rows = {}
with open(out_path, newline="", encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        rows[r["uuid"]] = r

print("[R1] restated against the stored weighted midpoint")
r = rows.get("aaaa000000000001", {})
check("old 32.0 (vs local avg) -> new 10.0 (vs midpoint)", r.get("old_anchor_bias") == "32.0" and r.get("new_anchor_bias") == "10.0", str(r))
check("no spurious sheet/disk mismatch when both say 32.0", r.get("reason") == "", str(r.get("reason")))
check("delta -22.0", r.get("delta") == "-22.0", str(r.get("delta")))
check("source sheet+disk", r.get("source") == "sheet+disk", str(r.get("source")))

print("[R2] no weighted midpoint -> blank")
r = rows.get("aaaa000000000002", {})
check("blank new value with reason", r.get("new_anchor_bias") == "" and "no weighted midpoint" in r.get("reason", ""), str(r.get("reason")))

print("[R3] estimate above 3x asking -> blank (PR #39)")
r = rows.get("aaaa000000000003", {})
check("blank new value with reason", r.get("new_anchor_bias") == "" and "3x asking" in r.get("reason", ""), str(r.get("reason")))

print("[R4] unparseable estimate -> blank")
r = rows.get("aaaa000000000004", {})
check("blank new value with reason", r.get("new_anchor_bias") == "" and "unparseable" in r.get("reason", ""), str(r.get("reason")))

print("[R5] anchor_bias None is not a populated row")
check("excluded from the CSV", "aaaa000000000005" not in rows, str(list(rows)))

print("[R6] sheet row with no stored report (pre-14-July)")
r = rows.get("bbbb000000000001", {})
check("listed with blank new value", r.get("new_anchor_bias") == "" and r.get("old_anchor_bias") == "6.7", str(r))
check("reason names the missing report", "no stored report" in r.get("reason", ""), str(r.get("reason")))
check("source sheet-only, sheet timestamp/postcode carried", r.get("source") == "sheet-only" and r.get("postcode") == "BA2 7HY" and r.get("timestamp", "").startswith("2026-06-12"), str(r))

print("[R7] sheet/disk old-value mismatch is called out")
r = rows.get("aaaa000000000006", {})
check("new -10.0 computed", r.get("new_anchor_bias") == "-10.0", str(r.get("new_anchor_bias")))
check("mismatch noted (sheet 8.5 vs disk 8.0)", "sheet shows 8.5" in r.get("reason", "") and "disk shows 8.0" in r.get("reason", ""), str(r.get("reason")))

print("[R8] read-only")
after = {f: digest(os.path.join(reports, f)) for f in os.listdir(reports)}
check("every report file byte-identical", after == before)
new_files = sorted(set(os.listdir(tmp)) - set(before_listing))
check("only --out was created", new_files == ["restate.csv"], str(new_files))
check("stderr says DRY RUN", "DRY RUN" in proc.stderr)

print("[R9] disk-only mode")
proc2 = subprocess.run([sys.executable, TOOL, "--reports-dir", reports], capture_output=True, text=True)
check("exits 0", proc2.returncode == 0, proc2.stderr[-200:])
rows2 = {r["uuid"]: r for r in csv.DictReader(proc2.stdout.splitlines())}
check("lists every populated record (5) and not the None one", set(rows2) == {"aaaa000000000001", "aaaa000000000002", "aaaa000000000003", "aaaa000000000004", "aaaa000000000006"}, str(sorted(rows2)))
check("source disk", all(r.get("source") == "disk" for r in rows2.values()))

print("[R10] no network client in the tool")
src = open(TOOL, encoding="utf-8").read()
check("no requests/urllib/http/socket imports", not any(f"import {m}" in src or f"from {m}" in src for m in ("requests", "urllib", "http", "socket")))

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)

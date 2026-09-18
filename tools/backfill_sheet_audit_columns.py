#!/usr/bin/env python3
"""Backfill rows for the Submissions-tab columns that never landed — dry run to CSV.

READ-ONLY. Never writes to the stored reports, never touches the Google Sheet,
never calls the network. Output is a CSV keyed by UUID for the Apps Script's
upsert (18 Sep plan, step 3) or for a manual paste.

Why: since 24 August every submission has posted six audit fields
(our_valuation, gap_vs_asking_pct, confidence, comps_count, comps_tier, country)
and six attribution fields (referrer, utm_*), and the script dropped them all
because it writes fixed columns. The data is still on the Render disk in the
stored report JSON, so this emits one row per report with exactly those twelve
columns, matching sheets/schema.json.

Run on the Render shell (Dashboard -> houseoffer-backend -> Shell):

    python tools/backfill_sheet_audit_columns.py --since 2026-08-24 --out /tmp/backfill.csv
    cat /tmp/backfill.csv
"""
import argparse
import csv
import glob
import io
import json
import os
import sys

AUDIT = ["our_valuation", "gap_vs_asking_pct", "confidence", "comps_count", "comps_tier", "country"]
ATTRIBUTION = ["referrer", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]
COLUMNS = ["uuid", "timestamp"] + AUDIT + ATTRIBUTION


def default_reports_dir():
    return os.path.join(os.environ.get("DATA_DIR", "/tmp"), "houseoffer_reports")


def row_for(uuid, rec):
    """The twelve columns, derived exactly as _run_free_build posts them."""
    report = rec.get("report") or {}
    attribution = ((rec.get("confirm_inputs") or {}).get("attribution")) or {}
    return {
        "uuid": uuid,
        "timestamp": rec.get("created_at") or "",
        "our_valuation": report.get("weighted_midpoint"),
        "gap_vs_asking_pct": report.get("valuation_asking_divergence_pct"),
        "confidence": (report.get("confidence_score") or "").upper(),
        "comps_count": report.get("comparables_count"),
        "comps_tier": report.get("comps_match_tier", ""),
        "country": report.get("country") or "",
        **{k: attribution.get(k, "") for k in ATTRIBUTION},
    }


def load(reports_dir, since):
    rows, skipped = [], 0
    for path in sorted(glob.glob(os.path.join(reports_dir, "*.json"))):
        uuid = os.path.basename(path)[:-5]
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except Exception:
            skipped += 1
            continue
        if not isinstance(rec, dict) or rec.get("status") != "ready" or not rec.get("report"):
            skipped += 1
            continue
        created = str(rec.get("created_at") or "")
        if since and created[:10] < since:
            continue
        rows.append(row_for(uuid, rec))
    rows.sort(key=lambda r: (r["timestamp"], r["uuid"]))
    return rows, skipped


def fmt(v):
    return "" if v is None else str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports-dir", default=default_reports_dir())
    ap.add_argument("--since", default="2026-08-24", help="ISO date; reports created before it are skipped ('' for all)")
    ap.add_argument("--out", help="write the CSV here instead of stdout")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.reports_dir):
        print(f"reports dir not found: {args.reports_dir}", file=sys.stderr)
        return 2
    rows, skipped = load(args.reports_dir, args.since)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({k: fmt(r.get(k)) for k in COLUMNS})
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
    else:
        sys.stdout.write(buf.getvalue())
    print(f"DRY RUN — nothing written to disk or sheet. rows={len(rows)} skipped={skipped} since={args.since or 'all'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

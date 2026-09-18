#!/usr/bin/env python3
"""Dry-run restatement of anchor_bias against the stored weighted midpoint.

READ-ONLY. Never writes to the stored reports, never touches the Google Sheet,
never calls the network. The only output is a CSV (stdout, or --out) for review
before anything is applied anywhere.

Why: until 16 Sep 2026 anchor_bias was computed against local_avg_sold (the raw
comparable average). PR #32 moved it to the stored weighted midpoint (Our
Valuation), which is the baseline the vote rows already used. Rows written
before that carry the old figure.

Sources (CEO 16 Sep):
  * The stored report JSON on the Render persistent disk is the ONLY place the
    stored weighted midpoint lives: DATA_DIR/houseoffer_reports/<uuid>.json
    (the same files app.py's load_report reads).
  * Rows from before 14 July 2026 have no stored report (they predate the
    persistent disk). They are listed with a BLANK new value, not chased.

Rules mirror production (_run_free_build in app.py, main as of 16 Sep):
  * estimate parsed as int(str(x).replace(",", "").replace("£", "").replace(" ", ""))
  * an estimate above 3x the stored asking price is implausible -> blank (PR #39)
  * no stored weighted midpoint -> blank
  * otherwise new = round((estimate - midpoint) / midpoint * 100, 1)

Run on the Render shell (Dashboard -> houseoffer-backend -> Shell), after the
commit carrying this file has deployed:

    python tools/restate_anchor_bias.py --out /tmp/restate_anchor_bias.csv
    cat /tmp/restate_anchor_bias.csv

Optional: join the Submissions tab so rows with no stored report are listed
too (Google Sheet -> File -> Download -> CSV of the Submissions tab, then
paste it into the shell with `cat > /tmp/submissions.csv`, Ctrl-D):

    python tools/restate_anchor_bias.py --sheet-csv /tmp/submissions.csv --out /tmp/restate.csv
"""
import argparse
import csv
import glob
import io
import json
import os
import sys

SHEET_UUID = "UUID"
SHEET_TS = "Timestamp"
SHEET_POSTCODE = "Postcode"
SHEET_OLD = "Anchor Bias %"

REASON_NO_REPORT = "no stored report on disk (pre-14-July, before the persistent disk): blank"
REASON_NO_ESTIMATE = "buyer estimate missing or unparseable: blank"
REASON_IMPLAUSIBLE = "estimate above 3x asking (implausible, PR #39): blank"
REASON_NO_MIDPOINT = "no weighted midpoint stored: blank"

COLUMNS = ["uuid", "timestamp", "postcode", "old_anchor_bias", "new_anchor_bias",
           "delta", "reason", "buyer_estimate", "weighted_midpoint", "asking_price",
           "source"]


def default_reports_dir():
    # Same derivation as app.py: REPORTS_DIR = DATA_DIR/houseoffer_reports
    return os.path.join(os.environ.get("DATA_DIR", "/tmp"), "houseoffer_reports")


def parse_estimate(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").replace("£", "").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def restate(record):
    """(new_value_or_None, reason) for one stored report record."""
    report = record.get("report") or {}
    est = parse_estimate(record.get("buyer_estimate"))
    midpoint = report.get("weighted_midpoint")
    asking = report.get("asking_price")
    if est is None:
        return None, REASON_NO_ESTIMATE
    if asking and est > 3 * asking:
        return None, REASON_IMPLAUSIBLE
    if not midpoint:
        return None, REASON_NO_MIDPOINT
    return round(((est - midpoint) / midpoint) * 100, 1), ""


def load_disk(reports_dir):
    """uuid -> stored record, for every readable <uuid>.json in the directory."""
    out = {}
    for path in sorted(glob.glob(os.path.join(reports_dir, "*.json"))):
        uuid = os.path.basename(path)[:-5]
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except Exception as exc:  # unreadable file: report it, never touch it
            out[uuid] = {"_error": f"{type(exc).__name__}: {exc}"}
            continue
        if isinstance(rec, dict):
            out[uuid] = rec
    return out


def load_sheet(path):
    """Submissions-tab rows with a populated Anchor Bias %, keyed by UUID."""
    rows = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            uuid = (row.get(SHEET_UUID) or "").strip()
            old = (row.get(SHEET_OLD) or "").strip()
            if uuid and old != "":
                rows[uuid] = row
    return rows


def fmt(value):
    """CSV cell text. Floats keep their decimal (10.0 stays 10.0, as production stores it)."""
    if value is None:
        return ""
    return str(value)


def same_number(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def build_rows(disk, sheet):
    rows = []
    seen = set()
    for uuid, srow in (sheet or {}).items():
        seen.add(uuid)
        rec = disk.get(uuid)
        old_sheet = srow.get(SHEET_OLD)
        if rec is None:
            rows.append({"uuid": uuid, "timestamp": srow.get(SHEET_TS, ""),
                         "postcode": srow.get(SHEET_POSTCODE, ""), "old_anchor_bias": old_sheet,
                         "new_anchor_bias": "", "delta": "", "reason": REASON_NO_REPORT,
                         "buyer_estimate": "", "weighted_midpoint": "", "asking_price": "",
                         "source": "sheet-only"})
            continue
        rows.append(row_from_record(uuid, rec, old_sheet, "sheet+disk"))
    for uuid, rec in disk.items():
        if uuid in seen:
            continue
        if "_error" in rec:
            rows.append({"uuid": uuid, "timestamp": "", "postcode": "", "old_anchor_bias": "",
                         "new_anchor_bias": "", "delta": "", "reason": f"unreadable: {rec['_error']}",
                         "buyer_estimate": "", "weighted_midpoint": "", "asking_price": "",
                         "source": "disk"})
            continue
        if rec.get("anchor_bias") is None:
            continue  # not a populated row: nothing to restate
        rows.append(row_from_record(uuid, rec, None, "disk" if sheet is None else "disk-only (not in sheet)"))
    rows.sort(key=lambda r: (str(r.get("timestamp") or ""), r["uuid"]))
    return rows


def row_from_record(uuid, rec, old_sheet, source):
    report = rec.get("report") or {}
    old_disk = rec.get("anchor_bias")
    old = old_disk if old_disk is not None else old_sheet
    new, reason = restate(rec)
    delta = ""
    if new is not None and old not in (None, ""):
        try:
            delta = round(new - float(old), 1)
        except (TypeError, ValueError):
            delta = ""
    if (old_sheet not in (None, "") and old_disk is not None
            and not same_number(old_sheet, old_disk)):
        reason = (reason + "; " if reason else "") + f"sheet shows {old_sheet}, disk shows {old_disk}"
    return {"uuid": uuid, "timestamp": rec.get("created_at") or "", "postcode": report.get("postcode", ""),
            "old_anchor_bias": old, "new_anchor_bias": new, "delta": delta, "reason": reason,
            "buyer_estimate": rec.get("buyer_estimate", ""), "weighted_midpoint": report.get("weighted_midpoint", ""),
            "asking_price": report.get("asking_price", ""), "source": source}


def write_csv(rows, out):
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: fmt(r.get(k)) for k in COLUMNS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports-dir", default=default_reports_dir(),
                    help="directory of stored <uuid>.json reports (default: $DATA_DIR/houseoffer_reports)")
    ap.add_argument("--sheet-csv", help="optional CSV export of the Submissions tab to join by UUID")
    ap.add_argument("--out", help="write the CSV here instead of stdout")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.reports_dir):
        print(f"reports dir not found: {args.reports_dir}", file=sys.stderr)
        return 2
    disk = load_disk(args.reports_dir)
    sheet = load_sheet(args.sheet_csv) if args.sheet_csv else None
    rows = build_rows(disk, sheet)

    buf = io.StringIO()
    write_csv(rows, buf)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
    else:
        sys.stdout.write(buf.getvalue())

    restated = sum(1 for r in rows if r["new_anchor_bias"] not in (None, ""))
    blank = len(rows) - restated
    changed = sum(1 for r in rows if r["delta"] not in ("", None) and float(r["delta"]) != 0)
    print(f"DRY RUN — nothing written to disk or sheet. rows={len(rows)} restated={restated} "
          f"blank={blank} changed={changed} reports_on_disk={len(disk)}"
          + (f" sheet_rows_with_anchor_bias={len(sheet)}" if sheet is not None else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

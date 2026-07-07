"""Generate the marketing demo numbers for the fictional example property
(14 Maple Close, Bristol BS6) by executing the REAL production code.

Every trio and Offer Frontier figure shown on the homepage, /sample-report/
and the white paper worked example must come from this script's output —
never hand-authored. Hand-built samples have already shipped a number the
live guardrails cannot emit (Secure above walk-away); this script makes
that impossible.

It does not import app.py (module import needs Flask and live env). Instead
it extracts and exec()s the exact source slices of the trio calculation
(inside build_report_data) and _offer_frontier + its constants, so the
arithmetic is the shipped code byte-for-byte. If the anchors move, the
script fails loudly rather than running stale logic.

Run:  python3 tools/demo_two_lenses.py
"""
import json
import textwrap
from pathlib import Path

SRC = (Path(__file__).parent.parent / "app.py").read_text()


def slice_between(start_anchor, end_anchor, include_end=True):
    a = SRC.index(start_anchor)
    b = SRC.index(end_anchor, a)
    if include_end:
        b += len(end_anchor)
    return SRC[a:b]


# ── real production code, extracted verbatim ─────────────────────────────
fmt_src = slice_between("def _fmt(value):", "return f\"£{value:,}\" if value else None")
frontier_src = slice_between("_FRONTIER_RISK = {", "\n@app.route(\"/r/<report_id>/buyer-profile\"", include_end=False)
trio_src = textwrap.dedent(slice_between(
    "    open_offer = target_price = walk_away = None",
    "        recommended_offer = open_offer",
))

# ── the fictional demo property ──────────────────────────────────────────
# Ten methods; context-only rows carry weight 0 and are excluded, exactly
# as build_report_data's available_methods filter does. Values chosen so
# the weighted range lands on the long-established example (352k–368k).
METHODS = [
    {"name": "Comparable sales (adjusted)",     "low": 352000, "high": 374000, "weight": 2},
    {"name": "Previous sold price (adjusted)",  "low": 355000, "high": 369000, "weight": 2},
    {"name": "Comparable sales (raw)",          "low": 351000, "high": 368000, "weight": 1},
    {"name": "Price per square metre",          "low": 355000, "high": 366000, "weight": 1},
    {"name": "Area price trend",                "low": 347000, "high": 371000, "weight": 1},
    {"name": "Bedroom-matched local price",     "low": 353000, "high": 362000, "weight": 1},
    {"name": "Automated valuation",             "low": 350000, "high": 369000, "weight": 1},
    {"name": "Est. lender range (modelled)",    "low": 350000, "high": 358000, "weight": 1},
    # context only — excluded from the weighted range, like the live filter
    {"name": "Rental yield implied value",      "low": 345000, "high": 353000, "weight": 0},
    {"name": "Asking-to-sold discount",         "low": 343000, "high": 370000, "weight": 0},
]

DEMO = {
    "asking_price": 385000,
    "verdict": "overpriced",
    "days_on_market": 47,
    "local_avg_dom": 32,
    "local_sold_discount_pct": 3.8,   # BS6 asking-to-sold average
    "price_reduced": True,
    "reduction_pct": 3.7,             # one cut, under the 5% bonus threshold
}

# ── run the trio calc (real source) ──────────────────────────────────────
env = {
    "available_methods": [m for m in METHODS if m["weight"] > 0],
    "asking_price": DEMO["asking_price"],
    "verdict": DEMO["verdict"],
}
exec(trio_src, env)

# ── run the frontier (real source) ───────────────────────────────────────
fenv = {}
exec(fmt_src, fenv)
exec(frontier_src, fenv)
report = dict(DEMO,
              walk_away=env["walk_away"],
              weighted_low=env["weighted_low"])
frontier = fenv["_offer_frontier"](report, profile=None)

out = {
    "input_methods": METHODS,
    "weighted_low": env["weighted_low"],
    "weighted_high": env["weighted_high"],
    "weighted_midpoint": env["weighted_midpoint"],
    "trio": {
        "open_offer": env["open_offer"],
        "target_price": env["target_price"],
        "walk_away": env["walk_away"],
    },
    "frontier": frontier,
}
print(json.dumps(out, indent=2))

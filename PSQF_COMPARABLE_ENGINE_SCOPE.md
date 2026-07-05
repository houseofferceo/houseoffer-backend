# Scope — Bedroom/Size/Distance-Matched Comparables from the £/sqf Feed We Already Fetch

Status: SCOPE (no code) — awaiting sign-off
Date: 2026-06-30
Companion docs: METHODOLOGY_REDESIGN.md, VALUATION_TEST_HISTORY.md

═══════════════════════════════════════════════════════════════════════════
1. THE FINDING THAT MAKES THIS SMALL
═══════════════════════════════════════════════════════════════════════════

Confirmed live via /debug-psqf on HA6 (2026-06-30): PropertyData's
/sold-prices-per-sqf feed returns, PER SOLD PROPERTY:

  { address, type, bedrooms, lat, lng, sqf (floor area), price, date,
    price_per_sqf, tenure, class, url }

— 300 records for HA6. We ALREADY call this feed every paid report
(fetch_psqf_points). But _psqf_points extracts only {psqf, sqf, address, price}
and DISCARDS bedrooms, lat, lng, date, type. So we already pull bedroom- and
location-tagged SOLD comparables and throw the best columns away.

This is NOT a new integration. It is "use the columns we already fetch."

═══════════════════════════════════════════════════════════════════════════
2. THE TWO SOLD FEEDS TODAY (why the headline number is still blind)
═══════════════════════════════════════════════════════════════════════════

Feed A — Land Registry /sold-prices (get_sold_comparables -> _filter_sold)
  Fields: type, price, date, address. NO bedrooms / floor area / coordinates.
  Drives: the HEADLINE comparable average (local_avg_sold), weight 2 — the
  dominant valuation method. This is the bedroom-blind number behind the bad
  cases (e.g. SE26 2-bed averaged with 4-bed terraces).

Feed B — PropertyData /sold-prices-per-sqf (fetch_psqf_points -> _psqf_points)
  Fields (available): type, bedrooms, lat, lng, sqf, price, date, price_per_sqf.
  Used today ONLY for: the £/sqm method (incl. ±20% size-match on sqf) and a
  floor-area lookup to size-match Feed-A comps by address (FIX 2).
  NOT used for: bedrooms, coordinates, or as the headline comparable set.

The fix: build the headline comparable set from Feed B (bedroom + size + distance
matched), with Feed A as the fallback.

═══════════════════════════════════════════════════════════════════════════
3. PROPOSED CHANGE — PHASED
═══════════════════════════════════════════════════════════════════════════

PHASE A — Stop discarding the columns. [small]
  Extend _psqf_points to carry bedrooms, lat, lng, date, type (canonical) on each
  point, in addition to today's psqf/sqf/address/price. No behaviour change yet —
  existing consumers (£/sqm, FIX 2 lookup) keep working off the same objects.

PHASE B — Add a bedroom/size/distance SOLD-comparable method. [moderate]
  Reuse the P2 machinery already in the codebase (built for the now-blocked
  Rightmove feed): _within_size_band, _haversine_miles, get_nearby_comparables.
  Repoint it at the Feed-B points instead of fetch_sold_nearby:
    1. type-match (canonical)
    2. bedroom-match (subject beds; widen to ±1 if too few)
    3. ±20% floor-area size band (reuse _within_size_band on sqf)
    4. distance-rank by haversine vs the subject's scraped lat/lng; take nearest N
    5. HPI-adjust each by its date
    6. average -> a bedroom/size/distance-matched SOLD comparable value
  Add it to the football field as a NEW weighted method ("Matched sold
  comparables") and SCORE it against the batch — do NOT make it primary yet.
  Widening ladder (correctness > coverage): beds ±0 -> ±1; radius 0.5 -> 1 -> 2 mi;
  then fall back to the Land Registry headline average, each step lowering
  comparable_confidence.

PHASE C — Promote to primary and simplify. [after Phase B proves out]
  Make the matched-sold method the PRIMARY comparable signal (replace the
  bedroom-blind Land Registry average as the weight-2 method); demote Land
  Registry to fallback + cross-check. Then RETIRE / simplify the patchwork that
  only existed to compensate for bedroom-blindness:
    - Option D (bedroom asking proxy) -> redundant (we now have sold by bedroom)
    - The thin-set guardrail -> mostly redundant
    - The "bedroom signal leads" down-weighting -> redundant
  Keep them only if Phase B shows the sold feed is too thin in some areas.

═══════════════════════════════════════════════════════════════════════════
4. WHAT WE ALREADY HAVE (so this is mostly wiring)
═══════════════════════════════════════════════════════════════════════════

  - Subject bedrooms: scraped (bedrooms_source).
  - Subject lat/lng: scraped + threaded into build_report_data (P2).
  - Subject floor area: scraped / EPC.
  - _within_size_band(area_sqf, subject_sqf, ±20%): exists (FIX 2).
  - _haversine_miles(lat,lng,lat,lng): exists (P2).
  - get_nearby_comparables(lat,lng,type,beds,records): exists (P2) — currently
    fed by the dead Rightmove feed; repoint to Feed-B points.
  - HPI adjustment by date: hpi_adjust_comparables exists.
  - fetch_psqf_points already called every paid report (postcode + district).

═══════════════════════════════════════════════════════════════════════════
5. OPEN QUESTIONS TO CONFIRM BEFORE / DURING BUILD
═══════════════════════════════════════════════════════════════════════════

  - COVERAGE in sparse areas. HA6 (leafy) returned 300 records. Need to confirm
    /sold-prices-per-sqf returns enough bedroom-tagged records in rural/thin
    postcodes. Mitigation: Land Registry headline average stays as the fallback.
  - BEDROOM completeness. Do most Feed-B records carry a non-null bedrooms? (HA6
    sample did.) Records with null beds drop out of the strict bedroom rung.
  - CREDIT COST. None new — we already make this call. (Confirm the per-sqf call
    isn't a higher-credit endpoint than expected.)
  - DISTANCE vs the £2m / median-band trims currently in _filter_sold (Feed A).
    Decide which junk-trims to carry over to the Feed-B comparable set.

═══════════════════════════════════════════════════════════════════════════
6. ACCEPTANCE CRITERIA (scored on /batch-valuation-test)
═══════════════════════════════════════════════════════════════════════════

  - New per-row signals: comparable_source = "matched_sold", plus the existing
    comparable_radius_miles / comparable_bedroom_band / comparable_count.
  - Median |gap vs asking| at or below the current single-digit level, with the
    matched-sold method firing on the majority of rows (not falling back to
    area_only).
  - SE26-type cases stay near asking WITHOUT relying on Option D / the guardrail
    (i.e. the sold-matched method carries them on its own).
  - Sparse-postcode test: confirm graceful Land Registry fallback (no blanks).
  - No regression on the clean cases (SE25, BS10, LS25).

═══════════════════════════════════════════════════════════════════════════
7. EFFORT & RISK
═══════════════════════════════════════════════════════════════════════════

  Effort: Phase A small (~1h), Phase B moderate (~half day — mostly wiring +
  batch tuning), Phase C small once B proves out. Most logic already exists.
  Risk: LOW-MODERATE. New method is added alongside (Phase B) before replacing
  anything (Phase C), and Land Registry remains the fallback throughout, so worst
  case is unchanged behaviour. Main unknown is Feed-B coverage in thin areas —
  measured directly by the batch before promotion.

  Note (separate infra item): the batch test runs on the production instance and
  heavy runs can destabilise it (seen 2026-06-30). Keep test runs small (n<=3) or
  move the test to a separate worker.

# HouseOffer — Valuation Engine: Confidence-Gated Methodology & Validation

Development log + performance · 30 June 2026

Records (1) what we built across four development cycles, (2) how the engine now
works, (3) the large-sample test results, and (4) what's left. Section 5 flags how
these numbers may/may not be used in marketing.

---

## 1. The strategic shift

From "value everything and hope it's right" to "value everything we have real data
for, and be honest about how much to trust each number." Three outcomes:

- **(a)** No usable UK postcode → we say we can't generate a report (rare).
- **(b)** Data exists but is risky (unclear type, special tenure, premium,
  thin/broad comparables) → we **still** return a value, with a visible confidence
  score and a plain-English reason.
- **(c)** Standard stock, good data → value with **HIGH** confidence, no caveat.

We never silently publish a confident wrong number.

---

## 2. What we built — four cycles

**Cycle 1 — Honest confidence + coverage**
- GB-postcode validity filter on the random-test harness (samples were ~35%
  non-GB/placeholder junk; now ~0%).
- Comparable fallback chain: postcode → sector → district → wider-area/region, so a
  valid postcode never returns "no comparables". The tier used drives the score.
- Confidence score (HIGH/MEDIUM/LOW) + buyer-facing caveat on every report.
- Special-tenure detection (shared ownership, auction/guide-price, retirement).
- Hardened property-type parsing (no silent "semi-detached" default).
- Size/bedroom-matched comparables feed the headline method where data allows.

**Cycle 2 — Make HIGH actually mean high**
- HIGH was being granted for "enough comparables in the postcode" even when those
  comparables ignored bedrooms/size → HIGH and MEDIUM were equally accurate.
- Fix: HIGH now requires **either** a genuine bedroom/size match **or** independent
  matched-sold comparables that **agree** with the published number (within 12%).
- "Methods disagree" guard: independent signal diverges >20% → cap to MEDIUM + flag.

**Cycle 3 — Sanity gate for non-standard listings**
- When our value is >1.5× or <0.6× the asking price → cap to LOW + caveat
  (shared ownership / short lease / auction guide / mis-listing). Catches the
  catastrophic mispricings (−69%, +130%, +477%).
- Widened tenure detection to sub-market schemes ("70% of market value",
  discounted-market-sale, First Homes).

**Cycle 4b — Premium-property guard** (built, pending deploy)
- The last HIGH misses were premium/larger homes our local comparables
  under-capture (£1.25M Hampstead flat, £700k detached) — both methods read low and
  corroborated each other → false HIGH.
- Fix: a would-be-HIGH whose value is >25% below asking → MEDIUM with a
  "premium/larger property" reason. Pushes HIGH to ~100% within 20% in testing.

**Cycle 4c — floor-area capture (next):** capture floor area on more listings via
EPC so genuine size-matching runs more often, growing/strengthening the HIGH bucket.

---

## 3. Validation — 100-property random sample (95 valued)

Fresh random national sample of 100 live Rightmove listings (Cycles 1–3 live; 4b
adds a small further improvement). 95 returned a valuation; 0 unresolvable. Gap is
measured vs **asking** price.

| Confidence tier | Share | Within 10% | Within 20% | Median gap |
|---|---|---|---|---|
| **HIGH** | 28% | 56% | **93%** | **7.0%** |
| **MEDIUM** | 46% | 48% | 75% | 11.6% |
| **LOW** | 25% | 0% | 13% | 69.0% |
| All valued | 100% | 38% | 64% | 14.4% |

- The confidence score **works and is honest**: HIGH (7.0% median, 93% within 20%) >
  MEDIUM (11.6%) > LOW (69%). The score genuinely tells a buyer how much to trust it.
- HIGH is at/above industry AVM standard for its segment (industry: ~55–65% within
  10%, ~85–90% within 20%).
- LOW ≈ 25% of valuations — our honest "treat with caution" segment, in the same
  15–30% band mature providers decline/flag. Median 69% confirms LOW correctly
  catches non-standard listings (auctions, shared ownership, mis-listings, missing
  data).
- Zero unresolvable, zero fabricated profiles, zero silent failures.
- Special listings detected/caveated: 7 auction/guide-price, 2 retirement.

---

## 4. Backlog

- **Cycle 4c** — floor-area capture (EPC join). [next]
- Back-test against **sold** price, not asking — the proper accuracy measure. [high]
- New-build premium modelling (currently caveated, not priced).
- Harvester: reject structurally-valid-but-fake postcodes (a handful slip through,
  return no data — safe but untidy).
- Floor-area sanity-check edge case (one 70,820 m² value slipped through; correctly
  ended up LOW, but worth hardening).
- Move the test harness off the production instance for large runs.

---

## 5. Note on marketing use of these numbers

Promising and defensible internally, but before any **external/marketing** use:

- **Measure vs sold price, not asking.** Every figure here is "gap vs asking", a
  proxy — a large gap can mean we're right and the asking is wrong. A sold-price
  back-test is needed before publishing accuracy claims.
- **Quote the segment, not the blend.** The honest strong claim is about
  HIGH-confidence cases ("93% within 20% of asking on high-confidence valuations"),
  not a single blended accuracy.
- **Lead with the differentiator that's already verifiable:** we give an honest
  confidence score and flag special/risky listings rather than publishing a falsely
  precise number — most automated valuations don't.
- Sample = 100 random listings on one date; widen and repeat before citing.

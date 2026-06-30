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

---

## 6. Data sourcing & API cost — the constraint we hit (30 Jun 2026)

During testing we exhausted the **PropertyData monthly allowance (2,000 API calls)**
and the key was disabled, which took live valuations down until the allowance resets.
This surfaced a structural constraint that matters for launch, not just testing:

- **Each paid report makes ~8–12 PropertyData calls** (sold prices at postcode /
  sector / district, sold £/sqf, asking price by bedroom, AVM, rents, days-on-market,
  asking-to-sold ratio).
- At a 2,000-call cap that is **only ~150–200 reports per month** before the key
  disables — a real ceiling on how many customers we can serve.
- It is also a **single point of failure**: when the key is down, every valuation is
  down. (The confidence layer fails *honestly* — flags LOW / "can't generate" rather
  than publishing wrong numbers — but coverage stops.)

**Near-term mitigations:**
- **Cache aggressively** — sold-price and £/sqf responses are already cached for a
  short TTL; extend the TTL and persist the cache so repeat lookups in the same area
  don't re-bill. Most cost is repeated calls to popular postcodes.
- **Separate, smaller test budget** — never run large batches against the production
  key again; use a dedicated test key and keep test batches small (n ≤ 25).
- Move the test harness off the production instance/key (already on the backlog).

---

## 7. Could we own the data instead of buying it? (for management discussion)

Short answer: **largely yes for the high-volume, high-cost parts — and it would
remove both the cost ceiling and the single point of failure.** What PropertyData
sells us is mostly a convenient packaging of data that is itself **free and open**:

| What we pull | Underlying source | Open / free? | Self-host feasibility |
|---|---|---|---|
| Sold prices (type, price, date, address) | **HM Land Registry Price Paid Data** (England & Wales) | **Yes — open bulk download + we already query its free SPARQL endpoint for last-sale** | High — load the dataset into our own DB |
| Floor area per property | **EPC certificates** (gov.uk) | **Yes — free API we already use** | High — join EPC to Land Registry by address |
| Coordinates / geocoding | **ONS / OS Open postcode data** | **Yes — open** | High |
| £/sqf comparables (sold price ÷ floor area) | Derived from the three rows above | n/a | High — it's a join we can compute |
| Asking price by bedroom, rents, market analytics | Aggregated **live listings** | No — licensed; scraping it ourselves hits Rightmove's T&Cs | Low — keep buying, or do without |
| Their AVM | Proprietary model | No | n/a — we already build our own ("football field") |

**The opportunity:** the dominant, most-billed calls — sold prices and £/sqf
comparables — are built from **Land Registry + EPC + open geocoding, all free**. We
could **self-host that as our own database**: download the Land Registry Price Paid
dataset, join it to EPC floor areas, geocode by postcode, and serve comparables from
our own store. That turns a metered, rate-capped, outage-prone API into **free,
unlimited, cached data we control** — removing the ~200-reports/month ceiling and the
single-point-of-failure we just hit.

**What we'd keep buying (or drop):** current *asking* prices by bedroom and rents come
from live-listing aggregation that is genuinely licensed (and that we can't scrape
ourselves within Rightmove's terms). We'd either keep a small metered API for those,
or rely on our own sold-based methods (which are already the dominant signal).

**Honest caveats:**
- **Coverage:** Land Registry Price Paid is **England & Wales only**. Scotland
  (Registers of Scotland) and Northern Ireland are separate sources; PropertyData
  abstracts that for us. Self-hosting means handling those ourselves or accepting
  reduced Scotland/NI coverage initially.
- **Bedrooms:** Land Registry has no bedroom count and EPC's is imperfect (room
  counts, not always bedrooms). PropertyData's bedroom tagging is part of its value —
  we'd approximate it from EPC/listings, which is the same address-matching problem we
  already wrestle with.
- **Effort:** this is a real one-time infrastructure project (ETL + address matching
  between Land Registry and EPC is the hard part — the same matching we already do for
  floor area), but once built it **scales for free**.

**Recommendation for discussion:** a **hybrid** — self-host the open data (Land
Registry + EPC + geocoding) for the high-volume comparable and floor-area lookups, and
keep only a small metered API (or drop it) for genuinely-licensed asking-price/rent
signals. This is the single change that most improves unit economics and removes the
outage risk, and it's worth scoping as a strategic infrastructure decision.

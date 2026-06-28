# Valuation Methodology Redesign — Bedroom-Matched, Distance-Ranked Comparables

Status: SCOPE (no code yet)
Trigger: 2026-06-28 batch valuation test — systematic upward bias, broad-comp
dilution (EH10: 454 district comps), new-build misses (B1: −39%), and three
blank reports (0 comparables). Root cause traced to the comparable engine.

---

## 1. The core problem

Our heaviest valuation method — the **HPI-adjusted comparable average (weight 2)** —
selects comparables using only **property type + a £2m cap + a 0.5×–2.0× median
price band**. It does **not** constrain by:

- **bedrooms** (the single strongest price driver)
- **floor area / size**
- **distance** from the subject (averages the whole postcode, then broadens to the
  whole sector/district when thin — EH10 pulled in 454 comps)

That average also propagates into two more weighted methods — **Area price trend**
and **Lender valuation band** both derive from it — so one biased number
contaminates three of the weighted inputs.

## 2. The data we already collect but don't use for valuation

| Data | Where we get it | Used for valuation today? |
|---|---|---|
| Subject **bedrooms** | listing scrape (`scrape_property_url`) | Only AVM + rent inputs — NOT the comparable average |
| Subject **floor area** | listing scrape / EPC | £/sqm method only, when present |
| Subject **lat/long** | listing scrape (`latitude`/`longitude`) | Address resolution only |
| Subject **`is_new_build`** | listing scrape | **Nowhere** — new-builds valued on resale comps (B1 −39%) |
| **Sold comparables WITH bedrooms + type + coordinates** | `fetch_sold_nearby()` (Rightmove house-prices page) | **Address resolution only** — the valuation never sees it |

The decisive point: `fetch_sold_nearby()` already returns, per sold property,
`{address, latitude, longitude, property_type, bedrooms, price, date}`. We fetch
this, use it to pin the address, then **discard it and value off the
bedroom-less, location-less Land Registry feed.**

## 3. New comparable-selection algorithm

Build the comparable set from `fetch_sold_nearby()` (Rightmove), constrained by the
subject attributes we already scrape:

1. **Type match** — same normalised property type.
2. **Bedroom match** — same bedroom count as the subject (the new, decisive filter).
3. **Distance filter** — within an initial radius R₀ (e.g. 0.5 miles) of the
   subject's listing pin, computed by haversine on the coordinates we already have.
4. **Rank** the survivors by a blend of **proximity** (nearer = better) and
   **recency** (more recent sale = better).
5. **Take the nearest N** (target N ≥ `MIN_COMPARABLES`).
6. **Size tighten (optional)** — where floor areas are available (psqf/EPC join),
   further restrict to ±20% of the subject (reuses `_within_size_band`, FIX 2).
7. **HPI-adjust** each comparable to today (existing `hpi_adjust_comparables`).
8. **Average** (interquartile mean, as now) → the comparable value.

### Widening ladder (correctness > coverage)
If a stage leaves fewer than `MIN_COMPARABLES`, relax in this order, each step
lowering `comparable_confidence`:

1. Bedrooms ±0 → **±1 bedroom** (e.g. a 3-bed can borrow 2- and 4-beds).
2. Radius R₀ → **R₁** (e.g. 0.5 → 1.0 mile), then **R₂** (e.g. 2 miles).
3. Only then fall back to the current Land Registry postcode/sector/district feed,
   flagged `comparable_confidence = "area_only"` (today's behaviour, now the LAST
   resort instead of the default).

Never silently broaden past a sensible ceiling — return the best available set with
an explicit low-confidence flag rather than a diluted average.

## 4. New-build handling (closes the B1 / CW11 miss)

When `is_new_build` is true, resale comparables structurally under-value the
property (new-builds carry a 10–25% premium). Options, in order of preference:

- **A.** Restrict comparables to other **new-build** sold records where the feed
  flags them; else
- **B.** Apply a configurable new-build uplift to the comp-based methods; and
- **C.** Change the verdict copy from "overpriced" to "new-build premium vs
  resale comparables" so the buyer isn't misled.

## 5. How it slots into the football field

- **Method 1b (Comparable sales, HPI-adjusted, weight 2)** → now bedroom- and
  distance-matched. This is the main change.
- **Method 1a (unadjusted, weight 1)** → same new set, no HPI.
- **Method 4 (Area price trend)** and **Method 6 (Lender band)** → automatically
  improve, since they derive from the comparable average.
- Methods 2, 3, 5, 6b, 7 (last-sale, £/sqm, AVM, rent, asking-discount) → unchanged.
- **Cross-check:** keep the Land Registry average as a secondary signal; when the
  two diverge by more than a threshold, lower confidence and surface a flag for the
  QC layer.

## 6. New / changed signals in the report payload

- `comparable_method`: `"bedroom_distance"` | `"size_matched"` | `"area_only"`
- `comparable_radius_miles`, `comparable_bedroom_band` (0 or ±1)
- `comparable_count`, `comparable_count_size_matched` (existing)
- `comparable_source`: `"rightmove_nearby"` | `"land_registry"`
- `lr_vs_rightmove_divergence_pct` (cross-check)
- `new_build_adjustment_applied` (bool) + method

## 7. Reliability & risks

- `fetch_sold_nearby()` scrapes Rightmove (can break / rate-limit), vs the stable
  PropertyData/Land Registry API. Design = **Rightmove-rich feed primary, Land
  Registry fallback + corroboration**, never a hard dependency.
- Bedroom data in the Rightmove feed can be null on some records — those drop out
  of the bedroom-matched set (acceptable; they remain in the area_only fallback).
- Coordinates are listing/house-price pins, accurate to property level — good
  enough for sub-mile distance ranking.
- **Bathrooms:** neither the subject scrape nor the sold feed currently carries
  bathrooms, so bathroom-matching is out of scope for v1. The Rightmove listing
  PAGE_MODEL does expose subject bathrooms — a later enhancement could scrape them
  for the AVM input only (comps still won't have them).

## 8. Success criteria (measured by the batch valuation test)

Re-run `/batch-valuation-test` after each change and track:

- **Median |gap vs asking|** trends down from the 2026-06-28 baseline (8.7%).
- **Positive-gap rate** (our valuation above asking) drops — should be rare.
- **`area_only` share** drops (more reports bedroom/distance-matched).
- **Zero-valuation (blank) count** → 0.
- **New-build extreme-overpriced calls** disappear.

## 9. Phasing

1. **P1 — Zero-comp bug** (S10/SE26/BS9 returned 0 comps). Investigate first; may
   be a type-key/lookup defect independent of this redesign.
2. **P2 — Bedroom + distance comparable engine** from `fetch_sold_nearby` (this
   doc, sections 3 & 5), Land Registry as fallback.
3. **P3 — New-build handling** (section 4).
4. **P4 — Size tighten + LR/Rightmove cross-check + confidence surfacing**.

Each phase ships behind the existing flags and is scored on the batch test before
the next begins.

---

## Appendix — Current vs New, as a word equation

**CURRENT comparable value**
= average sold price of
  [ every same-**type** sale in the **postcode** (widened to the whole sector/
    district when thin), **any size, any number of bedrooms, any distance** ],
  HPI-adjusted.

**NEW comparable value**
= average sold price of
  [ the **nearest** same-**type**, same-**bedroom** sales within **X miles** of
    this exact property, closest first, **±20% floor area** where known ],
  HPI-adjusted, with a **new-build premium** when the listing is new,
  falling back to the current method only when too few like-for-like sales exist.

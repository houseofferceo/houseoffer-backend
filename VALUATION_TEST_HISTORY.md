# Valuation-Accuracy Test History

Running log of `/batch-valuation-test` runs (curated 10-property batch) and the
methodology changes between them. Append; don't edit prior rows.

Columns: date | median |gap| vs asking | blanks | notes

| Date | Median \|gap\| | Blanks | Notes |
|---|---|---|---|
| 2026-06-28 | 8.7% | 3 | Baseline. Bedroom-blind comparable average; SE26/S10/BS9 returned 0 comps; B1 new-build −39%, EH10 −40% (454 district comps). Found + fixed an `open_offer` UnboundLocalError (no-method reports 500'd). |
| 2026-06-28 | 8.1% | 1 | After P1 (canonical type matching + non-destructive district fallback) and P2 (bedroom+distance engine). P1 fixed S10/SE26 zero-comps. **P2 inert**: `fetch_sold_nearby` (Rightmove sold pages) returns 0 from the server — blocked for datacentre IPs. New thin-set over-valuations surfaced (SE26 £647k). |
| 2026-06-28 | (diag) | — | Diagnostic run: `nearby_feed_count: 0` on every row confirmed the Rightmove sold feed is dead server-side. Pivoted away from scraping (also against Rightmove T&Cs). |
| 2026-06-28 | 17.0% | 1 | Shipped: guardrail (down-weight thin bedroom-blind outliers), subject bathroom scraping → AVM, and **Option D** (PropertyData `/prices` bedroom-specific method). D fired on only 3/10 — traced to PropertyData rate-limiting under the batch's 4-way concurrency (it also halved comp counts). |
| 2026-06-28 | ~5–6% (first-4) | 0 (so far) | Shipped: retry/backoff on `/prices` + `/sold-prices`, `&concurrency=` knob, and **bedroom signal leads** (when Option D is present, the bedroom-blind comparable drops 2→1). At concurrency=1, Option D fires reliably. **SE26 2-bed: £679k → £568k (+3.2% of asking)** — the headline bedroom-blindness case substantially fixed. SE25 −0.3%, BS10 −8.5%. |

## What changed this iteration (2026-06-28)

Valuation-accuracy work, all shipped to production:

1. **FIX 1–3** (earlier): killed the silent "3-bed semi" default (provenance
   flags); real ±20% size-matching of comparables; floor-area sanity check vs EPC.
2. **`open_offer` crash fix**: reports with no weighted method 500'd.
3. **P1 — robust comparable engine**: canonical property-type matching (variants
   like "Terraced"/"terraced_house"/"End Terrace" all match) + a district fallback
   that never wipes out comps already found. Fixed the zero-comp reports.
4. **P2 — bedroom+distance engine** from `fetch_sold_nearby`: built but **inert in
   production** because Rightmove blocks its house-prices pages for the server
   (`nearby_feed_count: 0`). Kept behind a coords check; falls back to Land Registry.
   (Unblocking would require evading Rightmove's IP block — against their T&Cs.)
5. **Guardrail**: thin (<10) bedroom-blind comparable averages that are >25%
   outliers vs other methods are dropped from the weighted range + flagged low.
6. **Bathrooms**: scraped from the listing and fed to the AVM (was hardcoded to 1).
7. **Option D — bedroom-matched local price**: PropertyData `/prices` filtered by
   the subject's exact bedroom count + type, converted to implied sold value via
   the asking-to-sold discount. The bedroom-aware signal Land Registry can't give.
   This is the main lever — legitimate/licensed data, no scraping.
8. **Bedroom signal leads**: when Option D is present, the bedroom-blind
   comparable average is down-weighted (2→1) so it can't outvote the bedroom-
   specific price.
9. **Resilience**: retry/backoff on `/prices` and `/sold-prices` (rate-limiting
   under load was corrupting comp counts and starving Option D).

## Open items / next levers

- **New-build premium** not yet modelled (B1/CW11 read low vs asking — we value
  new-builds on resale comps). `is_new_build` is scraped but unused in valuation.
- **Broad-comp dilution cap**: very wide district comp sets (100s) can pull a
  valuation low; consider distance/sector tightening or a confidence penalty.
- **BS9-type blanks**: genuinely sparse segments (5-bed detached) still return 0
  comps; the bedroom `/prices` + AVM should carry these once consistently firing.
- **S10 +12.8%**: likely a genuinely keen asking price (local 3-bed avg > asking),
  not an error — confirm against more samples.
- Production note: a single live report makes far fewer concurrent PropertyData
  calls than the batch test, so Option D + full comp sets fire reliably in prod.

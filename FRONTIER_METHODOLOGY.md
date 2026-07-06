# HouseOffer Efficient Frontier — Methodology, Formula & Assumptions

Version 1.0 · 5 July 2026 · Status: **live on the £29 Full Report** (merged `0a0c85c`)
Implementation: `_offer_frontier()` in `app.py` · Tests: `test_frontier_v2.py` (44 checks)
Audience: management review + marketing content. Assumptions are numbered so they
can be referenced, challenged and re-calibrated individually.

---

## 1. The concept

The efficient frontier is HouseOffer's signature negotiation idea, introduced in our
public white paper: **a buyer cannot seek a bigger expected saving without accepting
more risk of losing the property.** There is no free lunch in offers — there is only
choosing your position on the trade-off deliberately instead of by gut feel.

The frontier turns that idea into a per-property curve: discount sought below asking
(x-axis) against qualitative risk of losing the property (y-axis). Time on market
shifts the whole curve in the buyer's favour — the same discount carries less risk
against a seller whose listing has sat. The report presents three named positions on
the curve, each with a £ range, a % range and a plain-English risk description.

## 2. Data inputs

Every input is already computed per report. The frontier adds **zero** new API calls.

| Input | What it is | Source |
|---|---|---|
| `d̄` — local asking-to-sold discount | Avg % below asking that local sales actually complete at | PropertyData `/asking-vs-sold` |
| `r` — DOM ratio | This listing's days on market ÷ local average days on market | Rightmove listing metadata ÷ PropertyData `/avg-days-on-market` |
| Price-reduction history | Whether the asking price has been publicly cut, and by how much | Rightmove listing metadata |
| `weighted_low` | Bottom of the weighted valuation range (data-justified floor) | HouseOffer valuation engine (HM Land Registry PPD, ONS HPI, EPC, PropertyData) |
| `walk_away` | The report's stored walk-away ceiling | HouseOffer valuation engine |
| Asking price | Converts % positions into £ | Rightmove listing |

## 3. The formula

**Step 1 — time-on-market pressure multiplier**

```
r = days_on_market / local_avg_dom
m = clamp(0.5 + 0.5·r,  0.75, 1.75)          (r unavailable → m = 1, stated on the report)
```
A listing at the local average pace (r = 1) gives m = 1: the local clearing discount
applies as-is. Fresh listings (r < 1) pull the anchor below the local norm — down to
×0.75; stale listings push it up — capped at ×1.75 (reached at r = 2.5). The function
is linear between the clamps, so the curve moves continuously with every extra day —
no bucket cliffs.

**Step 2 — price-reduction bonus (percentage points)**

```
b = +1.0pp  if a recorded cut ≥ 5%
    +0.5pp  if any recorded cut
     0      otherwise
```
A public price cut is direct evidence the seller has already moved once.

**Step 3 — the anchor**

```
A = clamp(d̄·m + b,  1.0%, 12.0%)
```
A is the discount the local evidence suggests clears at market-normal risk **for this
listing** — the local norm, scaled by this property's time-on-market pressure, plus
the reduction evidence. The [1%, 12%] clamp is a sanity bound: values outside it
indicate anomalous input data, and the display widens and caveats rather than
trusting them.

**Step 4 — three positions on the curve**

```
SECURE      0.5·A … A
BALANCED    A … 1.5·A
AGGRESSIVE  1.5·A … min(2.0·A, 13.0%)
```
All percentages rounded to 0.5pp; all £ figures to £500 (precise-looking figures
would contradict "range"). The 13% figure is an absolute display ceiling: the
frontier never shows a discount deeper than 13% below asking, whatever the inputs.

## 4. Risk labels — qualitative by design

- **SECURE** — "Very likely to be taken seriously. Low risk of losing it on price —
  but you leave the most money on the table."
- **BALANCED** — "In line with what actually clears in this market once time on
  market is counted. Serious and defensible — expect negotiation, not offence."
- **AGGRESSIVE** — "Beyond what typically clears here. Real chance of rejection or
  being beaten by another buyer — take this position only if you can genuinely walk
  away."

**We publish no acceptance probabilities anywhere.** We hold no offer-level
acceptance data, so any percentage would be fabricated. The report says this to the
customer in as many words ("nobody can honestly give you an acceptance percentage,
so we don't"). This is a deliberate product position, not a gap.

## 5. Safety guardrails (enforced in code, tested)

1. **Valuation floor.** Every implied price is floored at `weighted_low`, the bottom
   of the data-justified valuation range. If a position's whole range falls below the
   floor it collapses onto it and says: *"The sold evidence can't credibly support
   opening lower than this."* The frontier never manufactures discounts the sold
   evidence can't back.
2. **Walk-away ceiling.** Every implied price is hard-capped at the report's stored
   walk-away via an explicit `min()` in code. If a position's whole range sits above
   the ceiling (e.g. SECURE on an overpriced listing) it collapses onto it and says:
   *"Paying more than £X isn't 'secure', it's overpaying."* Whichever position the
   buyer plays, the ceiling never moves. Verified by a 720-combination automated
   sweep across floors, ceilings, discounts, DOM values and buyer profiles: zero
   positions ever display a price above the walk-away.
3. **Display layer only.** The frontier is computed at render time from stored
   values. It never feeds, and can never alter, the valuation or the stored
   open/target/walk-away numbers.

## 6. Honesty states

- **No local discount data** → the national average (4.5%) is used, every band is
  widened ×1.25 around its midpoint, and the card is labelled: *"Based on national
  patterns — local asking-to-sold data unavailable. Ranges widened accordingly."*
- **No DOM comparison** → m = 1 and the card states the curve has not been shifted
  for time on market.
- Reports generated before this release lack the stored local-discount figure and
  correctly render the national-fallback state.

## 7. Personalisation (post-unlock questions)

The buyer's answer to *"How do you feel about this property?"* selects **emphasis
only** — a "Suggested for you" tag and an enlarged dot:

| Answer | Emphasised position |
|---|---|
| "One of several I'm considering" | AGGRESSIVE |
| "I want this one — at the right price" | BALANCED |
| "It's THE one" | SECURE |
| Skipped / unanswered | BALANCED |

All three positions always render. Answers never hide options, never change the
numbers, and never raise the ceiling.

## 8. Worked example

£250,000 asking · local discount d̄ = 5.5% · 99 days on market vs local average 60
(r = 1.65) · no price cut · valuation floor £224,000 · walk-away £249,000.

- m = clamp(0.5 + 0.5×1.65) = **1.325**; b = 0 → **A = 5.5 × 1.325 ≈ 7.3%**
- **SECURE** 3.5–7.5% → £231,000–£241,000
- **BALANCED** 7.5–11% → £224,000–£231,000 (deep end trimmed by the valuation floor)
- **AGGRESSIVE** 11–13% (2.0·A = 14.6% capped at 13%) → whole range falls below the
  floor → **collapses to £224,000**, labelled "at the data floor"

The stored recommended opening (£228,000) sits inside BALANCED — the frontier and
the valuation engine agree without either feeding the other.

## 9. Assumptions register

| # | Assumption | Nature | Rationale |
|---|---|---|---|
| A1 | The local asking-to-sold discount is the best available proxy for "what clears" | Empirical input | Only observed measure of local negotiation outcomes available to us; area-level, not property-level (stated) |
| A2 | Discount tolerance scales linearly with the DOM ratio between clamps | Calibration choice | Direction (longer listing → larger achieved discounts) is well supported by portal and industry research; the slope (0.5) is our choice, bounded by A3 |
| A3 | Multiplier clamps 0.75–1.75 | Calibration choice | Prevents a single very fresh or very stale listing extrapolating to absurd positions |
| A4 | Price-cut bonus +0.5pp / +1.0pp (≥5% cut) | Calibration choice | A public reduction is direct evidence of seller movement; kept additive and small |
| A5 | Anchor clamped to 1–12% | Sanity bound | Outside this, inputs are anomalous; display widens + caveats rather than trusting them |
| A6 | Band multipliers 0.5 / 1.5 / 2.0 around the anchor | Calibration choice | CEO-approved 2026-07-05 (aggressive multiplier reduced from 2.2) |
| A7 | 13% absolute display ceiling | Governance decision | CEO 2026-07-05: deep end read too bold; the cap makes "never more than 13% shown" checkable |
| A8 | National fallback discount 4.5% | Empirical approximation | Same constant as valuation method 7; approximates the UK average asking-to-sold discount; always labelled and widened |
| A9 | Risk expressed qualitatively only | Deliberate product position | No offer-level acceptance data exists to us; probabilities would be fabricated |
| A10 | Rounding to 0.5pp / £500 | Presentation honesty | False precision would misrepresent a modelled range |
| A11 | Frontier never alters the valuation or stored offer numbers | Architecture constraint | Display layer; enforced by construction and asserted in tests |
| A12 | Floor = weighted_low; ceiling = stored walk-away | Architecture constraint | Positions live inside the data-justified range; ceiling capped in code (720-combination test sweep) |

**Calibration status:** A2–A4 and A6–A7 are reasoned calibration choices, not
empirically fitted parameters — we do not yet hold outcome data (accepted-offer
levels) to fit them against. As reports and buyer outcomes accumulate, these
constants are the natural candidates for data-driven revision.

## 10. What the frontier is NOT — banned claims

For marketing and customer-facing copy, the following must never be claimed:

- ❌ Any acceptance probability ("73% chance your offer is accepted")
- ❌ Property-specific offer-outcome statistics (we hold none)
- ❌ Guaranteed savings or guaranteed acceptance at any position
- ❌ That any figure comes from lender data (see the separate "Estimated lender
  range (modelled)" relabel of the same date)
- ❌ That the frontier changes or improves the valuation itself

## 11. Marketing-safe claims

- ✅ "Built from what homes in your area actually sold for, relative to their asking
  prices" (A1)
- ✅ "The longer a property sits on the market, the further the curve shifts in your
  favour" (A2, direction)
- ✅ "Three deliberate positions — secure, balanced, aggressive — each with the
  trade-off spelled out in plain English"
- ✅ "It will never suggest a number above your walk-away ceiling" (A12, tested)
- ✅ "We don't invent acceptance percentages — and we tell you why" (A9)
- ✅ "Your answers personalise the emphasis, never the evidence" (§7)

## 12. References

- HouseOffer white paper — "efficient frontier" concept (`templates/white_paper.html`)
- Phase 1 method proposal, CEO-approved 5 July 2026 (`FRONTIER_V2_PROPOSAL`, session scratchpad)
- Session log with all judgment calls (`SESSION_LOG_2026-07-05.md`, Part 3)
- Implementation: `_offer_frontier()` in `app.py`; template card in `templates/report_paid.html`
- Test evidence: `test_frontier_v2.py` — 44 checks incl. the 720-combination ceiling sweep
- Data sources: HM Land Registry Price Paid Data (© Crown copyright, OGL v3.0); ONS/Land
  Registry House Price Index; PropertyData API (`/asking-vs-sold`, `/avg-days-on-market`);
  Rightmove listing metadata (days on market, price reductions)

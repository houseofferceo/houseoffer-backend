# HouseOffer Backend — Session Log
Date: 2026-07-05
Scope: paid report audit + development (bring report_paid.html up to the
free-report v3 generation; fix what the audit found). Part 2 (same day,
CEO build brief): free-report copy fix, lender relabel, seller signal
score, DOM offer-shift range, post-unlock buyer questions.

═══════════════════════════════════════════════════════════════════════
# PART 2 — CEO build brief (Tasks 1→4→2→3→5), autonomous session
═══════════════════════════════════════════════════════════════════════

All five tasks delivered on the feature branch. NOT merged, NOT deployed.
Hard stops: none triggered — no approved design conflicted, the offer
calculation was never touched, no new spend/keys/services.

## Task 1 — free report copy fix (single line)
- report_free.html:715 rewritten to: "The full report shows the typical
  discount range for properties on the market this long — and how to use
  it in your offer." Nothing else touched (diff verified = 1 line).
- FLAG for CEO (untouched per scope fence, same leverage-note block):
  line 713 still says the full report "widens your negotiating range — a
  lower opening offer and a higher walk-away ceiling both make sense
  here" (the walk-away never rises), and line 717's link text is still
  "Unlock the exact numbers →". Both slightly over-promise vs the new
  range-based reality; one-line fixes when you want them.

## Task 4 — lender relabel (honesty fix)
- app.py: method name → "Estimated lender range (modelled)", source →
  "Modelled — not sourced from lender data" (both branches).
- report_paid.html: methodology footnote under the method table (renders
  only when the method is available): models the typical conservatism of
  lender valuations (90–97% of our lowest independent estimate); not an
  actual lender assessment.
- JUDGMENT CALLS: (a) white_paper.html also updated — the public
  methodology page carried the old name and "the kind of range a mortgage
  surveyor would be likely to support", which is exactly the implication
  being removed; (b) METHODOLOGY_REDESIGN.md left as-is (internal,
  historical analysis); (c) in the football-field CHART the label renders
  as "Est. lender range (modelled)" — the label column is ~169px and the
  full name clipped its first character; the table and footnote carry the
  full name. Pre-existing note: "Bedroom-matched local price (N-bed)" is
  also near the clip width — untouched, flagging only.

## Task 2 — seller signal score
- _resolve_seller_signal() added directly below _resolve_confidence and
  mirrors it exactly: returns (score, reasons[], summary); honest lines
  for every missing input; caps instead of silent skips.
- WEIGHTINGS (judgment call, documented rationale in the docstring too):
  - Time on market (0 to +2, or −1): the strongest public pressure
    signal. dom_signal high (>1.5× local avg) +2; medium +1; low −1 (a
    listing moving at/faster than local pace actively counters a
    pressure read, it isn't merely neutral).
  - Price reduction (0 to +2): direct evidence the seller already moved.
    ≥5% cut +2; any recorded cut +1; none recorded 0 with the honest
    line "no price reduction recorded — the seller hasn't publicly
    moved yet" (covers both "no cut" and "cut we couldn't detect").
  - Local asking-vs-sold discount (−1 to +1): area-level context, not
    property-specific, so it only nudges. ≥5% +1; ≤2% −1 (local sellers
    hold firm); else 0. Uses the RAW local value captured before method
    7 substitutes the 4.5% national fallback into the same variable.
  - Score: ≥3 STRONG, 1–2 MODERATE, ≤0 WEAK. Honesty cap: STRONG is
    never claimed without the time-on-market comparison (capped to
    MODERATE with the cap stated as a reason).
- New report keys (all additive; stored offer values untouched):
  seller_signal_score/reasons/summary, local_sold_discount_pct.
- Template: "Seller motivation" section between £/m² comparison and the
  DOM card. Badge card (STRONG=teal, MODERATE=amber, WEAK=neutral ink on
  sand — weak isn't danger-red, it's just less leverage; red stays
  reserved for the overpriced verdict). Existing DOM and price-drop
  cards now sit under it as evidence rows; missing signals render as
  dashed .signal-note rows ("no price reduction recorded", "no local
  asking-vs-sold data", "couldn't read time on market"). New third
  evidence card for the local asking-vs-sold discount.
- Backward compatible: reports stored before this change (no new keys)
  render exactly as before — verified in tests.

## Task 3 — DOM → offer-shift RANGE
- Buckets keyed off dom_signal itself (single source of truth with the
  ratio thresholds — they can never drift): high → 3–7%, medium → 2–5%,
  low → 0–3%. CEO's suggested ranges kept: they bracket the observed
  asking-vs-sold reality (national avg ~4.5%; method 7 uses ±1%) and no
  better-grounded local dataset exists without new API calls (none made).
- £ conversion rounds to the nearest £500 (precise pounds would
  contradict "range"); low bound of 0 renders as "up to roughly £X".
- Rendered inside the DOM evidence card as a teal strip, with the
  explicit disclaimer: "the typical range for this time on market, not a
  prediction — your recommended numbers come from the valuation methods,
  not the clock." Does NOT feed the offer calculation (context only).

## Task 5 — post-unlock buyer questions + personalisation
- New templates/buyer_questions.html (brand system: sand/ink/teal, Lora
  + Plus Jakarta Sans, card style, radio pills). Plain form POST — works
  without JavaScript. Header: "Three quick questions / so your report is
  written for your exact situation". Prominent skip.
- Gate: view_report shows the questions once per paid report (after the
  building/failed checks, so the unlock-triggered rebuild still shows the
  progress page first). Wired to unlock state (paid=True), not payment,
  as briefed — Stripe later calls the same primitive.
- NEVER BLOCKS: skip button → buyer_profile_skipped; a submission with
  no valid answer is treated as a skip; every path renders the report
  with neutral defaults (verified in tests). Re-answering later updates
  the profile (POST is idempotent, clears the skip flag).
- Storage: stored["buyer_profile"] = {position, attachment, timeline,
  answered_at} (same pattern as buyer_estimate); Sheets webhook row
  type=buyer_profile with the three answers + postcode/verdict/asking/
  report_url; log_event buyer_questions_shown / buyer_profile /
  buyer_profile_skipped.
- Answer codes: position first_time|sold_stc|need_to_sell|cash|investor;
  attachment several|this_one|the_one; timeline fast|one_three|flexible.
- KNOWN LIMITATION (accepted, flagging): the questions gate is per
  report, not per viewer — anyone opening the paid link before the buyer
  answers sees the questions (skippable). Fine while reports are
  single-buyer; revisit if paid links get shared widely.

### 5(A) — negotiation approach card (copy variants for CEO review)
Rendered between the offer headline and the verdict, one bullet per
answered question, .dom-card visual language. Full variants:
- position=cash: "Say you're a cash buyer — explicitly. No chain, no
  mortgage risk: your offer is worth more than a higher offer from a
  buyer in a chain. Make the agent write 'cash buyer' next to your
  number."
- position=investor: "Negotiate like the investor you are. You're
  chain-free and unemotional — the two things sellers pay for in a
  buyer. Anchor on the data and be visibly ready to walk."
- position=sold_stc: "You're proceedable — lead with it. Sold subject to
  contract is the strongest position after cash. A proceedable buyer's
  offer is worth more than a higher offer from someone still stuck in a
  chain, so say so when you offer."
- position=first_time: "You're chain-free. First-time buyers carry no
  chain risk, and sellers value that certainty. Make sure the agent
  records you as chain-free the moment you offer — it strengthens a
  below-asking number."
- position=need_to_sell: "Be straight about your chain. Until your own
  sale is agreed, agents will treat your offer as not fully proceedable
  — expect that to count against you. Price competitiveness matters more
  in your position, and getting your own home under offer will do more
  for your negotiating power than anything else on this page."
- attachment=several: "Comparing several properties is leverage. Sellers
  and agents can tell when a buyer has options. Stay visibly willing to
  walk away — it makes every number on this page more credible."
- attachment=this_one: "You want this one at the right price — so hold
  the line. The walk-away figure above is where 'the right price' ends.
  Decide now that you'll honour it."
- attachment=the_one: "Careful: 'THE one' is how buyers overpay.
  Emotional attachment is the most expensive thing in property. For you
  the walk-away number matters more, not less — write it down before you
  offer, tell someone you trust, and don't cross it in the heat of the
  moment."
- timeline=fast + seller signal STRONG: "Your speed is a bargaining chip
  — and this seller looks under pressure. Offer a fast exchange
  explicitly in return for price. Speed traded for money is the cleanest
  deal in negotiation; don't give it away for free."
- timeline=fast (otherwise): "Your speed is worth money — trade it,
  don't gift it. Tell the agent you can move quickly, but only alongside
  your number: fast completion in exchange for the price you want."
- timeline=one_three: "A 1–3 month timeline is standard — it neither
  strengthens nor weakens your hand. Let the evidence on this page do
  the negotiating."
- timeline=flexible: "Flexibility is an underrated card. Ask the agent
  what completion timing suits the seller, then offer it — matched
  timing can be worth as much as a small price concession."
- stance note (aggressive): "Because you're comparing options from a
  strong buying position, the opening offer above stretches lower within
  the same data-justified range. Your target and walk-away are
  unchanged."
- stance note (tight): "Because this is the one you want and your own
  sale isn't settled, the opening offer above sits closer to target —
  fewer rounds, less risk of losing it. Your walk-away is unchanged: it
  never rises, whatever your answers."

### 5(B) — offer range presentation (display layer)
- _personalised_offer_display(report, profile): pure render-time
  overrides in view_report; the stored report dict is copied, never
  mutated, and the calculation code was not touched (verified: stored
  open/target/walk_away asserted unchanged in tests after answering).
- Movement rules (judgment call on magnitudes):
  - AGGRESSIVE (proceedable position — cash/investor/sold_stc/
    first_time — AND attachment=several): displayed opening drops by 50%
    of the open→target gap, floored at weighted_low, rounded to £1k.
  - TIGHT (attachment=the_one AND position=need_to_sell): displayed
    opening rises by 50% of the open→target gap, capped at target−£1k.
  - Everything else, including the_one with a strong position: numbers
    exactly as stored (the_one gets the warning copy instead of a
    numeric change — attachment should never look like it "unlocked" a
    different valuation).
  - Universal re-caps after rounding: below target−£1k and asking−£1k.
- HARD GUARDRAIL implemented as an explicit min() cap in code (not a
  convention): displayed walk-away = min(candidate, stored walk_away).
  Today no stance proposes a different ceiling; the cap exists so no
  future stance can breach it.
- BUG CAUGHT IN TESTING: when the asking-price caps have already pushed
  the stored opening BELOW weighted_low, the aggressive floor initially
  RAISED the displayed opening above the neutral one. Fixed: floor =
  min(weighted_low, stored open); regression test added.

## Testing (Part 2)
- 60-check suite (scratchpad test_session_0705.py): Task 1 diff, Task 4
  relabels in app/template/white-paper, _resolve_seller_signal across
  strong/moderate/weak/capped/all-missing cases, DOM bucket map + £500
  rounding, display-layer stances + floors + hard guardrail + no-crash
  edge cases, and a full test-client flow: unlock-state gate → questions
  page → answers persist → personalised render (opening stretched
  £228k→£224k, walk-away pinned £249k) → re-answer → tight stance →
  skip path → invalid-only answers → free reports unaffected →
  pre-change stored reports render unchanged. Plus the Part 1 27-check
  template suite still green.
- Screenshots for CEO review (scratchpad): shot_questions.png,
  shot_paid_1/2/3.png, shot_paid_chart.png; before-state screenshots
  from Part 1 (paid_top/mid/bottom.png).

## Audit findings

### Bug (fixed): context-only methods drew off-chart bars
- The football-field chart selected every AVAILABLE method
  (`selectattr('available')`), including weight-0 "context only" rows
  (national-fallback asking-to-sold, matched-sold scoring-only, and any
  comparable method zero-weighted by the thin-set outlier guardrail).
- But the chart axis bounds (chart_price_min/max in build_report_data) are
  computed from weight>0 methods + asking price only. A context-only range
  outside those bounds rendered its bar outside the plot area — reproduced
  with the Phase B matched-sold method (reads high by design): bar drawn at
  x=917 in a 680-wide viewBox, i.e. invisible, leaving a labelled but empty
  chart row. It also visually resurrected the exact outlier the guardrail
  had excluded from the weighted range.
- Fix: the chart now plots weight>0 methods only
  (`selectattr('available') | selectattr('weight')`); the method table still
  lists every method with the "context only" tag, and its sub-line explains
  the distinction. Hard-coded "seven methods" title replaced with the real
  count.

### Parity gaps vs free report v3 (all closed this session)
- No Open Graph tags: a shared paid-report link showed no WhatsApp preview
  card. Added og:title/description/image (main_photo_url with fallback)/url.
- No address-confirmation flow: the backend POST /r/<id>/confirm-address has
  supported paid-tier rebuilds since S20 ("dropdown on every report"), but
  the paid template never exposed it — paid buyers, who paid precisely for
  the address-keyed data (EPC floor area, HPI last sale, AVM), had no way to
  confirm or correct the match unless HPI happened to be missing. Ported the
  confidence-row button + modal (best-match display, sold-candidates
  dropdown, free-text correction) from the free template. The legacy inline
  picker in the HPI section is kept — same routes, second entry point.
- No crowd voting: the voting stack is tier-agnostic and the report keeps its
  id across an unlock, so a buyer who voted and collected family votes on the
  free report LOST the whole crowd section on upgrade. Ported the crowd card
  (input, live feed, markers, crowd-vs-data strip, share button) with
  paid-appropriate copy (no anchoring lecture — they've seen every number;
  no £29 hook). localStorage key is shared, so a pre-upgrade vote restores.
- Nav: old dot logo replaced with the v3 house logo mark; WhatsApp share and
  copy-report-link buttons added (paid-appropriate share text).
- Header: floor-area pill (📐 m²) added to the meta pills.
- Confidence card: renders the same fallback reason as free when there is no
  caveat (comparable count + match level) instead of a bare badge.
- No £99 path: the paid report sold nothing onward. Added a single compact
  Full Playbook card above the attribution (tier=99 track link, consistent
  with the S20 £49→£99 correction).

### Noted, deliberately unchanged
- AVM, rental yield and bedroom-matched price surface only as football-field
  rows — by design; no dedicated cards added.
- /preview-paid reports have no owner-seed vote (no buyer estimate on that
  path); the crowd feed simply starts empty.
- Backend paid build path reviewed end to end (parallel fetch phases, EPC
  resolution, FIX 2/3 floor-area guards, thin-set guardrail, offer caps):
  no defects found.

## Testing
- Jinja render suite (27 checks) across three contexts: full paid data with
  an out-of-range weight-0 method, thin data without report_id, thin data
  with report_id. Covers: OG tags, modal, crowd card, nav share, playbook
  card, floor-area pill, confidence fallback, weight-0 excluded from the SVG
  but present in the table, all bars within the axis, no literal "None",
  ourMid serialising to valid JS null.
- Bug reproduced against the pre-change template (git HEAD copy): weight-0
  bar at x=916.9 + width 593.5 vs axis right edge 635 / viewBox 680.
- Visual verification in headless Chromium at 760px: header, offer headline,
  verdict, confidence row, crowd card, chart, method table, playbook card,
  and the opened modal all render correctly.

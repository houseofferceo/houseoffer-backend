# HouseOffer — Session Log (backend)
Date: 2026-07-07
Scope: "One offer, two lenses" reconciliation — presentation + documentation ONLY.
Branch: `claude/houseoffer-homepage-refresh-port-e2uvqf` (restarted from main).
NOT merged, NOT deployed. Companion frontend work logged in
houseoffer-site.Private/SESSION_LOG_2026-07-07.md (Addendum 3).

⚠️ Calculation code untouched, verified by diff: the trio maths (app.py:2779–2811)
and `_offer_frontier` (app.py:3291+) are byte-identical. app.py has no changes at all.

## What changed

1. **templates/report_paid.html** (presentation only, Jinja syntax validated):
   - Top offer-headline trio gains one line naming it "the value lens" and pointing
     forward to the two-lens section.
   - The frontier section is retitled "One offer, two lenses" and now opens with the
     approved intro copy and two labelled panels — VALUE LENS (what it's worth, with
     a compact trio recap from the existing template vars) and PRESSURE LENS (how
     hard you can push) — before the existing Frontier card, which is retitled
     "The Offer Frontier — discount sought vs risk of losing it" (was "The efficient
     frontier…"; customer-facing name unified as "the Offer Frontier").
   - New "Where the lenses meet" strip after the frontier notes: shared floor/ceiling
     stated once (moved out of ff2-note to avoid duplication), plus the conditional
     narrative — overpriced verdict → the convergence explanation; otherwise → the
     agreement-is-a-strong-signal line.
   - New CSS: .lens-chip/.lens-intro/.lens-grid/.lens-panel/.lens-trio-recap/
     .lens-meet. Existing palette vars only.

2. **FRONTIER_METHODOLOGY.md** → v1.1: new §13 "Two lenses — reconciliation with the
   offer trio": full trio formula + cap sequence (previously documented nowhere),
   frontier cross-reference, the shared-bounds-only statement, the CEO Option B
   decision (two lenses, never merged), the canonical one-sentence model, the
   overpriced-convergence note ("designed behaviour — do not fix"), and the
   generated-not-authored rule for marketing samples.

3. **tools/demo_two_lenses.py** (new): executes the REAL trio source (exec of the
   exact app.py lines, anchored so it fails loudly if the code moves) and the real
   `_offer_frontier` against the canonical fictional property. Output for the demo:
   weighted £352,000–£368,000 (mid £360,000); trio £357,000 / £360,000 / £368,000;
   frontier anchor 5.2%, Secure £366,000–£368,000 (2.5–5%), Balanced
   £354,000–£366,000 (5–8%), Aggressive £352,000–£354,000 (8–10.5%), emphasis
   Balanced, nothing collapsed. These figures are the single source for all frontend
   demo surfaces.

## Judgment calls
- **Trio extraction via exec of source slices** rather than importing app.py: module
  import needs Flask + live env; exec of the verbatim lines keeps the arithmetic
  byte-identical to production while runnable anywhere. Anchored on exact code
  strings; moves/renames break the script loudly instead of silently going stale.
- **Demo method inputs chosen so the real calc lands on the long-established example**
  (weighted £352k–£368k, mid £360k, asking £385k): preserves every downstream example
  number (verdict 7%, hero card, crowd Data chip) while making the trio/frontier
  genuinely generated. Consequence accepted and implemented on the frontend: the
  displayed per-method bars change to the harness inputs so the weighted range
  actually follows from the displayed bars under the real formula (it never did
  before).
- **ff2-note trimmed**: its floor/ceiling sentence moved into the "Where the lenses
  meet" strip so the guardrail is stated once, in the two-lens frame.
- **Harness committed to the repo** (tools/) rather than left in the session
  scratchpad: the generated-not-authored rule is only enforceable if the generator
  is version-controlled.

## Consistency check — one-sentence model per surface
Canonical: "The value lens (open · target · walk-away) tells you where to land inside
what the home is worth; the pressure lens (Secure · Balanced · Aggressive) tells you
how hard the seller's position lets you push — two readings of one offer, and neither
ever points past your walk-away."
- Paid report: intro + panels state it across .lens-intro/.lens-panel-body; meet
  strip carries the shared-ceiling clause.
- Dev doc: §13.3 quotes the canonical sentence verbatim.
- Homepage + white paper: see frontend session log (same sentence, teaser and
  concept forms).

## Verification
- `git diff app.py` → empty (hard constraint honoured).
- Jinja parse of report_paid.html → OK.
- tools/demo_two_lenses.py runs green; output embedded above and in §13.5.

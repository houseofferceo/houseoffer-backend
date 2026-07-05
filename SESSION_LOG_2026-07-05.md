# HouseOffer Backend — Session Log
Date: 2026-07-05
Scope: paid report audit + development (bring report_paid.html up to the
free-report v3 generation; fix what the audit found).

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

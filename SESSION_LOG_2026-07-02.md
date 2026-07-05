# HouseOffer Backend — Session Log
Date: 2026-07-02
Management summary: Drive doc "HouseOffer — Actions & Decisions Log (Session 20)".

## What was built this session

### 1. Free report template v3 (report_free.html rebuilt)
- Rebuilt to the approved design mockup v3: valuation card first (real weighted
  midpoint + range shown free), diff badge vs asking, confidence badge + reason,
  always-amber opportunity hook, verdict hook card, crowd voting section,
  ammunition box, locked football field, efficient-frontier DOM card, single
  £29/£99 upgrade section, new logo mark, WhatsApp + copy-link in the nav.
- Blur rule enforced: free-tier football-field methods (comparables adj/raw,
  HPI-adjusted last sale) render REAL bars + midpoint figures from
  `football_field` data against the chart axis; paid methods and the offer trio
  stay blurred with placeholder digits (real numbers never enter the free DOM).
- Playbook price corrected £49 → £99 ("Expert buying agent support"); tracking
  links moved to tier=99. Open Graph tags added to the report page.
- Old template recoverable via git tag `free-report-baseline-2026-07-02`.

### 2. Address confirmation (accuracy loop)
- `POST /r/<id>/confirm-address`: plain confirm = logged, zero cost; corrected
  address = background rebuild via `_start_rebuild` at the report's own tier,
  capped at 2 corrections/report. Guards: 404/409/400/429.
- `last_sale_candidates` now populate on EVERY report (Land Registry first), so
  the modal always offers the sold-properties dropdown — a confident last-sale
  match can still be the wrong property.

### 3. Crowd voting ("Polymarket for property", Session 19 spec)
- `POST /r/<id>/share-link` mints a reusable 5-char slug; `/v/<slug>` is a
  lightweight voting page (photo, address, one input, optional first name, no
  signup, zero PropertyData cost). Asking price revealed only AFTER the vote
  locks in (anchoring control). Own OG tags for WhatsApp preview cards.
- `POST /api/vote` (report_id or slug), one vote per voter cookie per property,
  revote updates. `GET /api/votes/<id>` live feed; requester's own vote is
  excluded (rendered locally as "You"). 6s polling on both pages.
- Buyer's form estimate auto-seeds as the first vote ("The buyer",
  source=owner_seed) when a report finishes building.
- Every vote streams to the Sheets webhook as `type=vote` with asking price /
  our valuation / verdict snapshots (Items 63/64 groundwork). Local JSON on
  /tmp feeds the live display only; the Sheet is the durable copy.
- Scrapers capture the portal `og:image` (`main_photo_url`) — flows through
  merge_scraped_listing/build_report_data and survives rebuilds.

## Decisions / reversals (CEO, this session)
- REVERSAL vs S19: free-tier football-field rows un-blurred (never blur a
  number already free on the page). Paid rows stay locked.
- REVERSAL vs S19: crowd-vs-data comparison is FREE (live strip in the crowd
  box); £29 hook remains the offer strategy.
- Vote storage: Google Sheet as durable store (accepted: live feeds reset on
  deploy). Address correction: immediate rebuild, capped at 2. Voting-page
  photo: og:image scrape. £29 card restyled teal-light.

## Testing
- Jinja render tests across full/thin/value/fair/no-report_id contexts.
- Flask test-client suites: confirm-address (6 cases), voting stack (9 cases:
  slug mint/reuse, both vote entry points, update path, self-exclusion, name
  sanitisation, asking-price hold-back on /v/, expired slugs, validation,
  Sheets payload shape), owner-seed visibility in a friend's feed.
- NOT yet tested end-to-end with live data: PropertyData credits exhausted
  (days-on-market card + comparable quality pending credit reset).

## Deploy
- Merged to main and live on Render: 013e8c5, 765d604, c9e5521.
- CEO updated the Apps Script webhook (Votes tab handler) and redeployed it.

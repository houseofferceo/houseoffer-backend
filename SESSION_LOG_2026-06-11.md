# HouseOffer Backend — Session Log
Date: 2026-06-11

## What was built this session

### 1. Buyer estimate shorthand input
- Users can now enter `285` instead of `285000` in the "what do you think it's worth" field
- `normalise_buyer_estimate()` treats values under 10,000 as thousands
- Also accepts `285k`, `£285,000`, etc.

### 2. Generating report page
- Replaced blank wait screen with a branded spinner page matching the site palette
- Shows 6 animated checklist steps (Reading listing, Land Registry, EPC, Rents, Valuation models, Writing report)
- JS polls `/r/<id>/status` every 3 seconds and redirects when ready
- noscript fallback with meta refresh

### 3. Background thread fix — Load failed resolved
- Rightmove scraper was running synchronously, causing Render's 30s proxy timeout
- Moved all slow work (scrape, report build, email, Sheets) into a daemon thread
- `/submit` now returns in under 100ms with `status: "building"` and a `report_id`

### 4. Free report — previous sold price and address dropdown restored
- `find_last_sale` (Land Registry SPARQL) moved to both free and paid tiers
- `get_last_sale_candidates` also runs on both tiers
- SPARQL returning 0 results from Render's IP range; PropertyData fallback handles recent sales

### 5. Free report — football field (blurred SVG)
- Full SVG football field with method bars, weighted band, asking price line, recommended offer line
- Bars and weighted band blurred via SVG `<filter>` (`feGaussianBlur`)
- Asking price and recommended offer lines/labels remain crisp
- `min-width: 500px` wrapper prevents label clipping on mobile
- Axis labels moved from SVG pills to HTML below the SVG

### 6. Free report — lock overlay CTA
- Single `position:absolute;inset:0` overlay covers both the football field chart and the offer trio
- Shows lock emoji, "Your numbers are ready", brief description, "Unlock for £29" teal button
- Same treatment on the comparables table — first 3 rows visible, rows 4+ blurred with overlay
- Blur levels tuned to be visible-but-not-legible (SVG: 2.5, numbers: 3px, rows: 2.5px)

### 7. Free report — HouseOffer logo in nav
- SVG house logo mark added to the nav alongside the wordmark
- Links back to houseoffer.netlify.app

### 8. Free report — share buttons
- Discrete share buttons (WhatsApp, Copy link) in the top nav
- "Share this tool" label hidden on mobile under 480px
- Full share banner retained at the bottom of the report

### 9. Comparables accuracy handling
- Gap 0-20%: figure shown, no warning
- Gap 20-50%: figure shown + amber accuracy note explaining no bedroom/size filter
- Gap >50%: comparable figure suppressed entirely, replaced with "Comparables unreliable" warning card and pointer to paid report

### 10. CSS fix — `--forest` undefined variable
- All `var(--forest)` occurrences replaced with `var(--teal)` throughout report_free.html

---

## Active constraints (carry forward to all sessions)
- Do not install new pip packages without asking first
- Do not modify Render deployment or environment variables
- Do not touch reddit_monitor.py
- No em-dashes anywhere in text or code comments
- All prices in GBP with £ and commas
- UK English throughout
- Discuss before coding — confirm approach before making any changes

## Known issues / parked
- SPARQL returning 0 results from Render's IP range for some postcodes; PropertyData fallback works
- Comparables not filtered by bedrooms (Land Registry has no bedroom data) — handled by the >50% suppression
- Floor-plan vision (Claude Haiku + ANTHROPIC_API_KEY) — parked
- UDPRN verification — parked
- Stripe integration — parked
- "What do you think it's worth" placeholder text on houseoffer.uk frontend (Netlify repo, separate session)

## Repo / deployment
- Repo: houseofferceo/houseoffer-backend
- Deployed on Render from `main` branch
- Procfile: `gunicorn app:app --timeout 120 --threads 4`
- Feature branches developed under `claude/` prefix, merged to main to deploy

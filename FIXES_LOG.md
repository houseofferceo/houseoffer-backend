# HouseOffer Fixes Log

Running record of issues, root causes, decisions and the commits that fixed them.
UK English. All prices GBP.

---

## Fixed

### Walk-away price exceeded asking by £100k+ (WR2 5SG test)
- Date: 2026-06-10
- Symptom: Walk-away £528,000 vs asking £420,000 on a fairly-priced property.
- Root cause: Methods 1a/1b use Q3 (75th percentile) of all comparable sold
  prices as the high bound. The radius-based comparable set mixes house sizes,
  so large detached outliers (£621k-£663k) pushed Q3 to ~£600k, dragging the
  weighted high to £528k.
- Rule adopted: walk-away never exceeds asking price + 5%. Opening offer is
  always below asking (asking - £1,000 cap, applies on every verdict).
  After caps, ordering is re-enforced: open < target < walk-away.
- Commit: 0ebce79 (merged to main in b54c903).

### Opening offer above asking price
- Date: 2026-06-10 (earlier in session)
- Rule: open_offer = min(open_offer, asking_price - 1000), universal.
- Commit: b937e65.

### HPI last sale matching neighbour's property
- Date: 2026-06-10 (earlier in session)
- Root cause: street-token matching alone matched any house on the street.
- Fix: tiered matching in find_last_sale - exact postcode filter, then street
  tokens, then house number required when the subject address has one. If no
  house number and multiple candidates, return None and populate
  last_sale_candidates instead of guessing.
- Commit: c7d4820.

### £/sqm column empty in comparables table
- Date: 2026-06-10 (earlier in session)
- Root cause: PropertyData /sold-prices has no floor areas.
- Fix: cross-reference /sold-prices-per-sqf records (which carry address,
  price, sqf) by normalised address key.

---

## Open issues (working in order)

### 1. HPI-adjusted last sale shows n/a (IN PROGRESS)
- Symptom: WR2 5SG report shows HPI last sale n/a despite the property having
  sold in 2024 (£398,050) and 2025 (£485,000) per Rightmove history.
- Root cause: Rightmove displayAddress is street-only (no house number) for
  this listing. find_last_sale step 3c correctly refuses to guess between
  multiple sold properties at the postcode. This is the intended safe
  behaviour after the neighbour-match fix; the gap is that we have no way to
  identify which candidate is the subject property.
- Note: Rightmove's own sold-history XHR is off limits (ToS prohibits
  third-party use) and the data is not in PAGE_MODEL.
- Candidate fixes:
  a. Let the user supply the house number / full address (form field or
     candidates dropdown built from last_sale_candidates, already returned
     in report data).
  b. Heuristic auto-match - rejected, this is what caused the neighbour bug.
- Decision: pending discussion.

### 2. Price per m2 shows n/a
- Likely cause: EPC floor-area lookup failing for the subject (same
  street-only address problem, or no EPC record). Not yet diagnosed.

### 3. Rental yield shows n/a
- Likely cause: PropertyData /rents returning no data for this
  postcode/type/bedrooms combination. Not yet diagnosed.

### 4. Days on market missing from report
- Previously worked. Likely lost in a merge/overwrite. Not yet diagnosed.

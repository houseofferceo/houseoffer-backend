# Address Identification: Build Summary and Methodology

Status: 2026-06-17. Purpose: brief the management team on what we have built to
identify a property's full address from a Rightmove listing, how it compares to
the Chrome address-finder extensions and to HomeThink, and the options to make it
better.

## The problem

Rightmove deliberately hides the house number on many for-sale listings (it shows
"Abbotsbury Road, Weymouth" but not "120 Abbotsbury Road"). We need the full
address for two reasons: to display it, and (more importantly) to pull the exact
property's HM Land Registry sale history, which drives the valuation.

## Bottom line

There is no legitimate server-side method that reliably returns the exact house
number, and no competitor has one either. Every server-side "paste a URL" tool we
examined (HomeThink, the Apify actors, open-source clones, and us) uses the same
EPC-register matching method we have built. The Chrome extension's only real
advantage is that it runs inside the user's browser, which a backend cannot
replicate without breaching Rightmove's terms. We are not behind on technique. The
gap is execution quality, plus one AI-assisted matching layer competitors have and
we do not.

## What we have built (all live unless noted)

| Component | Status | Notes |
|---|---|---|
| Conservative resolution guardrails | Live, works | Stops damaging false matches (e.g. presenting a neighbour's address and sale price as the subject's). Only a coordinate, EPC, or house-number match is trusted; otherwise the report keeps the street-only address and shows the picker. Zero regression for unresolved listings. |
| EPC register cross-match | Live, works mechanically | Matches the listing against EPC certificates at the postcode. Now decides on floor area first (property type only breaks a tie), candidate cap raised to 30, certificates fetched in parallel. Resolves the resolvable subset only. |
| Land Registry sale history | Live, works | Once the house number is known, returns the correct sold-price history (free, official data). |
| Address picker fallback | Live, works | When we cannot resolve, the user self-selects from postcode candidates. Reliable and legal. The honest backbone. |
| Coordinate / map-pin matching | Dead end | Rightmove's pin is a postcode centroid, not the property; their sold data with coordinates is loaded client-side and is off limits under their terms. |
| UDPRN to address (Royal Mail PAF) | Dead end | Tested for free via Ideal Postcodes: the listing's delivery_point_id is not a real UDPRN ("No UDPRN found"), so PAF cannot use it. |

## What we measured

On a 6-listing test batch, EPC cross-match resolved 0 of 6. This was not due to
bugs (those are fixed); the failures are structural:

| Listing | Scraped size | Why it failed |
|---|---|---|
| Pembroke Place | 164 m2 | Scraped size matches no house on the street (street tops out at 140 m2). Likely a floor-area scrape error, or the subject has no EPC. |
| Thornsbeach Road | 277 m2 | Same pattern: scraped size exceeds every EPC on the street (max 216 m2). |
| Russell Road | 213 m2 | Three houses are ~213 m2: genuinely ambiguous. |
| Chapel Farm Road | 115 m2 | Seven houses are ~115 m2: genuinely ambiguous. |
| Merrion Avenue | none | No floor area scraped, so nothing to match on. |
| Bleak Hill Lane | 244 m2 | No EPC registered on the street at all. |

EPC resolves a minority of listings: distinctively-sized homes on small,
EPC-registered streets with an accurate scraped floor area. It is a bonus, not the
engine.

## Methodology comparison

| Dimension | Chrome extension | HomeThink (and Apify tools) | HouseOffer (us, now) |
|---|---|---|---|
| Where it runs | User's browser | Server | Server |
| How it gets the address | Reads Rightmove's in-browser data; coordinate / sold match | EPC matching + AI extraction + public-data cross-reference | EPC matching + Land Registry + picker |
| Gets exact house number? | Often (registered/sold homes) | Sometimes (EPC-resolvable subset; how often is unconfirmed) | Sometimes (same EPC-resolvable subset) |
| Works when never sold / no EPC? | No (falls back to postcode + Street View) | No (area-level) | No (picker) |
| Needs the user to install anything? | Yes (significant funnel cost) | No | No |
| Rightmove terms-of-service footing | Grey area, tolerated | Public data is fine; server-side scraping of Rightmove would be grey | Clean (we do not touch Rightmove's private API) |
| Blockable by Rightmove | Low (real user session) | Medium | Medium |
| AI-assisted matching | No | Yes (broadens coverage) | Not yet (the real gap) |

## Dead ends (tested and ruled out)

- Coordinate / pin matching: the pin is postcode-centroid, not the property.
- UDPRN to address: the listing's delivery_point_id is not a real UDPRN.
- Server-side scraping of Rightmove's private API: against their terms, easily
  blocked from a datacentre IP, and fragile. Not pursued.

## Options to improve

| Option | Impact | Effort | Legitimate? | Notes |
|---|---|---|---|---|
| 1. Fix floor-area extraction (AI or better parser) | High | Medium | Yes | We found scraped sizes that match no house on the street. This also corrupts the price-per-m2 and AVM numbers on every report, so it is worth fixing regardless of addresses. |
| 2. AI-assisted matching layer | Medium-High | Medium | Yes | The one thing competitors do that we do not. Feed the listing text and photos plus the EPC candidate list to a model to pick the best-supported house. Targets cases where the right address is in our candidate set but we cannot currently choose (Russell Road's 3 ties, Chapel Farm's 7). |
| 3. De-duplicate EPC certificates by address | Low-Medium | Low | Yes | The same property has multiple certificates over time, inflating ambiguity. Quick win. |
| 4. Make the address picker excellent | High (reliability) | Low-Medium | Yes | The fallback everyone relies on, competitors included. Polish: map thumbnails, sold-price hints, one-tap select. |
| 5. Browser extension or bookmarklet | High (only true parity) | High (product pivot) | Grey area | The only way to match the Chrome extension, because it runs in the user's browser. Needs install, Chrome Web Store review, ongoing upkeep, and a read on Rightmove's terms. A strategic bet, not a backend change. |

Rejected: headless server-side scraping of Rightmove's API (terms breach,
blockable, fragile).

## Recommendation

Options 1 to 4 are all legitimate, low risk, and will lift our resolution rate and
accuracy, but even combined they will not reach the Chrome extension's coverage,
because the structural limits (uniform streets, missing EPCs, missing floor areas)
are real for everyone. Only Option 5 (becoming an extension) achieves true parity,
and that is a product-direction decision.

Pragmatic path: do 1 + 2 + 4 to be genuinely competitive with HomeThink on the
legitimate server-side approach, and treat Option 5 as a separate strategic bet if
exact-address-on-everything is judged essential to the business.

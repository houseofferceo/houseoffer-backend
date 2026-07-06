# HouseOffer frontend (staged copy)

**What this is:** the houseoffer.uk Netlify site, refreshed per the CEO's
homepage brief of 2026-07-06. It is staged *inside the backend repo* only
because the actual frontend repo could not be attached to the build session
(details in `SESSION_LOG_2026-07-06.md` at the repo root).

**Base version:** imported verbatim from the CEO's own archive
`houseoffersite.zip` in Google Drive (16 June 2026) — the newest available
copy of the deployed site. If the live site was changed after 16 June,
re-apply the diff of the two commits on this branch rather than copying
files blindly.

**To deploy (after CEO sign-off only):** copy the *contents* of this
folder over the frontend repo that Netlify builds from, commit, push.
Nothing here touches the backend.

Contents:
- `index.html` — refreshed homepage
- `sample-report/` — NEW static sample £29 report (fictional property,
  figures blurred, `noindex`)
- `negotiation/` — one line changed (£49 → Full Playbook £99)
- `faq/`, `buying-hub/`, `offer-strategy/`, `property-value/`,
  `og-image.png` — unchanged from the 16 June snapshot

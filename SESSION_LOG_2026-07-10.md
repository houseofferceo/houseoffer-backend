# Session log — 2026-07-10 — Stripe setup (£29 report unlock)

Stripe Checkout is now wired end-to-end for the £29 Offer Report. The
previously parked integration calls the existing unlock primitive
(paid=True + paid-tier rebuild), exactly as briefed on 2026-07-05.
No SDK added — Stripe's REST API is called with `requests`, matching how
Resend/PropertyData/Sheets are integrated, so requirements.txt is unchanged.

## Flow

1. Free-report CTA → `GET /r/<report_id>/checkout?src=<cta>` — creates a
   Stripe Checkout session server-side (GBP, amount from
   `STRIPE_REPORT_PRICE_PENCE`, default 2900; buyer email prefilled;
   `report_id` in session + payment-intent metadata) and 303-redirects to
   Stripe's hosted payment page. Logs a `checkout_started` event with the
   CTA source. If the report is already paid it just redirects to `/r/<id>`.
2. On payment, Stripe redirects to `/r/<id>/checkout/success?session_id=…`.
   The session is re-fetched with the secret key (the query string alone is
   never trusted), and if `payment_status=paid` and the session's metadata
   matches the report, it unlocks immediately — no webhook latency.
3. `POST /stripe/webhook` is the durable backstop (buyer closes the tab
   before returning). Signature is verified manually (HMAC-SHA256 over
   `t.payload`, 5-minute replay tolerance). Handles
   `checkout.session.completed` + `checkout.session.async_payment_succeeded`.
4. Both paths call `_unlock_report` (refactored out of `/admin/unlock`,
   which still works as the manual support tool via `force=True`). It is
   idempotent — the second fulfilment path no-ops, so the rebuild and the
   owner "💰 Payment received" email fire exactly once. Every payment also
   streams to the Sheets webhook as a `type=payment` row.

## Failure behaviour

- `STRIPE_SECRET_KEY` unset (i.e. before go-live) or Stripe API error →
  checkout CTAs gracefully fall back to houseoffer.uk/#pricing, never a
  dead link. Deploying this before configuring keys is safe.
- Bad/missing webhook signature → 400, nothing unlocks.
- A session bound to report A can never unlock report B (metadata check).

## Go-live checklist (Render + Stripe dashboard)

1. Stripe dashboard → Developers → API keys: copy the live secret key.
2. Render env vars on houseoffer-backend:
   - `STRIPE_SECRET_KEY` = sk_live_…
   - `STRIPE_WEBHOOK_SECRET` = whsec_… (from step 3)
   - optional `STRIPE_REPORT_PRICE_PENCE` (defaults to 2900)
3. Stripe dashboard → Developers → Webhooks → Add endpoint:
   `https://houseoffer-backend.onrender.com/stripe/webhook`
   with events `checkout.session.completed` and
   `checkout.session.async_payment_succeeded`. Copy the signing secret.
4. Test-mode dry run first: set `STRIPE_SECRET_KEY` to sk_test_…, pay with
   card 4242 4242 4242 4242 on a real free report, confirm the paid rebuild
   kicks off and the owner email arrives, then swap to live keys.

## CTA changes

- report_free.html: all four £29 CTAs (mobile bar, locked football-field
  section, leverage strip, upgrade card) now hit `/r/<id>/checkout?src=…`.
- £99 Playbook CTAs untouched — the playbook is "in build / join the list"
  on the homepage, so it stays on `/track` lead capture (no payment taken).
- report_email.html: playbook line said £49; corrected to £99 to match
  every other surface.
- Homepage (site repo): the £29 pricing button used to hit `/track`, which
  redirected straight back to `#pricing` — a circular dead end. Checkout is
  per-report, so it now goes through `/track?…&next=form` (click still
  tracked + owner email) and lands on `#hero-form` to start the free report.

## Verified

28 functional checks against the Flask test client with Stripe/network
mocked: session creation params, 404s, already-paid short-circuit, webhook
signature accept/reject (bad sig, wrong secret, stale timestamp), unlock +
single rebuild + single owner email, duplicate-webhook idempotency,
success-redirect verification (paid/unpaid/forged/cross-report session ids),
admin unlock unchanged, no-key and Stripe-500 fallbacks, /track next=form.

## Still parked

- £99 playbook purchase (product not built — waitlist only).
- Refund handling (`charge.refunded` → re-lock) — manual for now via
  admin; add if volume warrants.

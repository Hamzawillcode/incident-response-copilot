---
services: [shipping, quote]
tags: [shipping, delivery, quote, shipping cost, international, slow quote, latency, rust, php]
owner: orders-team
last_reviewed: 2026-10-15
---
# Shipping and quote: slow or failing shipping quotes

## Overview
`shipping` (Rust) answers `GetQuote` and `ShipOrder`. For a quote it calls `quote` (PHP,
`POST /getquote`, internal span `calculate-quote`). `checkout` calls GetQuote while
preparing an order (inside `prepareOrderItemsAndShippingQuoteFromCart`) and ShipOrder
after the payment. `frontend` also asks for quotes on the cart page.

## Symptoms
- Checkout latency rising, with the time spent in shipping spans.
- `shipping` p95 latency rising, sometimes only for part of the requests (for example
  only some destinations), which makes p95 jump while the median stays normal.
- Shipping or quote errors in checkout traces.

## Diagnosis
1. `get_service_metrics(service="shipping")` and `get_service_metrics(service="quote")`:
   which operation is slow or failing.
2. Slow traces: `get_traces(service="shipping", min_duration_ms=...)`. If the time is in
   shipping's own spans (large self time) and `quote` answers quickly, the delay is in
   shipping. If `calculate-quote` is slow, it is in quote.
3. Look for a pattern in the slow requests: destination country, order size or one
   route. Compare the attributes of slow and fast spans.
4. Logs: `search_logs(service="shipping", level="warn")` and the same for `quote`.

## Common causes
- A slow external carrier or rate lookup for some destinations (international orders).
- A bad shipping or quote release.
- `quote` down: shipping quotes fail and checkout fails.

## Remediation
- Delays for some destinations: roll back the most recent shipping configuration or
  deployment change. If the carrier itself is slow, use cached or flat-rate quotes as a
  fallback.
- Bad release: roll back.
- Quote down: restart `quote` and check its logs.

## Verification
- Shipping p95 back to its baseline for all destinations, and checkout latency back to
  normal.

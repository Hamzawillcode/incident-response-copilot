---
services: [checkout]
tags: [place order, order, checkout failing, purchase, buy, orchestration]
owner: orders-team
last_reviewed: 2026-10-15
---
# Checkout: orders failing or slow

## Overview
`checkout` (Go) runs `oteldemo.CheckoutService/PlaceOrder`. It owns no data and
orchestrates other services. In order, it calls `cart` (GetCart), `product-catalog`
(GetProduct for each item), `currency` (Convert), `shipping` (GetQuote), `payment`
(Charge), `shipping` (ShipOrder), `cart` (EmptyCart) and `email` (order confirmation),
then publishes the order to `kafka` (`publish orders`). If any of them fails, the order
fails. That is why checkout errors are usually a symptom.

## Symptoms
- `POST /api/checkout` errors in `frontend` and `PlaceOrder` errors in `checkout`.
- Checkout p95 latency rising.
- Users report "something went wrong" when they pay.

## Diagnosis
1. Run `get_traces(service="checkout", errors_only=True)` and read
   `summary.error_origins`:
   - `payment :: charge` or a failing `oteldemo.PaymentService/Charge`: payment runbook;
   - `oteldemo.CartService/...` or `valkey-cart` spans: cart runbook;
   - `oteldemo.ProductCatalogService/GetProduct`: product-catalog runbook;
   - `oteldemo.CurrencyService/Convert`: currency runbook;
   - shipping or quote spans: shipping runbook.
2. If the failing span is checkout's own client span to a dependency and that dependency
   has no server span (no traffic), the dependency is unreachable: down, wrong address or
   DNS.
3. Slow checkout: in slow `PlaceOrder` traces (`min_duration_ms` about twice the usual
   p95), find the child span with the largest self time.
   `prepareOrderItemsAndShippingQuoteFromCart` covers cart, catalog, currency and the
   shipping quote.
4. If no dependency fails and the origin is inside checkout, read the checkout logs with
   `level="warn"`.

## Common causes
- A failing or slow dependency (by far the most common).
- A dependency that cannot be reached (crashed or misconfigured address).
- A bad checkout release: errors from PlaceOrder itself, right after a deployment.
- Publishing problems: orders succeed for the user, but `publish orders` is slow or
  failing. See the kafka runbook.

## Remediation
- Fix the dependency named by the error origins, following its runbook. Restarting
  checkout does not help when a dependency is failing.
- If checkout itself was just deployed and the origins are inside checkout, roll back
  the deployment.

## Verification
- `PlaceOrder` error ratio back under 1 %, `POST /api/checkout` succeeding, and p95 back
  to its baseline.

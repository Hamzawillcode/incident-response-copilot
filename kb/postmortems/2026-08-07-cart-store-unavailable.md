---
incident_id: INC-2026-0807
date: 2026-08-07
severity: SEV-2
services: [valkey-cart, cart, checkout, frontend]
root_cause_service: valkey-cart
failure_type: unavailable
duration_minutes: 22
---
# Postmortem: checkout failing because the cart store was unavailable

## Summary
The `valkey-cart` store was restarted during host maintenance and stayed down for about
20 minutes. Every cart operation failed, so users could neither add items nor check out.
Checkout had the highest error count and was investigated first, although it was only
passing on errors from cart.

## Impact
- Adding to cart and checkout failed for all users for about 22 minutes.
- All carts were lost, because the store had no persistence.

## Timeline (UTC)
- 11:30 Host maintenance restarted the valkey-cart container. It did not come back
  because of a bad volume mount.
- 11:31 Cart error ratio near 100 %, followed by checkout and frontend errors.
- 11:33 Alert fired on checkout.
- 11:38 Failed checkout traces showed the error origin in
  `oteldemo.CartService/GetCart`, and below it the `valkey-cart:6379` client span with
  "connection refused".
- 11:48 Volume mount fixed and valkey-cart started.
- 11:52 Error ratios back to normal.

## Root cause
The cart store was down. Cart could not reach it and failed every request, and checkout
and frontend failed as a consequence. The deepest cause was the store (valkey-cart), not
cart's code and not checkout.

## Detection and diagnosis
- Checkout had the most errors because it calls cart twice per order, which drew
  attention there.
- The error origins in the traces pointed below cart, to the valkey client span, within
  minutes.

## Resolution
The volume mount was fixed and valkey-cart started. The lost carts could not be
recovered.

## Lessons and action items
- The service with the most errors is not necessarily the cause. Follow the error
  origins.
- A failing client span to a data store, with "connection refused", means the store is
  unavailable.
- Add a health check and an alert on valkey-cart itself. Consider persistence for carts.

---
services: [cart]
tags: [shopping cart, add to cart, valkey, redis, cache store, empty cart, readiness]
owner: orders-team
last_reviewed: 2026-10-15
---
# Cart service: cart operations failing

## Overview
`cart` (.NET) serves `oteldemo.CartService/AddItem`, `GetCart` and `EmptyCart` and keeps
carts in `valkey-cart`, a Redis-compatible store (client spans named `valkey-cart:6379`).
`frontend` calls it for the cart page, and `checkout` calls it twice per order
(GetCart, then EmptyCart).

## Symptoms
- `POST /api/cart` errors in `frontend`, and cart errors in `checkout` traces.
- Checkout errors whose origin is a `CartService` span.
- Cart latency rising.

## Diagnosis
1. `get_service_metrics(service="cart")`: do all operations fail, or only one (for
   example only `EmptyCart`)?
2. `get_traces(service="cart", errors_only=True)`: is the failing span the cart's own
   server span, or the `valkey-cart:6379` client span below it? A failing valkey span
   means the store is the problem.
3. Logs: `search_logs(service="cart", level="warn")` for exceptions and connection
   errors to valkey.
4. Valkey health: memory and connections from the Redis metrics (for example
   `redis_memory_used_bytes` and `redis_clients_connected`), and whether the store
   restarted (all carts suddenly empty).

## Common causes
- `valkey-cart` unavailable or restarted: connection errors, every operation fails,
  carts are lost.
- A bad cart release: errors in one operation, with the origin inside cart.
- Cart instances not ready after a restart (failing readiness checks), so requests are
  refused for a while.

## Remediation
- Valkey down: restart `valkey-cart`. Carts may be lost, so tell users.
- Bad release: roll back the cart deployment or configuration change.
- Instances not ready: check the readiness endpoint and restart unhealthy instances.

## Verification
- Cart error ratio about 0 %, no more cart origins in checkout traces, and valkey client
  spans succeeding.

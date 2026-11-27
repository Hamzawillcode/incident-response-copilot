---
services: [frontend, frontend-proxy, checkout, cart, payment, product-catalog, currency, shipping, quote, email, recommendation, ad, accounting, fraud-detection, image-provider, kafka]
tags: [triage, first steps, dependencies, call graph, cascade, root cause, symptom]
owner: sre
last_reviewed: 2026-10-15
---
# Triage: telling the symptom from the cause

## Overview
The shop is a chain of services. A failure deep in the chain shows up as errors or
slowness in every service above it, so the service that alerts first is usually not
the one that broke. Use this runbook at the start of every incident, before opening a
service-specific runbook.

## Who calls whom
- `frontend-proxy` (Envoy) receives all user traffic. It routes pages and API calls to
  `frontend` (Next.js) and product images to `image-provider` (nginx).
- `frontend` calls `ad`, `cart`, `checkout`, `currency`, `product-catalog`,
  `recommendation` and `shipping`.
- `checkout` places an order by calling, in this order: `cart` (get the cart),
  `product-catalog` (each item), `currency` (convert prices), `shipping` (quote),
  `payment` (charge the card), `shipping` (ship), `cart` (empty it) and `email`
  (confirmation). It then publishes the order to `kafka` (topic `orders`).
- `shipping` calls `quote`. `recommendation` calls `product-catalog`. `cart` keeps carts
  in `valkey-cart`. `product-catalog` reads the `astronomy-db` Postgres database.
- `accounting` and `fraud-detection` consume the `orders` topic. They are not on the
  user's request path, so their problems show up as lag, not as user-facing errors.
- `load-generator` produces synthetic shopping traffic. It is a client: it shows the
  errors of the services it calls, and it can cause a traffic spike.

## Method
1. Overview first: `get_service_health` lists traffic, error ratio and p95 latency for
   every service. Write down every service with an observation.
2. Errors: take the most user-facing failing service (usually `frontend` or `checkout`)
   and pull its failed traces with `get_traces(service=..., errors_only=True)`. The
   `error_origins` summary names the deepest failing span. That service is the prime
   suspect.
3. Latency: pull slow traces (`min_duration_ms` about twice the normal p95) and read the
   spans with the largest self time. A caller that only waits for a slow dependency has
   little self time of its own.
4. Confirm inside the suspect: per-operation numbers with `get_service_metrics` and its
   logs with `search_logs(level="warn")`. Several services log failures as warnings, so
   searching for `error` alone can miss them.
5. Check the timing: the cause starts at or before its symptoms.

## Rules of thumb
- Two services with the same error ratio, where one calls the other: the one being
  called is usually the cause.
- A service with no traffic while its callers fail is down or unreachable. It is not
  healthy just because it reports no errors.
- Errors limited to one product, one route or one operation point to bad data or a
  narrow bug, not to an outage of the whole service.
- Memory that keeps growing, with latency rising in the same service, points to a
  resource problem (a leak) in that service.
- Many more requests than usual, with no single failing dependency, is a traffic problem.
- If nothing stands out beyond the known background noise, it may be a false alarm (see
  the known-noise runbook).

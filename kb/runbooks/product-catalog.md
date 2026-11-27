---
services: [product-catalog]
tags: [products, catalog, product page, product id, database, astronomy-db, postgres, lock, slow query]
owner: catalog-team
last_reviewed: 2026-10-15
---
# Product catalog: product lookups failing or slow

## Overview
`product-catalog` (Go) serves `ListProducts`, `GetProduct` and `SearchProducts`. It reads
from the `astronomy-db` Postgres database (client spans named `astronomy-db`). It is
called by `frontend` (product pages), `checkout` (every item of an order) and
`recommendation`.

## Symptoms
- `GET /api/products/[productId]/index` errors in `frontend`, and
  `oteldemo.ProductCatalogService/GetProduct` errors in its callers.
- Checkout failures for orders that contain an affected product. Recommendation errors
  or latency.
- p95 latency of `GetProduct` or `ListProducts` rising.

## Diagnosis
1. `get_service_metrics(service="product-catalog")`: which operation fails or is slow.
2. Errors: `get_traces(service="product-catalog", errors_only=True)`. Check whether the
   failures concern one product: look at the product ID on the failing spans (attribute
   `demo.product.id`) and at the URLs of the failing frontend routes. If one product
   fails and all others work, the cause is bad data or a bug for that item, not a
   service-wide outage.
3. Latency: take slow traces (`min_duration_ms`) and look at their `astronomy-db` spans.
   If most of the time is spent waiting on the database, look at the database: active
   connections (`postgresql_backends`), deadlocks (`postgresql_deadlocks_total`), and
   commit and rollback rates.
4. Logs: `search_logs(service="product-catalog", level="warn")`.

## Common causes
- Bad catalog data or a bug for one product (errors limited to one product ID).
- Database lock contention or long-running transactions: latency in `astronomy-db`
  spans and more connections waiting.
- Database unavailable: every query fails, with connection errors in the logs.
- A bad product-catalog release.

## Remediation
- One bad product: roll back the most recent catalog data or configuration change. As a
  stop-gap, hide the product.
- Lock contention: find and stop the blocking transaction or batch job, then roll back
  the change that introduced it.
- Database down: restore the database. product-catalog recovers by itself.

## Verification
- `GetProduct` error ratio about 0 % for every product, p95 back to its baseline, and
  `astronomy-db` span durations back to normal.

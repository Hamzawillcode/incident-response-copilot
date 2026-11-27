---
services: [frontend, frontend-proxy, image-provider]
tags: [website, web app, api route, http 500, 5xx, page load, images, envoy, proxy, nginx]
owner: web-team
last_reviewed: 2026-10-15
---
# Frontend, proxy and images: errors and slow pages

## Overview
Users reach the shop through `frontend-proxy` (Envoy). It routes pages and API calls to
`frontend` (Next.js) and product images (`/images/...`) to `image-provider` (nginx).
Every user request passes through these services, so they show almost every incident as
a symptom. Treat them as the root cause only after ruling out their dependencies.

## Symptoms
- HTTP 5xx from API routes such as `POST /api/checkout`,
  `GET /api/products/[productId]/index`, `POST /api/cart` or `GET /api/recommendations`.
- Pages or images loading slowly, and `frontend-proxy` p95 latency going up.
- `router frontend egress` spans in `frontend-proxy` marked as errors.

## Diagnosis
1. Find the failing or slow API route: `get_service_metrics(service="frontend")` lists
   the operations.
2. Pull failed frontend traces and read `error_origins`. If the origin is a downstream
   span (for example in `checkout`, `cart` or `product-catalog`), open that service's
   runbook instead.
3. If the origins stay inside `frontend` (an exception in an API route with no failing
   child call), read the frontend logs with `level="warn"` for the exception message.
4. Slow images: compare the proxy's time for image requests with `image-provider`'s own
   time (nginx metrics such as `nginx_requests_total`, and the image spans). If the proxy
   is slow while nginx answers quickly, the delay is added in the proxy layer (a route,
   delay or rate-limit setting, or connection limits), not in image-provider.
5. Slow pages: check whether the time is spent in a dependency (large self time in a
   downstream span) or in the frontend itself (CPU or memory, for example
   `container_memory_usage_total_bytes{container_name="frontend"}`).

## Common causes
- A failing dependency (the most common case). The frontend only passes the error on.
- A bad frontend release: errors on many routes at once, right after a deployment, with
  origins inside the frontend.
- A proxy configuration change: delays or errors on one route prefix only (for example
  only `/images`).
- Overload: see the traffic-spike runbook.

## Remediation
- Dependency failure: follow the dependency's runbook. Do not change the frontend.
- Bad frontend release or proxy configuration: roll back the last deployment or
  configuration change of that component.
- Image delays added by the proxy: revert the proxy route or filter configuration for
  `/images`. Restart `frontend-proxy` only if reloading the configuration does not help.

## Verification
- API routes back to their usual error ratio (under 1 %) and p95 latency.
- Image requests back to normal latency in the proxy spans.

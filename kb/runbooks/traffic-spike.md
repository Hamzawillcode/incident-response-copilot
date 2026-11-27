---
services: [frontend-proxy, frontend, load-generator]
tags: [traffic spike, flood, overload, capacity, requests per second, bot, homepage, surge, rate limit]
owner: sre
last_reviewed: 2026-10-15
---
# Traffic spike: a sudden flood of requests

## Overview
A sudden jump in requests can slow down or break the shop even though no code is
broken. The fix is about capacity and the source of the traffic, not about a bug.

## Symptoms
- Request rate of `frontend-proxy` and `frontend` at twice the window start or more (the
  health overview reports the traffic change).
- Latency rising in `frontend` and in the services it calls for that page. There may be
  503s or timeouts from the proxy.
- Usually concentrated on one route, for example the home page or the product list.

## Diagnosis
1. Confirm the extra traffic: traffic observations in `get_service_health`, then
   per-route numbers with `get_service_metrics(service="frontend")`.
2. Find the route whose request rate grew the most.
3. Find the source. Synthetic users come from `load-generator`: look at its traces and
   request rate (`get_traces(service="load-generator")`). A real-user surge has no
   matching rise there.
4. Check that errors are a consequence of the load (timeouts, queueing, CPU or memory
   near their limits) and not of one failing dependency. Failing traces then show
   timeouts on busy services rather than one broken service being called.

## Common causes
- Synthetic or bot traffic flooding one page.
- A real surge of users (a campaign or event).
- A retry storm: clients retrying after an earlier failure multiply the traffic.

## Remediation
- Stop or rate-limit the source: block the bot, throttle the load generator, or add a
  per-route rate limit at the proxy.
- Scale out `frontend` (more replicas) when the traffic is legitimate.
- Reduce client retries when a retry storm is involved.

## Verification
- Request rate back to its usual level, or the higher rate handled with normal latency
  after scaling. Error ratio under 1 %.

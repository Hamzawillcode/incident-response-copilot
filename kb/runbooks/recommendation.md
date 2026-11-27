---
services: [recommendation]
tags: [recommendations, cache, memory leak, memory growth, python, out of memory, latency]
owner: discovery-team
last_reviewed: 2026-10-15
---
# Recommendation service: slow recommendations and memory growth

## Overview
`recommendation` (Python) serves `oteldemo.RecommendationService/ListRecommendations` for
`frontend` (`GET /api/recommendations`). It fetches products from `product-catalog`
(`ListProducts`, internal span `get_product_list`) and keeps a cache of product IDs.
Recommendations are not on the checkout path: a problem here makes pages worse but does
not stop orders.

## Symptoms
- `ListRecommendations` p95 latency rising steadily over minutes, not as a single spike.
- Memory of the recommendation process growing without levelling off.
- Eventually errors or restarts (out of memory), seen as gaps in traffic.

## Diagnosis
1. `get_service_metrics(service="recommendation")`: latency and errors per operation.
2. Memory trend:
   `query_metrics('process_runtime_cpython_memory_bytes{service_name="recommendation"}', 30)`
   or the container metric
   `container_memory_usage_total_bytes{container_name="recommendation"}`. A steady
   `increasing` trend is the key sign.
3. Slow traces: is the time spent inside recommendation (large self time in its own
   spans, for example `get_product_list`) or in `product-catalog`? If product-catalog is
   slow too, start there.
4. Logs: `search_logs(service="recommendation", text="cache")`. A cache that keeps
   missing, or keeps growing while hits stay low, is suspicious.

## Common causes
- A cache that grows without a bound (it keeps adding entries, or keeps missing and
  refetching): memory leak plus rising latency.
- A slow `product-catalog`: latency inherited, memory normal.
- A bad recommendation release.

## Remediation
- Memory leak: restart the recommendation service to recover right away (memory will
  grow again), then roll back the change that broke the cache and bound its size.
- Inherited latency: follow the product-catalog runbook.

## Verification
- Memory stable for at least 15 minutes after the fix, and `ListRecommendations` p95
  back to its baseline.

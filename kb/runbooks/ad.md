---
services: [ad]
tags: [ads, advertising, java, jvm, cpu, garbage collection, gc pause, high cpu]
owner: discovery-team
last_reviewed: 2026-10-15
---
# Ad service: failing ads, high CPU and GC pauses

## Overview
`ad` (Java) serves `oteldemo.AdService/GetAds` to `frontend`. Ads are optional: when the
ad service fails, pages still load without ads, so the user impact is small. Its runtime
metrics are scraped from the service (job `ad`), and container metrics are also
available.

## Symptoms
- `GetAds` errors, and frontend warnings that ads could not be loaded.
- `GetAds` latency rising, with spiky p95 values.
- High CPU in the ad container, or long garbage-collection pauses.

## Diagnosis
1. `get_service_metrics(service="ad")`: error ratio and latency of `GetAds`.
2. Errors: `get_traces(service="ad", errors_only=True)` and
   `search_logs(service="ad", level="warn")` for the exception.
3. Latency with high CPU: check the container CPU
   (`container_cpu_utilization_ratio{container_name="ad"}`). Rising CPU with rising
   latency and no slow dependency means the service is busy with its own work.
4. Latency spikes with normal CPU: look for garbage-collection pauses in the JVM metrics
   of job `ad` and in the ad logs. Repeated full collections freeze the service for
   hundreds of milliseconds at a time.

## Common causes
- A bad ad release or configuration: errors in `GetAds`.
- A CPU-heavy code path or a busy loop: high CPU and latency.
- Heap pressure or explicit full garbage collections: periodic pauses and latency spikes.

## Remediation
- Errors after a change: roll back the ad deployment or configuration change.
- High CPU or GC pauses: roll back the change that introduced them. Restarting `ad`
  helps only for a short time if the cause is still active.
- Because ads are optional, you can turn the ad call off in the frontend while you
  investigate, if needed.

## Verification
- `GetAds` error ratio about 0 %, CPU back to its usual level, p95 latency stable.

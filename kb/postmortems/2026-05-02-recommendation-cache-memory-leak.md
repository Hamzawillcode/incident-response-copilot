---
incident_id: INC-2026-0502
date: 2026-05-02
severity: SEV-3
services: [recommendation, frontend]
root_cause_service: recommendation
failure_type: resource
duration_minutes: 95
---
# Postmortem: recommendation memory leak from an unbounded cache

## Summary
After a release of the recommendation service, its product cache stopped evicting
entries and kept growing. Memory rose steadily for more than an hour and recommendation
latency climbed with it, until the process was restarted and the release rolled back.

## Impact
- Product pages loaded more slowly: recommendation p95 went from about 30 ms to more
  than 1 s.
- No failed orders, because recommendations are not on the checkout path.

## Timeline (UTC)
- 09:10 Recommendation release deployed.
- 09:40 Latency alert on frontend p95. Nothing was obviously failing.
- 10:05 Recommendation identified as the slow span in page traces. Its memory had grown
  in a straight line since 09:10.
- 10:20 Logs showed the cache missing on almost every request while the cache kept
  growing.
- 10:30 Service restarted. Latency recovered, but memory started growing again.
- 10:45 Release rolled back. Memory flat afterwards.

## Root cause
A change in the cache logic appended product IDs on every miss without removing old
ones, and lookups almost never hit. The cache grew with every request, so each request
had more data to process (latency) and the process held more and more memory (resource
exhaustion).

## Detection and diagnosis
- The first signal was latency on the frontend, which was only a symptom.
- Traces showed the self time inside recommendation's own spans, not in product-catalog.
- The memory trend of the recommendation process was the clearest evidence: increasing
  steadily over the whole window.

## Resolution
A restart gave immediate relief. Rolling back the release fixed it.

## Lessons and action items
- Memory growth plus rising latency in the same service points to a leak in that service.
- A restart is a stop-gap: memory grows again until the cause is removed.
- Add a size bound and a hit-ratio metric to the cache. Alert on steady memory growth.

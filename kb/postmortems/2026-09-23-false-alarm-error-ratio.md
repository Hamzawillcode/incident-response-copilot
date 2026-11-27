---
incident_id: INC-2026-0923
date: 2026-09-23
severity: none
services: [load-generator, fraud-detection]
root_cause_service: none
failure_type: none
duration_minutes: 0
---
# Postmortem: error-ratio alert with no real incident

## Summary
An error-ratio alert fired at night. The investigation found no user impact. The errors
came from the load generator's calls to a chat-assistant endpoint that is not deployed,
plus a low-traffic service whose error ratio jumped on a handful of requests. The alert
was closed as a false alarm, and the alert rule was tightened.

## Impact
None. All user-facing services had normal error ratios and latency.

## Timeline (UTC)
- 02:10 Alert fired: error ratio above 5 % for 2 minutes.
- 02:14 Health overview: frontend, checkout, payment, cart and product-catalog normal.
- 02:18 All failing traces came from `load-generator` calls to `/prompt`
  (`user_ask_agent`), failing with DNS errors as they do every night.
- 02:22 fraud-detection had one failed message out of four in the window: a 25 % error
  ratio on almost no traffic.
- 02:25 Closed as a false alarm.

## Root cause
There was no incident. Background noise (synthetic traffic to an endpoint that is not
deployed, and an error ratio computed on very low traffic) crossed the alert threshold.

## Detection and diagnosis
- The right move was to check the user-facing services first and to read the error
  origins. Nothing pointed to a real failure.
- An earlier, similar alert had been escalated by mistake, because a high error ratio on
  very low traffic looked alarming.

## Resolution
Nothing needed fixing. The alert now requires a minimum request rate before the error
ratio counts.

## Lessons and action items
- "No incident" is a valid conclusion. Do not invent a root cause.
- Read request rates next to error ratios.
- Keep the list of known background noise up to date (see the known-noise runbook).

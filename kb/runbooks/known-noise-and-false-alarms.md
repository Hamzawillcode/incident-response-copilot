---
services: [load-generator, fraud-detection]
tags: [false alarm, noise, baseline, synthetic traffic, no incident, flapping alert, low traffic]
owner: sre
last_reviewed: 2026-10-15
---
# Known noise and false alarms

## Overview
Some errors and latency changes are always present in this environment and do not hurt
real users. Check this list before declaring an incident. When the evidence shows only
this noise, the right answer is "no incident".

## Background errors that are expected
- `load-generator` has a scripted task that calls a chat-assistant endpoint (`/prompt`,
  spans named like `user_ask_agent`). The assistant is not deployed in this environment,
  so these calls always fail with DNS or connection errors: roughly 6 % of the load
  generator's requests fail at baseline. This is synthetic traffic failing on the client
  side, not a service outage.
- Services with very little traffic have jumpy error ratios. At one request a minute, a
  single failure is a 100 % error ratio. Always read the request rate next to the error
  ratio.
- `fraud-detection` handles only a few messages a minute, so its p95 latency moves a lot
  without meaning anything.

## Signs of a false alarm
- The alerting service's error ratio or latency is back at its window-start value, or
  was above the threshold only briefly.
- Failed traces only show the load generator's chat-assistant calls, or `error_origins`
  is empty.
- Warning logs show the usual patterns with steady counts and nothing new in the alert
  window.
- User-facing services (`frontend`, `checkout`) have normal error ratios and latency.

## What to do
1. Compare the alert window with the start of the investigation window. A real incident
   persists.
2. Check the origins of the failing traces. If they are all load-generator calls to the
   missing assistant, it is noise.
3. Report "no incident" with the evidence (normal error ratios, no new log patterns). Do
   not invent a root cause.
4. If the same false alarm repeats, propose tuning the alert (a higher minimum request
   rate or a longer window).

## Not noise
- An error ratio above about 5 % on `checkout`, `payment`, `cart` or `product-catalog`
  that lasts more than two minutes.
- New log patterns that start inside the alert window.
- A service whose traffic drops to zero.

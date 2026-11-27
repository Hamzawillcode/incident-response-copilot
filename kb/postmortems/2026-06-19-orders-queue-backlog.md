---
incident_id: INC-2026-0619
date: 2026-06-19
severity: SEV-3
services: [kafka, fraud-detection, accounting, checkout]
root_cause_service: kafka
failure_type: queue_lag
duration_minutes: 70
---
# Postmortem: order messages piling up in kafka

## Summary
A batch job republished a large number of old orders to the `orders` topic while the
fraud-detection consumer was slowed down by a slow downstream lookup. The consumers fell
far behind, and fraud checks completed up to 40 minutes late. There were no user-facing
errors.

## Impact
- Fraud checks and accounting records delayed by up to 40 minutes.
- Checkout and the website were not affected.

## Timeline (UTC)
- 16:00 A replay batch job started publishing old orders.
- 16:08 fraud-detection consumer spans slowed down (p95 from tens of milliseconds to
  seconds).
- 16:25 The data team noticed that accounting reports were behind. An alert on consumer
  latency fired.
- 16:40 The producer burst was identified (publish rate many times its usual level) and
  the job stopped.
- 17:10 Backlog drained, consumer latency normal.

## Root cause
Two things happened at once: a burst of messages from the producer side (the replay
job) and a consumer that processed each message more slowly than usual. The queue
absorbed the difference as lag. No single service was broken: the messaging pipeline
was overloaded.

## Detection and diagnosis
- There was no error to follow, so the error-ratio alerts stayed quiet.
- The useful signals were the consumer span durations in fraud-detection and accounting,
  and the producer rate of the `publish orders` operation in checkout.
- No dedicated lag metric was available, which delayed detection.

## Resolution
The replay job was stopped and the consumers caught up. The slow lookup in
fraud-detection was fixed afterwards.

## Lessons and action items
- Queue lag shows up as consumer latency and producer rate, not as errors.
- Classify such incidents as queue lag in the messaging pipeline, not as a failure of
  one consumer.
- Export consumer lag as a metric and alert on it.

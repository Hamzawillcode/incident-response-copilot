---
services: [kafka, accounting, fraud-detection, checkout]
tags: [kafka, queue, consumer lag, backlog, orders topic, messaging, producer, consumer, slow consumer]
owner: data-team
last_reviewed: 2026-10-15
---
# Kafka: consumer lag on the orders topic

## Overview
After a successful order, `checkout` publishes a message to the `orders` topic in
`kafka` (producer span `publish orders`). Two consumers read it: `accounting` (.NET,
records the order) and `fraud-detection` (Kotlin, checks the order). They are not on the
user's request path, so a queue problem rarely causes user-facing errors. It shows up as
lag: messages waiting longer and longer before they are processed.

## Symptoms
- Consumer processing time rising in `accounting` or `fraud-detection` (their consumer
  spans get slower; the health overview shows their p95 latency going up).
- The producer rate (`publish orders` in checkout) far above its usual level, or the
  consumers processing fewer messages than are published.
- Possibly slower `publish orders` spans in checkout when the broker is overloaded.

## Diagnosis
1. `get_service_health`: compare `accounting` and `fraud-detection` latency and traffic
   with the window start. Consumer spans are counted as the work these services receive.
2. Producer side: `get_service_metrics(service="checkout")` and look at the
   `publish orders` operation (rate and latency). A burst of published messages can
   create a backlog even when consumers are healthy.
3. Consumer side: `get_traces(service="fraud-detection")` and
   `get_traces(service="accounting")`. Long consumer spans with large self time mean the
   consumer itself is slow.
4. Logs: `search_logs(service="fraud-detection", level="warn")` and the same for
   `accounting`.
5. A dedicated lag metric may not be exported. Do not conclude that there is no lag just
   because a lag query returns no data.

## Common causes
- A burst of messages from the producer (overload) together with consumers that cannot
  keep up.
- A slow consumer: a slow downstream call, a slow database write or an artificial delay
  in processing.
- Broker problems: a broker restart or disk or memory pressure.

## Remediation
- Stop or slow the source of the burst, and roll back the change that caused it.
- Fix or roll back the slow consumer. Add consumer instances if the volume is legitimate.
- Watch the backlog drain. Lag recovers gradually, not instantly.

## Verification
- Consumer span durations back to normal, consumer throughput at least equal to the
  producer rate, and the backlog shrinking.

---
services: [email]
tags: [email, order confirmation, ruby, memory leak, memory growth, out of memory, restart]
owner: orders-team
last_reviewed: 2026-10-15
---
# Email service: confirmation emails slow or failing, memory growth

## Overview
`email` (Ruby, Sinatra) sends order confirmations. `checkout` calls it at the end of
every order (`POST /send_order_confirmation`, internal spans `send_email` and
`sinatra.render_template`). Order confirmations are not critical for taking money, but
failures here can still fail or slow the checkout call.

## Symptoms
- Memory of the email container growing steadily during the window.
- Email latency rising, then errors or restarts when memory runs out.
- Checkout traces slow or failing in the `send_order_confirmation` call.

## Diagnosis
1. `get_service_metrics(service="email")`: latency and errors of the confirmation
   endpoint.
2. Memory trend:
   `query_metrics('container_memory_usage_total_bytes{container_name="email"}', 30)`. A
   steady `increasing` trend is the key sign of a leak.
3. Slow checkout traces: is the extra time in the email spans?
4. Logs: `search_logs(service="email", level="warn")`.

## Common causes
- A memory leak in the email service: objects kept for every email sent, for example
  in a cache or a template buffer.
- A slow mail provider: high latency, normal memory.
- A bad email release.

## Remediation
- Memory leak: restart `email` to recover right away (memory will grow again), then roll
  back the change that introduced the leak.
- Slow provider: queue the emails and send them asynchronously, so checkout does not wait.

## Verification
- Memory flat for at least 15 minutes after the fix, and email latency and error ratio
  back to normal.

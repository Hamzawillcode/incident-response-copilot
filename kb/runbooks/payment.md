---
services: [payment]
tags: [charge, card, credit card, transaction, billing, payment failed, unreachable, provider]
owner: payments-team
last_reviewed: 2026-10-15
---
# Payment service: charge failures and outages

## Overview
`payment` (Node.js) handles `oteldemo.PaymentService/Charge` for `checkout`. The real
work happens in its internal `charge` span. When payment fails, users cannot complete
orders, and the errors also appear in `checkout` and `frontend`, often with the same
error ratio.

## Symptoms
- `payment` error ratio up, with `checkout` and `frontend` (`POST /api/checkout`) error
  ratios rising at the same time.
- Failed checkout traces whose `error_origins` is `payment :: charge` (payment rejected
  the charge) or checkout's client span to `oteldemo.PaymentService/Charge` (payment
  could not be reached).
- Payment logs its failures as warnings, for example "Payment request failed ...".
  Search with `level="warn"`, not `level="error"`.

## Diagnosis
1. Read the error origins of failed checkout traces:
   `get_traces(service="checkout", errors_only=True)`.
2. Confirm inside payment: `get_service_metrics(service="payment")` should show
   `oteldemo.PaymentService/Charge` with a high error ratio.
3. Read the payment logs: `search_logs(service="payment", level="warn")`. Note the
   message pattern and its first-seen time. A steady fraction of charges failing points
   to a bad configuration or a partial provider outage rather than a crash.
4. Tell failing apart from unreachable:
   - failing: payment receives requests (normal traffic) and returns errors;
   - unreachable: payment has little or no incoming traffic, while checkout's calls fail
     with connection, DNS or UNAVAILABLE errors.
5. Check the timing: payment errors should start at or before the checkout errors.

## Common causes
- A bad payment deployment or configuration change: charges rejected with a provider or
  validation error.
- The payment provider is degraded: errors carry provider messages and are often partial.
- Payment is down or unreachable: crashed process, wrong service address in the caller,
  or a network or DNS problem.

## Remediation
- Charges failing after a change: roll back the most recent payment deployment or
  configuration change.
- Unreachable: restart payment, check that the container is running, and check that
  callers use the correct address.
- Provider outage: fail over to the backup provider if there is one, and tell customers.

## Verification
- `oteldemo.PaymentService/Charge` error ratio back to about 0 %, no new
  "Payment request failed" warnings, and the checkout error ratio back to normal.

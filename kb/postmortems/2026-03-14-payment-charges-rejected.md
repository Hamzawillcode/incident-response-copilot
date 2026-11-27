---
incident_id: INC-2026-0314
date: 2026-03-14
severity: SEV-2
services: [payment, checkout, frontend]
root_cause_service: payment
failure_type: errors
duration_minutes: 47
---
# Postmortem: card payments rejected after a payment configuration change

## Summary
For 47 minutes, roughly a third of all orders failed at the payment step. A change to
the payment service's provider settings made the provider reject part of the charge
requests. The alert fired on checkout, which at first looked like the broken service.

## Impact
- About a third of checkout attempts failed with "something went wrong". Users who
  retried usually succeeded.
- No double charges: the failed charges were rejected before any money moved.

## Timeline (UTC)
- 14:02 Payment configuration change rolled out.
- 14:05 Checkout error ratio above 5 %. The checkout alert fired at 14:07.
- 14:12 On-call started with checkout: no errors in checkout's own code, but every failed
  order ended in the payment charge call.
- 14:21 Payment logs showed "Payment request failed" warnings starting at 14:03, for
  about a third of the charges.
- 14:38 The configuration change was identified as the trigger and rolled back.
- 14:49 Error ratios back to baseline. Incident closed.

## Root cause
The new payment configuration made the provider reject part of the charge requests.
Payment returned an error for those charges, and checkout, which calls payment, failed
the order. Payment and checkout therefore showed the same error ratio: checkout was the
symptom and payment the cause.

## Detection and diagnosis
- Detected by the checkout error-ratio alert, not by a payment alert.
- The first nine minutes went into checkout's code. The failed traces already showed the
  error origin in payment's `charge` span.
- Payment logs its failures as warnings, so the first search for error-level logs found
  nothing.

## Resolution
The payment configuration change was rolled back. No restart was needed.

## Lessons and action items
- Start from the error origins of failed traces, not from the service that alerted.
- Search payment logs at warning level.
- Add an alert on the payment charge error ratio itself.
- Require a canary rollout for payment configuration changes.

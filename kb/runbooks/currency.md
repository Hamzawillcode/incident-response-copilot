---
services: [currency]
tags: [currency, conversion, exchange rate, prices, c++]
owner: orders-team
last_reviewed: 2026-10-15
---
# Currency service: price conversion failing or slow

## Overview
`currency` (C++) serves `oteldemo.CurrencyService/Convert` and `GetSupportedCurrencies`.
`frontend` calls it to show prices in the user's currency, and `checkout` calls it for
every order. It is one of the busiest services.

## Symptoms
- `Convert` errors in checkout or frontend traces.
- Prices missing or wrong on pages.
- Currency latency rising, which slows every page that shows prices.

## Diagnosis
1. `get_service_metrics(service="currency")`: error ratio and latency of `Convert`.
2. `get_traces(service="checkout", errors_only=True)`: is
   `oteldemo.CurrencyService/Convert` an error origin?
3. Logs: `search_logs(service="currency", level="warn")`. The currency service writes
   its own timestamps incorrectly (some records show 1970), so judge timing by when the
   logs were received, not by the timestamp printed in the message.

## Common causes
- A bad currency release or a broken exchange-rate data file.
- An unsupported currency code sent by a caller (errors for one currency only).
- CPU saturation at high traffic.

## Remediation
- Bad release or data: roll back the deployment or the rate data.
- Unsupported currency: fix the caller, or add the currency.
- Saturation: scale out currency.

## Verification
- `Convert` error ratio about 0 %, latency normal, prices displayed again.

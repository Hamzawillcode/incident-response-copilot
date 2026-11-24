import json, sys
from copilot.tools import query_metrics, search_logs, get_traces

def show(name, result):
    text = json.dumps(result, indent=2)
    print(f"\n===== {name}  ({len(text)/1024:.1f} KB, status={result['status']}) =====")
    print(text[:3000])           # first 3 KB is enough to read
    for bad in ("feature_flag", "flagd", "paymentFailure", "productCatalogFailure"):
        if bad in text:
            print(f"!!! LEAK: '{bad}' found in output")

show("error rate by service", query_metrics(
    'sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[2m]))', 15))
show("error logs", search_logs(level="error", minutes=10))
show("failed checkout traces", get_traces(service="checkout", errors_only=True, minutes=10, limit=5))
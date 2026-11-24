#!/usr/bin/env bash
# Start the OpenTelemetry Demo and apply the copilot overlay (deploy/compose.copilot.yaml).
#
#   scripts/start_demo.sh            # make start + overlay
#   scripts/start_demo.sh --overlay  # only (re)apply the overlay to a running demo
#
# `make start` recreates every container from the upstream compose files, which
# publishes OpenSearch/Jaeger on random 0.0.0.0 ports again; the overlay then
# replaces those with 127.0.0.1:9200 and 127.0.0.1:16686.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/opentelemetry-demo"

if [[ "${1:-}" != "--overlay" ]]; then
  make start
fi

docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f compose.extras.yaml \
  -f "$ROOT/deploy/compose.copilot.yaml" up -d --no-deps opensearch jaeger

for _ in $(seq 1 40); do
  if curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:9200/_cluster/health | grep -q 200 &&
     curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:16686/jaeger/ui/api/services | grep -q 200; then
    echo "OpenSearch: http://localhost:9200   Jaeger: http://localhost:16686/jaeger/ui   Prometheus: http://localhost:9090"
    exit 0
  fi
  sleep 3
done
echo "Backends did not become ready in time" >&2
exit 1

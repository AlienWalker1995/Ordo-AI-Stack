#!/usr/bin/env bash
# Validate the monitoring configs with the exact tools the stack runs: promtool from the Prometheus
# image and amtool from the Alertmanager image that services/monitoring/plugin.yaml pins (read from
# the manifest, so the check cannot drift from what deploys). Run by CI's `monitoring-config` job;
# safe to run anywhere with docker, from the repo root. It starts only throwaway `--rm` containers.
#
#   promtool check config  monitoring/prometheus/prometheus.yml (and the rule files it loads)
#   promtool check rules   monitoring/prometheus/rules/*.yml
#   promtool test rules    monitoring/prometheus/tests/*.test.yml (each alert against replayed series)
#   amtool check-config    monitoring/alertmanager/alertmanager.yml
#   loki -verify-config    monitoring/loki/loki.yaml
#   alloy fmt              monitoring/alloy/config.alloy (a syntax check: it fails on a parse error)
set -euo pipefail

MANIFEST=services/monitoring/plugin.yaml
MONITORING="$(pwd)/monitoring"
# Git Bash on Windows: hand docker a Windows host path, and stop MSYS rewriting the container paths.
if command -v cygpath >/dev/null 2>&1; then
  MONITORING="$(cygpath -w "$MONITORING")"
  export MSYS_NO_PATHCONV=1
fi

image_of() {
  python3 - "$MANIFEST" "$1" <<'PY'
import sys
import yaml
manifest, name = sys.argv[1], sys.argv[2]
services = yaml.safe_load(open(manifest, encoding="utf-8"))["services"]
print(next(s["image"] for s in services if s["name"] == name))
PY
}

PROMETHEUS_IMAGE="$(image_of prometheus)"
ALERTMANAGER_IMAGE="$(image_of alertmanager)"
LOKI_IMAGE="$(image_of loki)"
ALLOY_IMAGE="$(image_of alloy)"

# The rendered mounts: prometheus.yml at /etc/prometheus, the rules directory beside it.
docker run --rm --entrypoint promtool \
  -v "$MONITORING/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$MONITORING/prometheus/rules:/etc/prometheus/rules:ro" \
  "$PROMETHEUS_IMAGE" check config /etc/prometheus/prometheus.yml

docker run --rm --entrypoint sh -v "$MONITORING:/monitoring:ro" "$PROMETHEUS_IMAGE" \
  -c 'promtool check rules /monitoring/prometheus/rules/*.yml'

# Test files name their rules relative to themselves (../rules/...), so run from their directory.
docker run --rm --entrypoint sh -v "$MONITORING:/monitoring:ro" -w /monitoring/prometheus/tests \
  "$PROMETHEUS_IMAGE" -c 'promtool test rules *.test.yml'

docker run --rm --entrypoint amtool \
  -v "$MONITORING/alertmanager:/etc/alertmanager:ro" \
  "$ALERTMANAGER_IMAGE" check-config /etc/alertmanager/alertmanager.yml

# Loki is distroless with no healthcheck, so a bad config would only show as a restarting container.
docker run --rm -v "$MONITORING/loki:/etc/loki:ro" "$LOKI_IMAGE" \
  -config.file=/etc/loki/loki.yaml -verify-config

docker run --rm -v "$MONITORING/alloy:/etc/alloy:ro" "$ALLOY_IMAGE" fmt /etc/alloy/config.alloy >/dev/null

echo "monitoring configs valid (prometheus: $PROMETHEUS_IMAGE, alertmanager: $ALERTMANAGER_IMAGE," \
     "loki: $LOKI_IMAGE, alloy: $ALLOY_IMAGE)"

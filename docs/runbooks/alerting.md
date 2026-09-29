# Runbook: alerting (Prometheus rules, Alertmanager, Discord, dead-man switch)

The `monitoring` plugin evaluates alert rules in Prometheus and delivers them through Alertmanager:
every alert to the operator's Discord channel, and an always-firing `Watchdog` alert to an off-box
heartbeat check, so a dead box is noticed by something that is not on the box.

```
ops-controller GET /metrics ─┐
gpu-exporter, llama.cpp,     ├─> Prometheus ── rules ──> Alertmanager ─┬─> Discord webhook   (every alert but Watchdog)
model-gateway, alertmanager ─┘  (monitoring/prometheus)                └─> heartbeat URL     (Watchdog, every minute)
                                                                                  │
                                                           off-box check: no ping for 10 min -> it alerts you
```

| file | what it is |
|---|---|
| `monitoring/prometheus/rules/ordo-alerts.yml` | the alert rules, each with the live observation behind its threshold |
| `monitoring/prometheus/tests/ordo-alerts.test.yml` | `promtool test rules` cases: each alert replayed against series that must, or must not, fire it |
| `monitoring/alertmanager/alertmanager.yml` | routing, grouping, inhibition, the two receivers |
| `ordo/control/metrics.py` | ops-controller's `GET /metrics`: GPU lease, containers, disks, the edge certificate |
| `scripts/ci/check-monitoring-config.sh` | promtool + amtool from the pinned images; CI's `monitoring-config` job runs it |

## 1. Set the two delivery secrets (one-time)

Both are URLs with a token in them, so they are secrets in the secret store, delivered to
Alertmanager as read-only files. Neither blocks a bring-up, and `ordo doctor` fails until both are set.

**Discord webhook** (`ALERTMANAGER_DISCORD_WEBHOOK_URL`): in the Discord channel that should receive
alerts, open *Edit Channel > Integrations > Webhooks > New Webhook*, name it (for example
"Ordo alerts"), and *Copy Webhook URL*.

**Heartbeat check** (`ALERTMANAGER_HEARTBEAT_URL`): create a check on an off-box service that alerts
when pings stop, for example healthchecks.io (hosted or self-hosted somewhere other than this box):

- *Period*: 1 minute (Alertmanager pings every minute while the pipeline works).
- *Grace time*: 10 minutes (an `ordo apply` that recreates Prometheus or Alertmanager pauses the
  pings for a minute or two; ten minutes keeps that from paging you).
- Connect its notifications to somewhere that does not depend on this box (email, phone push, or
  the same Discord channel: Discord is off-box).
- Copy the check's ping URL.

Then, on the host, from the repo root:

```bash
ordo secrets set ALERTMANAGER_DISCORD_WEBHOOK_URL --from-stdin   # paste, then Ctrl-D
ordo secrets set ALERTMANAGER_HEARTBEAT_URL --from-stdin
ordo recreate --reading ALERTMANAGER_DISCORD_WEBHOOK_URL ALERTMANAGER_HEARTBEAT_URL   # what `set` prints
ordo doctor --source out/ordo.yaml    # "alerting: the Discord webhook and the heartbeat URL are set"
```

While either is blank, Alertmanager still runs: every delivery to that receiver fails (logged, and
counted in `alertmanager_notifications_failed_total`), and `ordo doctor` exits 1 with a finding that
names the key and what is missing. The dashboard's Overview shows the same finding.

## 2. Check it end to end

- The heartbeat check turns green within a minute of the recreate.
- Send a test alert to Discord (it resolves by itself after 5 minutes):

  ```bash
  docker exec ordo-alertmanager-1 amtool alert add AlertPipelineTest severity=warning \
    --annotation=summary="Test alert from the runbook" --alertmanager.url=http://localhost:9093
  ```

- Stop the heartbeat on purpose once, to see the off-box check alert:
  `docker stop ordo-alertmanager-1`, wait past the grace time, then `docker start ordo-alertmanager-1`.

## 3. The alerts

Thresholds come from 15 days of the live stack's own data (2026-09-29); each rule's comment says
what was observed.

| alert | severity | fires when |
|---|---|---|
| `Watchdog` | none | always (the heartbeat; never sent to Discord) |
| `DiskUsageHigh` | warning / critical | the Docker VM disk or the host disk is over 85% for 15 min / over 95% for 5 min |
| `ContainerRestartLoop` | critical | a service restarted 2+ times in 5 min, for 10 min (a loop still going) |
| `ContainerUnhealthy` | warning | a service's healthcheck is unhealthy for 10 min |
| `GpuLeasePastTtl` | critical | a GPU lease sits past its TTL for 5 min (the lease sweep is failing) |
| `GpuResidentEvictedWithoutLease` | critical | llama.cpp (or another resident) is evicted with no lease running or queued, for 10 min |
| `GpuResidentEvictedLong` | warning | a resident has been evicted for 2 hours (longest observed render: 54 min) |
| `LlamaCppDown` | critical | llama.cpp is unscrapable for 10 min and not evicted for a lease |
| `TargetDown` | warning | any other scrape target is down for 15 min |
| `GpuTemperatureHigh` | warning / critical | a GPU at 80 C for 5 min / 88 C for 2 min (observed max: 65 C) |
| `GpuPowerAboveLimit` | warning | a GPU's 5-min average draw is over 1.05x its enforced power limit for 5 min |
| `TlsCertExpiring` | warning / critical | the edge certificate expires in under 14 / 3 days |
| `MetricsCollectorFailing` | warning | ops-controller cannot read one of its sources (docker, a disk, the certificate) for 15 min |
| `AlertRuleEvaluationFailing` | warning | a rule failed to evaluate in the last 15 min |

Routing: one Discord message per alert name (grouped), repeated every 4 hours while critical and
every 12 hours while warning, plus one when it resolves. A critical alert inhibits the warning of the
same alert on the same target (the 95% disk alert replaces the 85% one).

## 4. Silence, change, validate

- **Silence** during planned work:
  `docker exec ordo-alertmanager-1 amtool silence add alertname=DiskUsageHigh --duration=2h --comment="cleanup" --alertmanager.url=http://localhost:9093`
- **Change a threshold or add a rule**: edit the rules file, add a case to the test file, and run
  `bash scripts/ci/check-monitoring-config.sh` (CI runs it too). Prometheus reads its config and rules
  at start, so apply a change with `ordo apply`: the render labels each service with the content digest
  of the config files it bind-mounts (`ordo/render/bind_configs.py`), so an edited rule recreates
  prometheus (and `alertmanager.yml` alertmanager).

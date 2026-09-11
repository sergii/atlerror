# Atlerror end-to-end demo

The checkout-to-Stripe demo exercises the current read-only Atlerror stack in one deterministic command.

## Run

Install the validator dependencies used by the repository, then run:

```bash
python scripts/demo_checkout_stripe.py
```

By default the harness writes artifacts under:

```text
/tmp/atlerror-demo
```

Use another directory when needed:

```bash
python scripts/demo_checkout_stripe.py \
  --output-dir ./tmp/demo
```

For machine-readable console output:

```bash
python scripts/demo_checkout_stripe.py --json
```

## What the demo proves

The harness uses saved telemetry fixtures, not hand-written diagnosis output:

```text
Prometheus TCP retransmissions ----\
                                   -> runtime evidence composition
OpenTelemetry Stripe trace -------/              |
                                                  v
                                      semantic scope + freshness
                                                  |
                                                  v
                                          causal ranking
                                                  |
                                                  v
                                      recommended next probe
                                                  |
                                                  v
                                         diagnosis snapshot
                                           /             \
                                          v               v
                                        HTTP             MCP
```

Prometheus contributes:

```text
observation.network.tcp_retransmissions
```

OpenTelemetry contributes:

```text
observation.dependency.latency
observation.network.connection_timeout
```

All three resolve into the same checkout-to-Stripe semantic scope.

The expected top explanations are:

```text
observation.dependency.latency
  -> hypothesis.latency.external_dependency

observation.network.connection_timeout
  -> hypothesis.network.connection_timeout

observation.network.tcp_retransmissions
  -> hypothesis.network.packet_loss
```

TCP retransmissions still have packet corruption as an alternative. The next-probe projection therefore recommends:

```text
probe.network.inspect_tcp_integrity_errors
```

The recommendation is derived from the existing causal paths, hypothesis predictions, falsifiers, and probe catalog. The demo contains no special-case diagnostic rule.

## Artifacts

A successful run writes:

```text
/tmp/atlerror-demo/prometheus-evidence.json
/tmp/atlerror-demo/opentelemetry-evidence.json
/tmp/atlerror-demo/runtime-evidence.json
/tmp/atlerror-demo/diagnosis.json
/tmp/atlerror-demo/demo-summary.json
```

Inspect `diagnosis.json` for the full transparent causal and next-probe rankings. `demo-summary.json` is a smaller human-facing projection.

## HTTP

The one-shot harness starts an ephemeral loopback HTTP server and verifies that `/diagnosis` and `/status` read back the exact persisted snapshot.

To leave the HTTP API running after the demo:

```bash
python scripts/diagnosis_http_api.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json
```

Then read:

```text
GET http://127.0.0.1:4320/status
GET http://127.0.0.1:4320/diagnosis
```

## MCP

The one-shot harness also verifies the existing MCP resource implementation against the exact same persisted snapshot.

To attach an MCP host after the demo, run:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json
```

The resources are:

```text
atlerror://diagnosis/current
atlerror://diagnosis/status
```

## Determinism

The default `as_of` is fixed at:

```text
2026-09-11T16:31:00Z
```

This keeps the included telemetry inside its freshness window and makes repeated runs reproducible.

You can override it explicitly:

```bash
python scripts/demo_checkout_stripe.py \
  --as-of 2026-09-11T16:31:00Z
```

## Safety boundary

This demo is read-only. It recommends the next diagnostic probe but does not execute probes, expose mutation tools, or perform remediation.

Design details are documented in [RFC 0015](RFC/0015-end-to-end-demo-harness.md).

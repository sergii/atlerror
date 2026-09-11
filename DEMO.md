# Atlerror end-to-end demo

The checkout-to-Stripe demo exercises the current Atlerror stack in one deterministic command.

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

## Optional active read-only extension

The diagnosis recommends `probe.network.inspect_tcp_integrity_errors`. On a Linux host, Atlerror can now execute that specific probe through a fixed read-only executor without invoking a shell or arbitrary subprocess.

Capture a baseline:

```bash
python scripts/probe_execution.py begin \
  --incident-id incident.demo.checkout.stripe \
  --probe probe.network.inspect_tcp_integrity_errors \
  --session /tmp/atlerror-demo/tcp-integrity-probe-session.json \
  --scope-boundary boundary.application.external_dependency \
  --scope-attribute service=checkout-api \
  --scope-attribute dependency=stripe
```

Exercise the intended controlled workload outside Atlerror, then finish the session:

```bash
python scripts/probe_execution.py finish \
  --session /tmp/atlerror-demo/tcp-integrity-probe-session.json \
  --output /tmp/atlerror-demo/tcp-integrity-probe-evidence.json \
  --pretty
```

The result is standard `runtime_evidence`. An increase in Linux `Tcp.InErrs` produces `observation.network.tcp_integrity_errors=observed`; an unchanged counter produces explicit absence. A counter reset fails closed.

The integration tests compose that evidence back into the checkout-to-Stripe incident and verify the feedback loop: observed integrity errors move `hypothesis.network.packet_corruption` ahead of packet loss, and the completed integrity probe is no longer recommended.

## Safety boundary

The one-shot fixture demo remains read-only and does not execute probes automatically. The optional execution layer only supports explicitly registered `read_only` probes. The first executor reads Linux `/proc/net/snmp`; it cannot run user-supplied commands, mutate network state, execute the workload, or perform remediation. Non-read-only probes are rejected.

Design details are documented in [RFC 0015](RFC/0015-end-to-end-demo-harness.md) and [RFC 0016](RFC/0016-safe-read-only-probe-execution.md).

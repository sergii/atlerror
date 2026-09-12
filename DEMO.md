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

To attach an MCP host after the demo in resource-only mode, run:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json
```

The resources are:

```text
atlerror://diagnosis/current
atlerror://diagnosis/status
```

Resource-only mode remains the default and exposes no MCP mutation tools.

## Executor capability discovery

Before enabling active diagnostics, inspect the built-in executor registry:

```bash
python scripts/probe_execution.py capabilities --pretty
```

The capability projection reports canonical probe semantics, executor ID, platform, produced observation, registered source, current local availability, and explicit executor policy.

The current built-ins are:

```text
probe.network.inspect_tcp_integrity_errors
  -> executor.linux.proc_net_snmp.tcp_inerrs
  -> /proc/net/snmp

probe.cpu.inspect_utilization
  -> executor.linux.proc_stat.cpu_utilization
  -> /proc/stat
```

Discovery is read-only. It checks platform and registered source availability but does not run either probe.

## Optional active read-only extension

The checkout-to-Stripe diagnosis recommends `probe.network.inspect_tcp_integrity_errors`. On a Linux host, Atlerror dispatches that probe through the registered executor rather than a probe-specific branch.

Capture a baseline directly through the CLI:

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

## Second built-in executor: CPU utilization

The same two-phase runtime can execute the second registered probe:

```bash
python scripts/probe_execution.py begin \
  --incident-id incident.cpu.example \
  --probe probe.cpu.inspect_utilization \
  --session /tmp/atlerror-cpu-session.json
```

Run the workload externally, then finish:

```bash
python scripts/probe_execution.py finish \
  --session /tmp/atlerror-cpu-session.json \
  --pretty
```

This executor samples aggregate Linux CPU counters from `/proc/stat` at begin and finish. It computes utilization from cumulative idle and total deltas and emits `observation.cpu.utilization` as standard runtime evidence.

Its first built-in classification policy uses an 80% observed threshold. That threshold is executor policy, not a universal Atlerror semantic threshold. The exact utilization percentage and threshold are retained in the evidence measurement.

## Opt-in MCP probe tools

The same safe execution path can be exposed to an MCP host, but only through explicit process-level opt-in:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json \
  --enable-readonly-probe-tools \
  --runtime-evidence /tmp/atlerror-demo/runtime-evidence.json \
  --probe-session-dir /tmp/atlerror-demo/probe-sessions
```

When enabled, the server advertises exactly two tools:

```text
atlerror.probe.begin_recommended
atlerror.probe.finish
```

`atlerror.probe.begin_recommended` accepts a diagnosis target, not an arbitrary probe ID. Atlerror reads the current validated diagnosis, selects the current top next-probe recommendation, verifies that it is canonical `risk: read_only`, and dispatches it through the registered executor for the exact diagnosis scope.

For the demo target:

```text
observation.network.tcp_retransmissions
```

the selected probe is:

```text
probe.network.inspect_tcp_integrity_errors
```

The begin result returns an opaque `probe-session.<digest>` handle. Run the controlled workload outside Atlerror, then pass that handle to `atlerror.probe.finish`.

Finish emits standard probe runtime evidence, composes it into `runtime-evidence.json`, increments `evidence_revision`, recomputes `diagnosis.json`, and returns the revised top hypothesis and next probe. A positive integrity-error result therefore closes the loop:

```text
packet_loss #1
  -> begin recommended integrity probe
  -> external workload
  -> finish probe
  -> tcp_integrity_errors observed
  -> packet_corruption #1
  -> completed integrity probe suppressed
```

Finish is retry-safe. If the MCP response is lost after evidence was persisted, another finish call with the same session ID returns the existing result instead of executing the counter read again or appending duplicate evidence.

## Determinism

The default fixture `as_of` is fixed at:

```text
2026-09-11T16:31:00Z
```

This keeps the included telemetry inside its freshness window and makes repeated one-shot demo runs reproducible.

You can override it explicitly:

```bash
python scripts/demo_checkout_stripe.py \
  --as-of 2026-09-11T16:31:00Z
```

## Safety boundary

The one-shot fixture demo and default MCP mode remain read-only. Active MCP tools are absent unless `--enable-readonly-probe-tools` is supplied explicitly.

Even when enabled, the active layer supports only explicitly registered canonical `read_only` probes. The built-in registry currently contains the Linux TCP integrity and aggregate CPU-utilization executors. MCP cannot supply a command string, choose an arbitrary executor, choose an arbitrary local source path, generate traffic, execute the workload, mutate network state, or perform remediation. `low`, `state_changing`, and `high` risk probes are rejected before executor dispatch.

The direct CLI has `--source-path` only for local replay and fixtures. Such sessions are marked `source_mode=explicit_override`; MCP never exposes that parameter.

Design details are documented in [RFC 0015](RFC/0015-end-to-end-demo-harness.md), [RFC 0016](RFC/0016-safe-read-only-probe-execution.md), [RFC 0017](RFC/0017-opt-in-mcp-read-only-probe-tools.md), and [RFC 0018](RFC/0018-probe-executor-registry.md).

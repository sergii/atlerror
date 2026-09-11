# RFC 0015: End-to-end demo harness

Status: Accepted

## Summary

Atlerror now has enough independent semantic and transport layers to demonstrate the complete read-only troubleshooting loop in one reproducible scenario.

This RFC defines a deterministic checkout-to-Stripe demo harness that exercises existing adapters, multi-source runtime evidence composition, causal diagnosis, recommended next-probe ranking, persisted diagnosis snapshots, and both read-only transport projections.

The harness is intentionally orchestration, not a second implementation of any semantic behavior.

## Motivation

Before this slice, every individual layer was executable and tested, but a person evaluating Atlerror still had to assemble the following manually:

```text
Prometheus fixture
OpenTelemetry fixture
      |
      v
telemetry adapters
      |
      v
runtime evidence documents
      |
      v
multi-source composition
      |
      v
scope and freshness resolution
      |
      v
causal ranking
      |
      v
recommended next probe
      |
      v
diagnosis snapshot
      |
      +--> HTTP
      +--> MCP
```

That made the architecture sound but the product story harder to see. A strong demo needs one deterministic command that proves the layers work together without hiding any of them behind a bespoke demo-only reasoning path.

## Decision

Add `scripts/demo_checkout_stripe.py` as a thin orchestration layer.

The default command is:

```bash
python scripts/demo_checkout_stripe.py
```

The default output directory is:

```text
/tmp/atlerror-demo
```

The harness uses the existing checkout-to-Stripe fixtures and a fixed default `as_of` time so results are reproducible.

## Scenario

The demo represents a checkout service making a synchronous request to Stripe.

Prometheus contributes:

```text
observation.network.tcp_retransmissions
```

OpenTelemetry contributes:

```text
observation.dependency.latency
observation.network.connection_timeout
```

All three evidence instances resolve into the same normalized semantic scope:

```yaml
boundaries:
  - boundary.application.external_dependency
attributes:
  dependency: stripe
  service: checkout-api
```

The resulting diagnosis explains all three observations through the existing causal graph.

For TCP retransmissions, the current ambiguity remains visible:

```text
1. hypothesis.network.packet_loss
2. hypothesis.network.packet_corruption
```

The existing next-probe projection recommends:

```text
probe.network.inspect_tcp_integrity_errors
```

No demo-specific ranking rule is introduced.

## Generated artifacts

The harness writes five JSON artifacts:

```text
prometheus-evidence.json
opentelemetry-evidence.json
runtime-evidence.json
diagnosis.json
demo-summary.json
```

The first two show the source-specific adapter outputs. `runtime-evidence.json` shows the composed incident evidence without altering source provenance. `diagnosis.json` is the same validated snapshot consumed by HTTP and MCP. `demo-summary.json` is a compact human-facing projection of the current top hypotheses and next probes.

The summary is not a new semantic contract. It is a demo presentation convenience derived entirely from the diagnosis snapshot.

## Transport verification

A demo that only writes files would not prove the existing read-only transport boundaries still consume the same contract. Therefore the harness verifies both transports by default.

### HTTP

The harness starts an ephemeral loopback `ThreadingHTTPServer` using the existing `DiagnosisSnapshotReader` and HTTP handler. It reads `/diagnosis` and `/status`, then shuts the server down.

The returned diagnosis must exactly equal the persisted diagnosis snapshot and status must be `ready`.

### MCP

The harness instantiates the existing stdio MCP server implementation in-process and performs modern `resources/read` calls for:

```text
atlerror://diagnosis/current
atlerror://diagnosis/status
```

The current diagnosis must exactly equal the persisted diagnosis snapshot and status must be `ready`.

The harness does not add a new HTTP server, MCP protocol implementation, or transport-specific reasoning path.

## Determinism

The default fixture time is:

```text
2026-09-11T16:31:00Z
```

This is one minute after the metric and trace fixture timestamps and before their five-minute freshness windows expire.

For fixed inputs and `as_of`, the following are deterministic:

- source runtime evidence
- composed runtime evidence
- semantic partitioning
- causal ranking
- next-probe ranking
- diagnosis snapshot
- compact demo summary

Ephemeral HTTP ports and filesystem paths are operational details and are not embedded in the deterministic summary artifact.

## Failure semantics

The harness fails instead of weakening the demonstration if any layer is invalid.

Examples include:

- telemetry adapter validation failure
- incompatible incident IDs during composition
- runtime evidence schema or semantic-reference failure
- contradictory active evidence in one selected scope
- diagnosis snapshot validation failure
- persisted snapshot mismatch
- HTTP read-back mismatch
- MCP read-back mismatch

This keeps the demo aligned with normal Atlerror fail-closed behavior.

## CLI options

The harness supports:

```text
--output-dir PATH
--incident-id ID
--as-of TIMESTAMP
--skip-transport-checks
--json
```

`--skip-transport-checks` is intended for constrained environments. The default demo verifies both read-only transports.

## Security boundary

The demo adds no active diagnostic execution and no mutation surface.

It does not:

- execute recommended probes
- construct shell commands from diagnosis output
- expose MCP tools
- add HTTP mutation endpoints
- perform remediation
- infer capabilities from the local machine

The existing recommendation-only boundary remains intact.

## Testing

CI exercises the complete fixture path and verifies that:

- one Prometheus metric instance and two trace instances compose into one incident
- both source types remain visible
- all evidence normalizes into one external-dependency scope
- latency, connection timeout, and TCP retransmission diagnoses are present
- TCP retransmission ranking keeps packet loss first for this evidence set
- the integrity-error probe is recommended next
- HTTP serves the same diagnosis snapshot
- MCP serves the same diagnosis snapshot
- generated artifacts match the in-memory documents
- repeated fixed-time runs are deterministic

## Non-goals

This RFC does not add:

- active probe execution
- a web UI
- a persistent incident database
- live Prometheus polling in the demo harness
- remote MCP transport
- authentication or TLS
- deployment packaging
- probabilistic diagnosis
- autonomous remediation

Those can be evaluated after the read-only semantic loop is demonstrably complete.

## Result

After this RFC, Atlerror has a single-command, multi-source, end-to-end read-only demo that preserves the project's architectural rule:

```text
one semantic source -> multiple transparent projections
```

The demo proves the existing layers compose. It does not create a parallel demo architecture.

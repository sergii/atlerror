# RFC 0005: Runtime evidence instances

Status: accepted

## Summary

Atlerror's canonical knowledge describes classes of symptoms, hypotheses, observations, probes, and causal relationships. Diagnostic reasoning also needs incident-specific facts: what was actually observed, when it was observed, where it applies, and where the evidence came from.

This RFC introduces runtime evidence instances as a separate layer over the static semantic graph.

The distinction is intentional:

- `observation.network.tcp_retransmissions` is a reusable semantic concept
- an evidence instance says that retransmissions were observed for a particular incident, at a particular time, from a particular source and scope

Static knowledge remains reusable. Runtime evidence remains contextual and disposable.

## Decision

Runtime evidence bundles validate against `schema/runtime-evidence.schema.json`.

A bundle is incident-scoped and contains one or more evidence instances:

```yaml
schema_version: "0.1"
kind: runtime_evidence
incident_id: incident.network.retransmission_spike
instances:
  - id: evidence.network.tcp_retransmissions.current
    observation: observation.network.tcp_retransmissions
    state: observed
    observed_at: "2026-09-11T14:46:00Z"
    expires_at: "2026-09-11T15:01:00Z"
    confidence: high
    source:
      type: metric
      name: linux.tcp.retranssegs
    measurement:
      value: 184
      baseline: 3
      delta: 181
      unit: segments
      comparison: above_baseline
```

The evidence instance references an existing semantic observation. It does not create a new observation concept.

## Evidence state

The initial state vocabulary is deliberately small:

- `observed` means the referenced observation is currently present according to the evidence instance
- `absent` means it was explicitly checked and is currently absent or normal according to the evidence instance

Absence is evidence, not missing data. If no instance exists for an observation, Atlerror treats its runtime state as unknown.

## Time and freshness

Every instance has `observed_at`. It may also have `expires_at`.

When evidence is resolved at an `as_of` time:

- instances whose `observed_at` is later than `as_of` are future evidence and are not used
- instances whose `expires_at` is at or before `as_of` are stale and are not used
- all other instances are active

`expires_at` must be later than `observed_at`.

Freshness is explicit because a normal packet-loss probe from forty minutes ago should not override current retransmission and checksum-error telemetry.

## Provenance

Every instance declares a source with a type and name. Initial source types include metrics, logs, traces, probes, manual observations, experiments, and synthetic checks.

Source metadata answers where the runtime fact came from. It is separate from causal-edge provenance, which describes the claims and experiments supporting a reusable causal relationship.

## Scope

Evidence may reference semantic system entities and boundaries plus adapter-specific attributes.

For example, an observation may apply to the boundary between an application service and an external dependency rather than to every network path in an incident.

The first implementation validates and preserves scope but does not yet automatically match or partition rankings by scope. Incident bundles should therefore avoid mixing contradictory evidence from unrelated scopes in one ranking query.

## Measurement

An instance may carry structured measurement context such as value, baseline, delta, unit, and comparison.

Measurements improve auditability and explanation. The initial ranking policy does not turn arbitrary measurement magnitudes into numeric probabilities or weights.

## Confidence

Evidence confidence is ordinal: `low`, `moderate`, or `high`.

The value records confidence in the runtime observation instance. It is intentionally not treated as a calibrated probability and does not currently alter candidate ordering. A future ranking policy may use confidence only after its semantics are justified across concrete diagnostic cases.

## Conflict handling

If active instances in the same bundle assert both `observed` and `absent` for the same observation, resolution fails instead of silently choosing one state.

This conservative behavior prevents ranking from hiding contradictory incident evidence. Scope-aware conflict resolution can be introduced later when ranking itself becomes scope-aware.

## Ranking integration

`scripts/runtime_evidence.py` validates and resolves runtime evidence into active `observed` and `absent` observation sets.

`scripts/causal_ranking.py` can consume the same bundle directly:

```bash
python scripts/causal_ranking.py --pretty \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-corruption-chain.yaml \
  --as-of 2026-09-11T14:48:00Z
```

Active runtime states augment any explicit `--observed` and `--absent` arguments. Stale and future instances remain visible in `evidence_context` but do not affect ranking.

The ranking output preserves a compact evidence context containing the incident ID, resolution time, active instance references, and stale or future instance IDs. This gives consumers an auditable link between runtime evidence and the resulting candidate order.

## Non-goals

This RFC does not introduce:

- a telemetry database or event store
- automatic ingestion from OpenTelemetry, Prometheus, logs, or cloud vendors
- probabilistic calibration
- numeric Bayesian inference
- automatic scope matching
- time-series aggregation semantics
- automatic conflict resolution across distinct scopes

Those capabilities belong in adapters or later semantic layers once concrete consumers require them.

## Future work

Likely next steps are:

1. scope-aware evidence selection and ranking
2. adapters that produce runtime evidence bundles from metrics, traces, logs, and probes
3. persistent incident evidence stores when a real consumer needs retention
4. explicit masking and recovery semantics where repeated diagnostic cases justify them
5. thin MCP and HTTP adapters over the shared projection, ranking, and evidence contracts

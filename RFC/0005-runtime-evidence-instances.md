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
- all other instances are fresh enough for scope selection

`expires_at` must be later than `observed_at`.

Freshness is explicit because a normal packet-loss probe from forty minutes ago should not override current retransmission and checksum-error telemetry.

## Provenance

Every instance declares a source with a type and name. Initial source types include metrics, logs, traces, probes, manual observations, experiments, and synthetic checks.

Source metadata answers where the runtime fact came from. It is separate from causal-edge provenance, which describes the claims and experiments supporting a reusable causal relationship.

## Scope

Evidence may reference semantic system entities and boundaries plus adapter-specific attributes.

For example, an observation may apply to the boundary between an application service and an external dependency rather than to every network path in an incident.

Runtime evidence resolution supports an optional scope query with three selector dimensions:

- `boundaries` select evidence explicitly scoped to all requested semantic boundaries
- `entities` select evidence whose effective entity set contains all requested semantic system entities
- `attributes` require exact adapter-specific key/value matches

Boundary scope also implies the boundary's semantic `source` and `target` entities. Therefore evidence scoped only to `boundary.application.external_dependency` can satisfy an entity selector for `system_entity.external_dependency` without duplicating that entity in every runtime instance.

Scope selection is deliberately conservative. When a scope query is present, an evidence instance that does not provide enough scope information to prove applicability is excluded. Unscoped evidence is therefore not treated as a wildcard for a scoped ranking query.

A query with no scope selectors preserves incident-wide behavior and considers all active instances. This remains useful for simple bundles, but mixed-scope bundles can contain contradictory states that are meaningful only when partitioned by scope.

## Scope-aware conflict handling

Freshness is evaluated before scope matching. Contradictory states are then checked only among active instances selected for the requested scope.

This means the same observation may legitimately be `observed` on one boundary and `absent` on another boundary in the same incident bundle. A scoped query resolves each partition independently. The same mixed bundle queried without scope still fails if contradictory active states remain visible together.

The returned evidence context records:

- the normalized `scope_query`
- selected `active_instances`
- `scope_filtered_instance_ids`
- stale instance IDs
- future instance IDs

This makes scope exclusion visible rather than silently discarding evidence.

## Measurement

An instance may carry structured measurement context such as value, baseline, delta, unit, and comparison.

Measurements improve auditability and explanation. The initial ranking policy does not turn arbitrary measurement magnitudes into numeric probabilities or weights.

## Confidence

Evidence confidence is ordinal: `low`, `moderate`, or `high`.

The value records confidence in the runtime observation instance. It is intentionally not treated as a calibrated probability and does not currently alter candidate ordering. A future ranking policy may use confidence only after its semantics are justified across concrete diagnostic cases.

## Ranking integration

`scripts/runtime_evidence.py` validates and resolves runtime evidence into active `observed` and `absent` observation sets.

`scripts/causal_ranking.py` can consume the same bundle directly:

```bash
python scripts/causal_ranking.py --pretty \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-corruption-chain.yaml \
  --as-of 2026-09-11T14:48:00Z
```

A mixed-scope bundle can be restricted to one semantic path:

```bash
python scripts/causal_ranking.py --pretty \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-mixed-scopes.yaml \
  --as-of 2026-09-11T14:48:00Z \
  --scope-boundary boundary.application.external_dependency
```

The CLI also supports repeated `--scope-entity` selectors and exact `--scope-attribute KEY=VALUE` selectors. Active runtime states selected for that scope augment any explicit `--observed` and `--absent` arguments.

The ranking output preserves a compact evidence context containing the incident ID, resolution time, scope query, selected instance references, scope-filtered IDs, and stale or future instance IDs. This gives consumers an auditable link between runtime evidence and the resulting candidate order.

## Non-goals

This RFC does not introduce:

- a telemetry database or event store
- automatic ingestion from OpenTelemetry, Prometheus, logs, or cloud vendors
- probabilistic calibration
- numeric Bayesian inference
- automatic inference of the desired scope from a causal target or path
- hierarchical topology containment beyond explicit boundary endpoint expansion
- time-series aggregation semantics
- automatic reconciliation of contradictory evidence inside the same selected scope

Those capabilities belong in adapters or later semantic layers once concrete consumers require them.

## Future work

Likely next steps are:

1. adapters that produce runtime evidence bundles from metrics, traces, logs, and probes
2. richer topology identity and scope hierarchies when concrete multi-service cases require them
3. persistent incident evidence stores when a real consumer needs retention
4. evidence quality and freshness policies that remain transparent to ranking consumers
5. explicit masking and recovery semantics where repeated diagnostic cases justify them
6. thin MCP and HTTP adapters over the shared projection, ranking, and evidence contracts

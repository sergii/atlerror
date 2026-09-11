# RFC 0014: Multi-source runtime evidence composition

- Status: Accepted
- Date: 2026-09-11

## Summary

Atlerror can now compose multiple `runtime_evidence` documents for the same incident into one deterministic evidence bundle before causal diagnosis.

The first executable example combines Prometheus metric evidence with OpenTelemetry trace evidence for the same checkout-to-Stripe dependency scope.

```text
Prometheus metrics ----\
                       -> runtime evidence composition -> live diagnosis
OpenTelemetry traces --/                                -> next probe
```

Composition is intentionally a data-contract operation. It does not introduce another ranking method, infer causality from telemetry correlation, or reconcile contradictory facts.

## Motivation

Real incidents rarely have a single telemetry source. A trace can show that one downstream call was slow or failed while metrics can show retransmissions, saturation, queueing, or other surrounding behavior.

Atlerror already normalizes each telemetry family into the same runtime evidence contract. The missing operation was a conservative way to combine those normalized documents without losing provenance or semantic scope.

## Decision

Add `scripts/runtime_evidence_composition.py` with a reusable `compose_runtime_evidence()` function and CLI.

Every source document must:

- validate against `schema/runtime-evidence.schema.json`;
- reference known semantic observations, entities, and boundaries;
- use the same `incident_id` as every other source document.

The composed document remains an ordinary `runtime_evidence` document. No composition-specific wrapper is introduced.

## Identity and deduplication

Runtime evidence instance IDs remain source-adapter identities.

If two source documents contain the same instance ID with identical content, composition deduplicates the repeated instance.

If the same instance ID appears with different content, composition fails. It does not choose one source, use timestamps as an implicit overwrite policy, or generate a replacement identity.

This keeps identity collisions visible and prevents source ordering from changing the result.

## Determinism

Composition is order-independent.

Instances are sorted by stable evidence instance ID. The output description depends only on the incident ID and number of source documents, not source ordering.

For the same set of source documents, changing their input order produces the same composed runtime evidence document.

## Provenance

Composition does not rewrite evidence instances.

Each instance preserves its original:

- `source.type`;
- `source.name`;
- `source.uri`;
- source attributes;
- labels;
- observation timestamp;
- freshness boundary;
- confidence;
- measurement;
- semantic scope.

A composed bundle can therefore contain `metric` and `trace` instances while downstream diagnosis remains able to audit where each fact came from.

## Scope semantics

Composition does not merge or broaden scopes itself.

The existing live diagnosis scope normalization remains authoritative. In particular, redundant explicit entities already implied by a semantic boundary can normalize away, while exact attributes such as `service=checkout-api` and `dependency=stripe` remain part of the partition key.

This means independently produced metric and trace evidence can enter the same diagnosis partition only when their normalized semantic scopes actually match.

## Contradictions

Composition does not silently reconcile opposite states.

Two source documents may both be structurally valid while asserting contradictory active states for the same observation and diagnosis scope. The composed bundle preserves both instances. Existing runtime evidence resolution then fails closed when that scope is resolved.

This separation is intentional:

- composition answers whether evidence documents can coexist in one incident bundle;
- resolution answers whether the currently active evidence is logically consistent for a selected scope and time.

## First multi-source example

The Prometheus example maps TCP retransmission rate into:

```text
observation.network.tcp_retransmissions
```

with scope:

```yaml
boundaries:
  - boundary.application.external_dependency
attributes:
  service: checkout-api
  dependency: stripe
```

The existing OpenTelemetry trace example maps the same dependency call into:

```text
observation.dependency.latency
observation.network.connection_timeout
```

with the same normalized scope.

When the two runtime evidence documents are composed, live diagnosis sees one partition containing all three active observations.

The resulting diagnosis can simultaneously explain:

- external dependency latency from the trace;
- connection timeout from the trace;
- TCP retransmissions from the metric;
- the next discriminating network probe for packet-loss versus packet-corruption candidates.

## CLI

Two or more runtime evidence files can be composed directly:

```bash
python scripts/runtime_evidence_composition.py \
  /tmp/prometheus-evidence.yaml \
  /tmp/otel-evidence.yaml \
  --format json \
  --pretty
```

The output is still valid input to the existing runtime evidence resolver and live diagnosis engine.

## Non-goals

This RFC does not add:

- time-window joins;
- heuristic source precedence;
- confidence averaging;
- probabilistic evidence fusion;
- automatic conflict resolution;
- automatic incident discovery;
- cross-incident joins;
- semantic scope widening;
- trace correlation as causal proof;
- a durable incident store;
- a new transport service.

## Failure policy

Composition fails on:

- fewer than two source documents;
- invalid runtime evidence schema;
- unknown semantic references;
- mismatched incident IDs;
- duplicate instance IDs with different content.

It does not fail merely because two valid evidence instances may later contradict at a particular freshness boundary. That remains a resolution-time error.

## Consequences

The live diagnosis engine now has a transport-neutral path for multi-source telemetry:

```text
telemetry adapters
  -> runtime_evidence documents
  -> deterministic composition
  -> existing scope/freshness resolution
  -> existing causal ranking
  -> existing next-probe ranking
  -> HTTP / MCP projections
```

No telemetry source gains privileged ranking weight simply because of its transport or vendor.

## Next work

The next demo-focused slice should provide an end-to-end harness that:

1. produces Prometheus and OpenTelemetry evidence for one incident;
2. composes the evidence;
3. runs diagnosis and next-probe projection;
4. writes the diagnosis snapshot;
5. exposes or demonstrates the existing HTTP and MCP read-only views.

That harness should orchestrate existing contracts rather than introducing new reasoning semantics.

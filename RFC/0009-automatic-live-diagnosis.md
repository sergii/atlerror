# RFC 0009: Automatic live diagnosis

Status: accepted

## Summary

Atlerror can now receive live OTLP traces and maintain a current incident-scoped `runtime_evidence` bundle. The next layer is to keep causal diagnosis current without requiring a human or agent to invoke `causal_ranking.py` after every evidence change.

This RFC introduces a transport-independent diagnosis snapshot plus a small watcher that consumes the OTLP receiver's existing `GET /evidence` contract.

The resulting loop is:

```text
telemetry
  -> runtime evidence
  -> semantic scope partitions
  -> freshness resolution
  -> causal ranking
  -> diagnosis snapshot
```

The watcher does not create a new reasoning algorithm. It continuously reuses the same runtime-evidence resolver and deterministic ordinal causal ranking already exposed by the CLI.

## Decision

Diagnosis snapshots validate against `schema/diagnosis-snapshot.schema.json` and use:

```yaml
schema_version: "0.1"
kind: diagnosis_snapshot
incident_id: incident.checkout.live
generated_at: "2026-09-11T16:31:00Z"
as_of: "2026-09-11T16:31:00Z"
evidence_revision: 1
next_recompute_at: "2026-09-11T16:35:00.250000Z"
partitions:
  - scope:
      boundaries:
        - boundary.application.external_dependency
      attributes:
        service: checkout-api
        dependency: stripe
    observed:
      - observation.dependency.latency
      - observation.network.connection_timeout
    absent: []
    diagnoses:
      - target: observation.dependency.latency
        ranking: {...}
      - target: observation.network.connection_timeout
        ranking: {...}
    unranked_observations: []
```

Each `ranking` is the existing `causal_ranking` projection, including candidate order, causal paths, factors, reasons, and runtime evidence context.

## Separate consumer boundary

Automatic diagnosis is implemented as a consumer of runtime evidence rather than being embedded into the OTLP receiver.

This separation is deliberate:

- the receiver remains responsible for transport, telemetry translation, and live evidence aggregation
- the diagnosis layer remains responsible for semantic evidence resolution and causal ranking
- either layer can evolve without creating a second telemetry-specific reasoning path
- other evidence producers can eventually feed the same diagnosis layer

`scripts/live_diagnosis_watch.py` polls `GET /evidence` and atomically updates a diagnosis snapshot file.

The initial watcher uses polling because the receiver already has a stable HTTP read contract. Event delivery, SSE, webhooks, or an internal message bus can replace polling later without changing diagnosis semantics.

## Evidence revisions

The watcher computes a SHA-256 fingerprint over canonical JSON for the current evidence document.

When the fingerprint changes:

1. the local evidence revision increments
2. diagnosis is recomputed immediately
3. the new snapshot is atomically written

An unchanged evidence document does not cause another causal-ranking run merely because it was polled again.

The revision is local to the watcher process. It is an ordering token for diagnosis refreshes, not a globally durable incident version.

## Freshness transitions

Evidence can change meaning without the evidence document changing. An active evidence instance can expire, or a future-dated instance can become active.

The diagnosis snapshot therefore records `next_recompute_at`, calculated as the earliest future evidence activation or expiry boundary.

On each poll, the watcher asks the diagnosis engine for the current snapshot. If the next transition is due, the engine recomputes even when the evidence fingerprint is unchanged.

This keeps diagnosis consistent with RFC 0005 freshness semantics without inventing a second clock model.

The initial implementation is polling-driven. If the watcher is not running, no background process updates the snapshot at an expiry boundary.

## Scope partitions

Automatic diagnosis must not mix evidence from unrelated dependencies or topology paths.

Evidence is partitioned by normalized semantic scope before ranking.

Normalization removes explicit system entities already implied by a semantic boundary. For example:

```yaml
entities:
  - system_entity.application_service
  - system_entity.external_dependency
boundaries:
  - boundary.application.external_dependency
```

and:

```yaml
boundaries:
  - boundary.application.external_dependency
```

represent the same effective topology partition because the boundary already implies its source and target entities.

Non-redundant entities and exact runtime attributes remain part of the partition. Therefore `dependency=stripe` and `dependency=github` stay independent.

This normalization follows the endpoint-expansion semantics already established by RFC 0005. It does not introduce arbitrary topology hierarchy inference.

## Automatic target discovery

Within each active scope partition, every currently `observed` semantic observation is considered a possible diagnosis target.

The engine calls the existing `rank_causes` projection for each target.

- if upstream hypothesis candidates exist, the full ranking is stored under `diagnoses`
- if no upstream hypothesis path exists, the observation is listed under `unranked_observations`

An unranked observation is not an error. It means the current causal knowledge graph does not yet explain that observation.

This makes missing ontology coverage visible rather than silently inventing a cause.

## Trace-backed causal coverage

The live OpenTelemetry example observes both `observation.dependency.latency` and `observation.network.connection_timeout`. Existing claims and executable experiments already ground both mechanisms, so this RFC adds explicit causal edges:

```text
hypothesis.latency.external_dependency
  --causes-->
observation.dependency.latency

hypothesis.network.connection_timeout
  --causes-->
observation.network.connection_timeout
```

Both edges are strong and carry claim plus experiment provenance.

The external-dependency edge does not claim to explain why the provider itself became slow. It models a slow synchronous dependency interaction as the causal antecedent of the measured dependency latency.

The connection-timeout edge does not claim that every timeout comes from the same packet-drop mechanism used in the experiment. It models unresolved TCP establishment until the configured deadline as the causal antecedent of the connection-timeout observation, while preserving limitations on the grounded mechanism.

## Explainability

Automatic diagnosis must remain as transparent as manual ranking.

The watcher does not emit a new probability, confidence percentage, or root-cause score. Each diagnosis preserves:

- candidate rank
- causal path
- conflicts
- matched path observations
- prediction matches by declared strength
- weakest causal-edge strength
- weakest provenance
- experiment and claim support
- human-readable reasons
- selected runtime evidence context

A consumer can therefore answer why candidate A ranks above candidate B using the same visible factors as the CLI.

## Failure behavior

The diagnosis layer fails rather than guessing when:

- the runtime evidence document is malformed
- scope references are invalid
- runtime evidence contains a contradiction inside one selected scope
- diagnosis snapshot schema validation fails
- the evidence HTTP endpoint is unreachable or returns an unexpected document

A receiver with no evidence yet is a normal waiting state, not a diagnosis failure.

## Non-goals

This RFC does not introduce:

- probabilistic root-cause scoring
- automatic remediation
- automatic execution of diagnostic probes
- an incident database
- a durable global evidence revision
- event-stream delivery from the OTLP receiver
- multi-incident routing inside one watcher
- causal discovery from trace structure or telemetry correlations
- automatic creation of missing causal edges

## Consumer transport

RFC 0010 adds a thin read-only HTTP API over the diagnosis snapshot. That API validates and serves the same snapshot contract instead of moving HTTP concerns into the diagnosis engine.

MCP and other consumer transports should follow the same pattern: project the existing diagnosis snapshot without creating a second reasoning model.

## Future work

The next useful steps are:

1. expose diagnosis snapshots through an MCP resource for agents
2. replace polling with an event-driven evidence-change notification when a real deployment needs it
3. add explicit recommended-next-probe projection when top candidates remain ambiguous
4. correlate traces, logs, and metrics into the same incident evidence partitions
5. add durable incident state only when retention and restart recovery become concrete requirements
6. add controlled remediation proposals only after diagnosis and verification semantics are mature

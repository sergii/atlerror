# Atlerror

Atlerror is an open, machine-readable semantic layer for software troubleshooting.

It is designed for two consumers at the same time:

1. **Humans** - to learn how software systems fail, how to reason about incidents, and how system design concepts relate to real failure modes.
2. **Machines and agents** - to classify observations, evaluate hypotheses, select diagnostic probes, and explain why a diagnostic action should happen next.

The project starts from a small executable vertical slice around **high CPU utilization** and grows toward a broader debugging ontology.

## Core idea

Atlerror models a diagnostic loop explicitly:

```text
symptom
  -> observations
  -> candidate hypotheses
  -> predictions
  -> probes / experiments
  -> findings
  -> hypothesis updates
  -> cause / contributing factors
  -> mitigation / fix
  -> verification / prevention
```

The long-term goal is to make this loop useful both as documentation and as a deterministic reasoning substrate for developer tools and AI agents.

## Design principles

- **One semantic source, multiple projections.** Human documentation, agent context, CLI output, APIs, MCP resources, and websites should derive from the same canonical knowledge.
- **Knowledge first. Tools second. AI third.** Atlerror should not depend on Datadog, OpenTelemetry, Kubernetes, Ruby, or an LLM. Those are adapters and consumers.
- **Stable machine-addressable IDs.** Product naming may change; semantic IDs should not.
- **Falsification matters.** A useful hypothesis defines what evidence would support it and what evidence would make it less likely.
- **Transport agnostic.** CLI, embedded libraries, MCP, HTTP, gRPC, or Unix domain sockets are integration choices above the semantic core.
- **Human-readable and machine-usable.** Every concept should teach something to a person and be addressable by software.

## Repository structure

```text
RFC/             design decisions and semantic roadmap
schema/          structural validation for machine-readable knowledge
vocabulary/      canonical kinds and relations
knowledge/       concrete troubleshooting concepts
causal/          explicit directional causal edges
rules/           deterministic inference rules
claims/          empirically testable claims
experiments/     experiment manifests
lab/             executable empirical labs
scripts/         validators and transport-independent projections
examples/        complete diagnostic flows and runtime evidence samples
```

## Causal projection

The semantic causal graph can be projected as stable JSON before adding any transport-specific adapter.

Find the shortest directed causal path:

```bash
python scripts/causal_projection.py --pretty path \
  hypothesis.network.packet_corruption \
  observation.network.transport_latency
```

Look backward from an observation to plausible causal antecedents:

```bash
python scripts/causal_projection.py --pretty causes \
  observation.network.tcp_retransmissions
```

Bound reverse traversal when a consumer wants a local causal neighborhood:

```bash
python scripts/causal_projection.py --pretty causes \
  observation.network.tcp_retransmissions \
  --max-depth 1
```

The JSON contract is defined by `schema/causal-projection.schema.json`. CLI, MCP, HTTP, and other adapters should consume the same projection instead of reimplementing graph semantics.

## Causal ranking

A projection can also rank upstream **hypotheses** for an observed target without pretending to calculate a universal probability.

With only TCP retransmissions as the target, packet loss ranks ahead of packet corruption because packet loss directly and strongly predicts retransmissions through a shorter evidence-backed causal path:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --pretty
```

Add current observations to change the ranking transparently. Receiver-side TCP integrity errors move packet corruption ahead because that observation lies directly on its causal path and is a strong hypothesis prediction:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --observed observation.network.tcp_integrity_errors \
  --pretty
```

Known-absent observations can penalize a causal path or an explicit hypothesis falsifier:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --absent observation.network.tcp_integrity_errors \
  --pretty
```

Ranking is deterministic and ordinal. The output exposes every factor used for ordering: conflicts, observed causal path nodes, matching hypothesis predictions by declared strength, weakest causal edge strength, evidence provenance, path distance, and `contributes_to` edges. It deliberately emits no probability or opaque numeric score. The contract is defined by `schema/causal-ranking.schema.json`.

## Runtime evidence

Runtime evidence connects the reusable semantic graph to facts from one incident. An evidence instance says that a semantic observation was present or explicitly absent at a particular time, with provenance, scope, optional measurements, and ordinal confidence.

Resolve a bundle independently:

```bash
python scripts/runtime_evidence.py \
  examples/runtime-evidence/network-corruption-chain.yaml \
  --as-of 2026-09-11T14:48:00Z \
  --pretty
```

Or feed the same bundle directly into causal ranking:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-corruption-chain.yaml \
  --as-of 2026-09-11T14:48:00Z \
  --pretty
```

Only active evidence affects ranking. Future instances and expired instances remain visible in the returned `evidence_context` but do not become `observed` or `absent` facts. Contradictory active states fail resolution instead of being silently reconciled.

Runtime evidence can also be selected by semantic topology scope. This prevents observations from one path on a shared host from leaking into reasoning about another path:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-mixed-scopes.yaml \
  --as-of 2026-09-11T14:48:00Z \
  --scope-boundary boundary.application.external_dependency \
  --pretty
```

The same mixed bundle can be ranked for the database boundary instead:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --evidence examples/runtime-evidence/network-mixed-scopes.yaml \
  --as-of 2026-09-11T14:48:00Z \
  --scope-boundary boundary.application.database \
  --pretty
```

Selectors can also use `--scope-entity` and exact `--scope-attribute KEY=VALUE` matches. When a scope query is supplied, evidence that cannot be proven applicable to that scope is excluded conservatively and listed in `scope_filtered_instance_ids`. Boundary scopes imply their semantic source and target entities, so an entity selector can match evidence scoped to a boundary containing that entity.

Runtime confidence and measurement magnitude are preserved for auditability but are not converted into probabilities or hidden weights. The contracts and semantics are defined by `schema/runtime-evidence.schema.json` and [RFC 0005](RFC/0005-runtime-evidence-instances.md).

## First vertical slice

The initial slice models `symptom.cpu.high` and a small set of hypotheses:

- `hypothesis.traffic.increase`
- `hypothesis.cpu.busy_loop`
- `hypothesis.gc.pressure`
- `hypothesis.kernel.work`

This slice is intentionally small. The broader semantic model is tracked in [RFC 0001](RFC/0001-semantic-foundation.md).

## Status

Experimental. The schema and vocabulary are expected to evolve while keeping the intent and stable IDs explicit.

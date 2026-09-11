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
scripts/         validators, projections, ranking, and telemetry adapters
examples/        diagnostic flows, runtime evidence, adapter mappings, and telemetry fixtures
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

## Prometheus adapter

The first concrete telemetry adapter translates Prometheus instant-query results into the same runtime evidence contract. Prometheus-specific query syntax and deployment thresholds stay in adapter configuration instead of leaking into canonical semantic knowledge.

Query a live Prometheus-compatible server and write a runtime evidence bundle:

```bash
python scripts/prometheus_adapter.py \
  examples/adapters/prometheus/network-tcp.yaml \
  --incident-id incident.network.production \
  --base-url http://localhost:9090 \
  > /tmp/atlerror-evidence.yaml
```

The generated bundle can immediately feed scope-aware causal ranking:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --evidence /tmp/atlerror-evidence.yaml \
  --scope-boundary boundary.application.external_dependency \
  --pretty
```

Saved Prometheus HTTP API responses can be used instead of live requests for deterministic replay and tests:

```bash
python scripts/prometheus_adapter.py \
  examples/adapters/prometheus/network-tcp.yaml \
  --incident-id incident.network.replay \
  --response tcp_retransmissions_rate=examples/telemetry/prometheus/tcp_retransmissions_rate.json \
  --response tcp_integrity_errors_rate=examples/telemetry/prometheus/tcp_integrity_errors_rate.json
```

Each vector series becomes one evidence instance. Mapping rules explicitly define the threshold comparison and the `observed` or `absent` state on each side of that comparison. Semantic boundaries and entities can come from validated Prometheus label values, while ordinary labels can become exact scope attributes. Missing required scope labels or unknown topology IDs fail conversion instead of silently producing unscoped evidence.

The first adapter supports Prometheus `vector` and `scalar` instant-query results. Range aggregation remains a PromQL concern for now. Optional bearer authentication reads the token from an environment variable through `--bearer-token-env`. The mapping schema and design rationale are documented in `schema/prometheus-adapter.schema.json` and [RFC 0006](RFC/0006-prometheus-runtime-evidence-adapter.md).

## OpenTelemetry trace adapter

The trace adapter converts OTLP/HTTP JSON trace payloads into the same runtime evidence contract. Mapping configuration selects spans explicitly by kind, name, and attributes, then evaluates duration or error status without treating trace structure itself as causal proof.

Convert the included checkout-to-Stripe trace fixture:

```bash
python scripts/opentelemetry_trace_adapter.py \
  examples/adapters/opentelemetry/external-dependency.yaml \
  examples/telemetry/opentelemetry/external-dependency-trace.json \
  --incident-id incident.checkout.stripe_timeout
```

The example emits `observation.dependency.latency` and `observation.network.connection_timeout` for `boundary.application.external_dependency`, preserving `service=checkout-api`, `dependency=stripe`, trace ID, span ID, timing, and source provenance.

Dynamic semantic boundary and entity IDs read from telemetry are validated before evidence is emitted. OpenTelemetry span names, status codes, deployment attributes, and latency thresholds remain adapter policy rather than canonical Atlerror knowledge. The mapping contract and rationale are documented in `schema/opentelemetry-trace-adapter.schema.json` and [RFC 0007](RFC/0007-opentelemetry-trace-adapter.md).

## Live OTLP/HTTP receiver

Run an incident-scoped live receiver on the standard OTLP/HTTP port:

```bash
python scripts/otlp_http_receiver.py \
  examples/adapters/opentelemetry/external-dependency.yaml \
  --incident-id incident.checkout.live \
  --snapshot /tmp/atlerror-runtime-evidence.json
```

The receiver binds to `127.0.0.1:4318` by default and accepts JSON trace exports at `POST /v1/traces`. It also exposes `GET /health`, `GET /status`, and `GET /evidence`. Identity and gzip request bodies are supported; binary protobuf is intentionally rejected in this first slice.

A Collector can send JSON OTLP to the receiver:

```yaml
exporters:
  otlp_http/atlerror:
    endpoint: http://127.0.0.1:4318
    encoding: json

service:
  pipelines:
    traces:
      exporters: [otlp_http/atlerror]
```

Live traces repeat continuously, so the receiver does not retain every span-derived state. It keeps the latest evidence instance for each `(observation, exact scope)` pair. Exact replays are deduplicated, older out-of-order spans cannot roll state backward, and different dependency scopes stay independent. `/status` exposes the insert, replacement, duplicate, and out-of-order counters.

The optional snapshot is atomically replaced with the current runtime evidence bundle and can feed existing ranking commands directly. The receiver semantics and explicit aggregation policy are documented in [RFC 0008](RFC/0008-live-otlp-http-receiver.md).

## First vertical slice

The initial slice models `symptom.cpu.high` and a small set of hypotheses:

- `hypothesis.traffic.increase`
- `hypothesis.cpu.busy_loop`
- `hypothesis.gc.pressure`
- `hypothesis.kernel.work`

This slice is intentionally small. The broader semantic model is tracked in [RFC 0001](RFC/0001-semantic-foundation.md).

## Status

Experimental. The schema and vocabulary are expected to evolve while keeping the intent and stable IDs explicit.

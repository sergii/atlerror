# RFC 0008: Live OTLP/HTTP trace receiver

Status: accepted

## Summary

RFC 0007 introduced a file-oriented OpenTelemetry trace adapter. That proved the semantic translation from OTLP spans into Atlerror runtime evidence, but it still required a saved JSON payload.

This RFC adds a small live ingestion boundary for OTLP/HTTP JSON traces. The receiver accepts trace export requests, applies the existing OpenTelemetry mapping configuration, keeps one current evidence instance per semantic observation and exact scope, and exposes the resulting runtime evidence bundle over HTTP or an optional atomic JSON snapshot.

The receiver is intentionally small. It is not a replacement for the OpenTelemetry Collector, a trace backend, or an incident database.

## Decision

`scripts/otlp_http_receiver.py` runs one incident-scoped receiver process.

Example:

```bash
python scripts/otlp_http_receiver.py \
  examples/adapters/opentelemetry/external-dependency.yaml \
  --incident-id incident.checkout.live \
  --snapshot /tmp/atlerror-runtime-evidence.json
```

The default listener is `127.0.0.1:4318`. Binding to loopback is deliberate so the experimental receiver is not exposed remotely by default.

The receiver provides:

- `POST /v1/traces` for OTLP/HTTP JSON trace export
- `GET /health` for a minimal liveness check
- `GET /status` for receiver counters and aggregation policy
- `GET /evidence` for the current runtime evidence bundle

The success body for `POST /v1/traces` is the empty OTLP JSON export response `{}`.

## OTLP transport boundary

The first live receiver supports OTLP/HTTP JSON only.

Accepted request properties:

- `Content-Type: application/json`
- identity or gzip `Content-Encoding`
- a bounded request body controlled by `--max-body-bytes`

Binary OTLP protobuf is intentionally rejected with HTTP 415. A standard OpenTelemetry Collector can target this receiver by using its OTLP HTTP exporter with JSON encoding.

For example:

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

The Collector remains responsible for production-grade receiving, batching, retries, TLS, queues, and upstream protocol diversity. Atlerror consumes the JSON export boundary after those concerns.

## Unmatched spans

A live OTLP stream normally contains many spans that are irrelevant to one Atlerror mapping configuration.

Therefore the live receiver differs intentionally from the file adapter CLI:

- the file adapter fails when no span matches, because a saved input was explicitly supplied for conversion
- the live receiver accepts a valid OTLP request even when zero spans match, returns `{}`, and records no evidence

This lets the OpenTelemetry mapping behave as a semantic filter over a normal trace stream.

## Repeated spans and evidence aggregation

Live traces make repeated evidence unavoidable. A dependency may produce hundreds of spans within the freshness window, and individual spans may cross a threshold in opposite directions.

Storing every span-derived `observed` and `absent` instance in one incident bundle would make ordinary variation look like contradictory runtime evidence.

The first receiver therefore uses an explicit policy:

```text
latest_per_observation_and_exact_scope
```

The aggregation key is:

```text
(semantic observation, exact runtime scope)
```

For each key, the receiver retains one evidence instance: the instance with the latest `observed_at` timestamp. If two different instances have the same timestamp, their deterministic evidence IDs provide a stable tie-breaker.

This policy has several important consequences:

- a newer normal span can replace an older failing span for the same observation and scope
- an older replay cannot roll state backward
- different dependencies or boundaries remain separate because their scopes differ
- a broad scope query may still reveal meaningful contradictions across distinct sub-scopes, which is correct because the query did not isolate them

The receiver does not hide this policy. `/status` reports its name plus insert, replacement, duplicate, and out-of-order counters.

## Deterministic replay

RFC 0007 defines evidence IDs from adapter ID, mapping ID, trace ID, and span ID.

The live receiver uses those IDs for replay safety:

- an exact replay of the same evidence instance is counted as a duplicate and does not create another stored instance
- if the same deterministic ID arrives with different content, ingestion fails instead of silently rewriting provenance
- an older different span for the same observation and exact scope is counted as out of order and ignored

This makes Collector retries and fixture replay safe without relying on arrival order.

## Evidence snapshot

`--snapshot PATH` enables an optional current-state file.

After a successful state change the receiver writes the complete runtime evidence bundle to a temporary sibling file and atomically replaces the configured snapshot path.

The snapshot uses the same `runtime_evidence` contract as every other Atlerror evidence source. It can therefore feed existing commands directly:

```bash
python scripts/causal_ranking.py \
  observation.dependency.latency \
  --evidence /tmp/atlerror-runtime-evidence.json \
  --scope-boundary boundary.application.external_dependency \
  --scope-attribute dependency=stripe \
  --pretty
```

The snapshot is current state, not a durable trace archive.

## Incident model

One receiver process has one explicit `--incident-id`.

This matches the existing runtime evidence contract and avoids silently inventing incident boundaries from telemetry attributes.

Multi-incident routing, automatic incident creation, and persistent incident stores are separate concerns and should be introduced only with a concrete consumer.

## Failure behavior

The receiver returns an error rather than guessing when:

- OTLP JSON is malformed
- the request exceeds the configured size bound
- an unsupported content type or content encoding is used
- a matched span contains an unknown or wrong-kind semantic boundary/entity ID
- a required topology attribute is missing
- a deterministic evidence ID is replayed with different content
- an atomic snapshot write fails

Unmatched but otherwise valid spans are not errors.

## Security boundary

The built-in receiver is an integration primitive, not an internet-facing telemetry gateway.

It binds to `127.0.0.1` by default and does not implement TLS or authentication. Remote deployments should keep a Collector, reverse proxy, service mesh, or equivalent authenticated transport boundary in front of it.

## Non-goals

This RFC does not introduce:

- OTLP/gRPC
- OTLP binary protobuf decoding
- metrics or logs receiving
- TLS or authentication
- persistent trace storage
- durable queues or backpressure
- automatic incident discovery
- multi-tenant routing
- automatic topology discovery
- statistical trace aggregation
- probabilistic ranking
- parent/child span causality inference

## Future work

The next useful steps should be driven by real use of the live path. Likely candidates are:

1. a consumer that runs causal ranking automatically when the current evidence snapshot changes
2. explicit evidence-window aggregation policies when latest-value semantics prove insufficient
3. trace-log correlation through trace and span IDs
4. standardized OpenTelemetry semantic-convention mappings for common HTTP, database, RPC, and messaging spans
5. a durable incident evidence store if in-memory current state becomes insufficient

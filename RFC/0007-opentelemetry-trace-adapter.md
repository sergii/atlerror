# RFC 0007: OpenTelemetry trace runtime evidence adapter

Status: accepted

## Summary

Atlerror can now ingest metric evidence through the Prometheus adapter. Distributed traces add a different class of information: concrete service interactions, span timing, trace correlation, error status, and topology attributes attached to one request path.

This RFC defines a first OpenTelemetry trace adapter that translates OTLP/HTTP JSON trace payloads into the existing `runtime_evidence` contract.

The adapter remains a transport-specific boundary. It does not move OpenTelemetry field names, span kinds, or deployment-specific topology labels into canonical troubleshooting knowledge.

## Decision

OpenTelemetry trace adapter configurations validate against `schema/opentelemetry-trace-adapter.schema.json`.

A configuration maps a selected class of spans to an existing semantic observation. For example:

```yaml
schema_version: "0.1"
kind: opentelemetry_trace_adapter
id: adapter.opentelemetry.external_dependency
mappings:
  - id: dependency_latency
    observation: observation.dependency.latency
    match:
      span_kind: SPAN_KIND_CLIENT
      attributes:
        rpc.system: http
    signal:
      type: duration_ms
      operator: gt
      threshold: 100
    scope:
      boundary_attributes:
        - atlerror.boundary
      attribute_map:
        service: service.name
        dependency: peer.service
```

The mapping expresses adapter policy:

- which spans count as evidence for an observation
- how a span-derived signal becomes `observed` or `absent`
- how span and resource attributes become semantic topology scope
- freshness TTL and confidence defaults

These decisions are explicit and reviewable rather than inferred from arbitrary telemetry names.

## Supported OTLP shape

The first implementation consumes OTLP/HTTP JSON trace payloads with `resourceSpans`, `scopeSpans`, and `spans`.

Resource attributes and span attributes are decoded and merged, with span attributes taking precedence. Common OTLP `AnyValue` scalar forms are supported.

The adapter currently supports two signal types.

### Duration

`duration_ms` derives a numeric measurement from `startTimeUnixNano` and `endTimeUnixNano` and compares the result with an explicit threshold using one of `gt`, `gte`, `lt`, `lte`, `eq`, or `neq`.

The resulting runtime evidence preserves:

- measured duration
- threshold as baseline
- numeric delta
- unit
- above/below/equal comparison

The threshold remains deployment adapter policy. It is not promoted to canonical semantic knowledge.

### Error status

`status_error` interprets OTLP span status `STATUS_CODE_ERROR` as the true condition. This is useful when a mapping already narrows the span class, for example by an explicit `error.type` attribute.

## Span selection

Mappings can select spans by exact:

- span kind
- span name
- attribute key/value pairs

The first version deliberately avoids regexes, semantic guessing, and fuzzy matching. Exact matching is easier to audit and portable across adapters.

## Topology scope

Trace evidence can carry topology without requiring a separate metrics label join.

Mappings can combine:

- static semantic entities and boundaries
- semantic entity IDs read from configured attributes
- semantic boundary IDs read from configured attributes
- ordinary runtime attributes copied into exact Atlerror scope attributes

Dynamic semantic references are validated. If a span says `atlerror.boundary=boundary.application.external_dependency`, that ID must exist and must have `kind: boundary`.

Missing required scope attributes fail conversion. The adapter does not silently downgrade a scoped mapping into unscoped evidence.

The included example copies `service.name` and `peer.service` into runtime scope attributes. A resolver can therefore select evidence for a specific dependency while keeping canonical topology reusable.

## Provenance and identity

Generated evidence uses `source.type: trace`.

Provenance records:

- trace ID
- span ID
- span name
- span kind
- selected service/dependency attributes
- optional source URI

Evidence instance IDs are deterministic for the tuple `(adapter, mapping, trace ID, span ID)`. Replaying the same trace through the same mapping therefore produces the same evidence identity.

## Freshness

The span end time becomes `observed_at`. `expires_at` is derived from the configured TTL.

This follows the same freshness semantics as metric and manual runtime evidence. The trace adapter does not create a second time model.

## Failure behavior

The adapter fails instead of guessing when:

- adapter schema validation fails
- a mapping references an unknown observation
- a dynamic entity or boundary reference is unknown or has the wrong semantic kind
- required topology attributes are missing
- nanosecond timestamps are invalid
- span end precedes span start
- no spans match any configured mapping

The file-oriented adapter keeps the final rule because an explicitly supplied saved payload that produces no evidence is usually a configuration mistake. RFC 0008 defines different no-match behavior for a live stream, where unrelated spans are expected.

## Non-goals

This RFC does not introduce:

- a trace store
- topology discovery from arbitrary service names
- probabilistic inference from trace frequency
- automatic latency baselines
- regex or natural-language span classification
- log correlation
- parent/child causal inference from trace structure

Live OTLP/HTTP ingestion is defined separately in RFC 0008 so transport and repeated-evidence policy do not become part of the trace-to-observation mapping contract.

## Example flow

The repository fixture demonstrates a client span from `checkout-api` to `stripe` on `boundary.application.external_dependency`.

The span lasts 250 ms against a configured 100 ms threshold and ends with an error status plus `error.type=TimeoutError`. The adapter emits two existing semantic observations:

- `observation.dependency.latency` as observed
- `observation.network.connection_timeout` as observed

Both evidence instances retain the same boundary and dependency scope, so runtime evidence resolution can isolate the Stripe path from other dependencies in the same incident.

## Future work

The next useful trace work should be driven by real incidents. Likely extensions are:

1. parent/child path context without treating span trees as a causal graph automatically
2. trace-log correlation using trace and span IDs
3. standardized semantic-convention mappings for common HTTP, database, RPC, and messaging spans
4. richer explicit evidence-window aggregation if the live latest-state policy in RFC 0008 proves insufficient
5. persistent incident evidence storage when a concrete consumer needs retention

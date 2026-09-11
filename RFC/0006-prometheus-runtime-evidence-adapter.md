# RFC 0006: Prometheus runtime evidence adapter

Status: accepted

## Summary

Atlerror now has a static semantic graph, incident-scoped runtime evidence, freshness rules, and scope-aware causal ranking. This RFC adds the first concrete telemetry adapter: Prometheus instant-query results can be translated into the existing `runtime_evidence` contract without putting Prometheus-specific semantics into canonical knowledge.

The adapter boundary is intentional:

```text
Prometheus / PromQL
  -> adapter mapping policy
  -> runtime evidence instances
  -> freshness and scope selection
  -> causal ranking
```

Prometheus is a producer of runtime facts, not part of Atlerror's ontology.

## Decision

Prometheus adapter mappings validate against `schema/prometheus-adapter.schema.json` and are executed by `scripts/prometheus_adapter.py`.

A mapping connects one PromQL instant query to one semantic observation:

```yaml
id: tcp_retransmissions_rate
observation: observation.network.tcp_retransmissions
query: rate(node_netstat_Tcp_RetransSegs[5m])
unit: segments_per_second
state_rule:
  operator: gt
  threshold: 5
  state_when_true: observed
  state_when_false: absent
```

Thresholds belong to adapter configuration because a meaningful baseline depends on deployment, path, workload, and query semantics. They are not universal properties of the observation concept.

## HTTP and offline inputs

The adapter can query a Prometheus-compatible HTTP API through `/api/v1/query` or consume saved query responses for deterministic tests and incident replay.

Live mode uses:

```bash
python scripts/prometheus_adapter.py \
  examples/adapters/prometheus/network-tcp.yaml \
  --incident-id incident.network.production \
  --base-url http://localhost:9090
```

Saved responses use repeated `--response MAPPING_ID=PATH` overrides. A mapping without a saved response falls back to the live Prometheus server when `--base-url` is present.

Optional bearer authentication reads the token from an environment variable rather than a command-line value, so the adapter does not require secrets in configuration or shell history.

## Result semantics

The first implementation supports Prometheus instant-query `vector` and `scalar` results.

Each vector series becomes one runtime evidence instance. The instance ID is deterministic for the adapter ID, mapping ID, and label set, which makes repeated conversion stable while allowing several scoped series from the same query.

Matrix/range-query results are deliberately out of scope. Time-series aggregation belongs in PromQL for this adapter version, so the semantic boundary receives an already evaluated instant value.

Non-finite values such as `NaN` or infinity fail conversion instead of becoming ambiguous evidence.

## State rules

The first rule vocabulary is deliberately small:

- `gt`
- `gte`
- `lt`
- `lte`
- `eq`
- `neq`

A rule explicitly declares the runtime evidence state when the comparison is true and false. This is important because a metric being below a threshold is not automatically equivalent to absence unless the adapter author makes that policy explicit.

The threshold is preserved as the evidence measurement baseline, the raw value is preserved as the measurement value, and the numeric delta remains available for auditability. Causal ranking still does not convert those magnitudes into hidden weights or probabilities.

## Scope from Prometheus labels

Mappings can combine static semantic scope with scope extracted from Prometheus labels.

Supported dynamic mappings are:

- label value -> `system_entity` concept ID
- label value -> `boundary` concept ID
- label value -> exact runtime scope attribute

For example:

```yaml
scope:
  boundary_labels:
    - atlerror_boundary
  entity_labels:
    - atlerror_entity
  attribute_labels:
    service: service
    instance: instance
```

If a configured scope label is missing, conversion fails. If a label claims a semantic boundary or entity that does not exist or has the wrong kind, conversion also fails. The adapter must not silently downgrade supposedly scoped telemetry into unscoped evidence.

This allows telemetry pipelines to attach semantic topology IDs through Prometheus labels or relabeling rules while keeping those IDs independent from vendor-specific query syntax.

## Provenance

Generated runtime evidence records:

- source type `metric`
- stable source name derived from the mapping ID
- PromQL query
- all Prometheus series labels
- optional Prometheus base URL
- adapter and mapping IDs

The generated evidence can therefore be audited back to the metric series and mapping policy that produced it.

## Configuration versus canonical knowledge

Prometheus mappings are adapter policy, not semantic truth.

In particular:

- a query may be deployment-specific
- a threshold may need local calibration
- an exporter metric may not exist everywhere
- labels may need relabeling before they carry Atlerror topology IDs

The example mapping intentionally demonstrates both a standard node-exporter retransmission query and a deployment-specific checksum-error metric. The latter must only be used when the metric actually represents TCP checksum or equivalent integrity failures. A generic TCP error counter must not be relabeled as `observation.network.tcp_integrity_errors` merely because the names look similar.

## Failure behavior

The adapter fails loudly on:

- invalid mapping schema
- unknown semantic observation, entity, or boundary references
- duplicate mapping IDs
- missing configured scope labels
- unsupported Prometheus result types
- malformed API responses
- query API failures
- non-numeric or non-finite samples
- missing live or saved response input for a mapping

This conservative behavior is preferable to emitting plausible-looking but semantically unsafe diagnostic evidence.

## Non-goals

This RFC does not introduce:

- Prometheus as canonical Atlerror storage
- automatic threshold learning
- automatic semantic mapping from arbitrary metric names
- range-query or histogram interpretation
- recording-rule management
- service discovery
- alert ingestion
- probabilistic inference
- automatic topology inference from labels

Those can be added only when a concrete use case justifies their semantics.

## Future work

The next useful adapter work should be driven by real diagnostic inputs rather than adapter breadth for its own sake. Likely candidates are:

1. OpenTelemetry trace/log adapters that can preserve trace, span, service, and boundary context
2. Prometheus range-query support when a diagnostic rule genuinely needs temporal shape rather than an instant PromQL result
3. adapter composition so several telemetry sources can contribute to one runtime evidence bundle
4. thin MCP or HTTP surfaces over the now end-to-end reasoning pipeline

The core invariant remains: adapters translate external telemetry into the shared runtime evidence contract; causal reasoning should not depend on any particular telemetry vendor.

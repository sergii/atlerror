# RFC 0004: Explicit causal graph

Status: accepted

## Summary

Atlerror currently models diagnostic relevance, predictions, evidence, falsification, and non-causal relationships. Those are not enough to express directional mechanism chains without relying on prose. This RFC introduces first-class causal edges as separate semantic records.

The key distinction is:

- diagnostic relations answer **what should I consider or believe given evidence?**
- causal relations answer **what can produce what, under which conditions?**

These concerns must remain separate.

## Motivation

The network labs now demonstrate mechanism chains that cannot be represented correctly by `related_to` or `may_indicate`.

For example:

```text
packet corruption
  -> receiver TCP integrity error
  -> TCP retransmission
  -> additional transport latency
```

An application may still receive an exact payload. Treating these nodes as merely related loses causal direction, while interpreting `may_indicate` as causal would invert diagnostic semantics.

A machine diagnostic agent needs to traverse both directions safely:

- forward: if this mechanism is real, what consequences should I seek?
- backward: given this observation, which causal antecedents are plausible?

## Decision

Causal edges live under `causal/**/*.yaml` and validate against `schema/causal-edge.schema.json`.

They are not concept nodes. Endpoints reference existing concepts.

Required fields:

```yaml
id: causal.network.packet_loss.tcp_retransmissions
kind: causal_edge
source: hypothesis.network.packet_loss
target: observation.network.tcp_retransmissions
relation: causes
strength: strong
explanation: Missing TCP segments drive reliable transport recovery and retransmission.
```

Optional fields declare conditions, evidence, and limitations.

## Relations

The initial causal relation vocabulary is intentionally small.

### `causes`

Use when the source is modeled as a direct causal antecedent of the target under the declared conditions.

### `contributes_to`

Use when the source can materially increase or worsen the target but is not necessarily sufficient on its own.

Do not introduce `causes` merely because two values correlate or because one predicts the other.

## Evidence

A causal edge may reference claims and experiments:

```yaml
evidence:
  claims:
    - claim.network.packet_loss.partial_loss_causes_tcp_retransmissions
  experiments:
    - experiment.network.packet_loss.tcp_retransmissions_python_linux
```

Evidence references provide provenance. They do not transform experimental observations into universal causal laws. Conditions and limitations remain part of the edge.

The semantic validator must ensure:

- edge IDs are unique
- source and target resolve to concepts
- self-edges are rejected
- duplicate `(source, relation, target)` triples are rejected
- claim and experiment evidence IDs resolve
- when an experiment is listed alongside claims, it must support at least one listed claim

## Epistemic versus causal graph

Atlerror intentionally keeps two graph layers.

### Epistemic / diagnostic graph

Examples:

```text
symptom --may_indicate--> hypothesis
hypothesis --predicts--> observation
observation --supports--> hypothesis
observation --contradicts--> hypothesis
```

These describe relevance and belief updates.

### Causal graph

Examples:

```text
hypothesis --causes--> observation
observation --causes--> observation
observation --contributes_to--> observation
```

These describe directional mechanism and consequence structure.

An observation can be both evidence for a hypothesis and an effect in a causal chain. Those roles are distinct.

## Initial network slice

The first graph slice is grounded by existing empirical labs:

```text
hypothesis.network.packet_corruption
  --causes-->
observation.network.tcp_integrity_errors
  --causes-->
observation.network.tcp_retransmissions
  --contributes_to-->
observation.network.transport_latency

hypothesis.network.packet_loss
  --causes-->
observation.network.tcp_retransmissions
```

This immediately enables useful diagnostic reasoning:

- exact application payload does not falsify packet corruption
- retransmissions have multiple causal antecedents
- retransmissions can explain additional latency without making latency itself the root mechanism

## Projection boundary

The first consumer-facing projection is transport independent. `scripts/causal_projection.py` reads canonical concepts and causal edges and emits JSON that validates against `schema/causal-projection.schema.json`.

The projection supports two query modes:

- `path` returns one deterministic shortest directed path between two concepts
- `causes` traverses backward from a target and returns one shortest path from each distinct upstream antecedent, optionally bounded by depth

Projected paths include concept identity and human-facing metadata plus causal edge semantics, conditions, evidence provenance, limitations, and source file paths. CLI, MCP, HTTP, and other adapters should consume this projection contract instead of implementing their own traversal rules.

The older `scripts/causal_path.py` command remains a compatibility view but delegates graph traversal to the shared projection implementation.

## Causal candidate ranking

A causal projection can contain several upstream antecedents, but an agent also needs a deterministic way to decide which **hypotheses** deserve attention first under the evidence currently available.

`scripts/causal_ranking.py` adds a transport-independent ordinal ranking projection defined by `schema/causal-ranking.schema.json`. The target is treated as the observed effect being explained. Callers may also provide observation IDs that are currently present and observation IDs that are known absent or normal.

The initial ranking deliberately does not assign probabilities. Candidates are ordered lexicographically using visible semantic factors, in this priority order:

1. fewer conflicts with supplied absent observations and explicit hypothesis falsifiers
2. more supplied observations that occur as intermediate nodes on the candidate causal path
3. more strong, then moderate, then weak hypothesis predictions matching the target or supplied observations
4. stronger weakest causal edge on the path
5. stronger weakest evidence provenance on the path, ordered as experiment, claim, none
6. shorter causal path
7. fewer `contributes_to` edges
8. stable source ID as the final deterministic tie-breaker

This ordering is intentionally explainable rather than statistically calibrated. Each ranked candidate includes the projected path and all factors used to order it. A consumer can therefore explain why one candidate moved above another without reconstructing hidden weights.

For example, TCP retransmissions alone rank packet loss ahead of packet corruption in the current N1 slice because packet loss strongly predicts retransmissions through a shorter evidence-backed path. If receiver TCP integrity errors are also observed, packet corruption moves ahead because the observed integrity error is on its causal path and is also a strong hypothesis prediction. If those integrity errors are known absent, that path receives an explicit conflict.

Query-time `--observed` and `--absent` IDs remain useful lightweight inputs. RFC 0005 adds incident-scoped runtime evidence instances with timestamps, freshness, provenance, scope, measurements, and ordinal confidence. `scripts/causal_ranking.py` can resolve those instances into active observation state and preserve the compact evidence context in its output.

## Non-goals

This RFC does not introduce:

- numeric Bayesian probabilities
- automatic causal discovery from correlations
- a universal DAG requirement
- interventions or counterfactual syntax beyond evidence-backed edge conditions
- calibrated probabilistic root-cause scoring

Feedback loops may eventually require cycles, so the validator must not require the graph to be acyclic.

Runtime evidence instance semantics are defined separately in RFC 0005 rather than being embedded into the causal-edge model.

## Future work

Causal path projection, reverse-cause lookup, transparent ordinal ranking, and runtime evidence inputs now have shared transport-independent contracts. Likely next steps are:

1. scope-aware evidence selection and candidate ranking
2. explicit masking/recovery semantics if repeated use cases justify new relation types
3. adapters that translate real metrics, traces, logs, and probes into runtime evidence instances
4. thin MCP and HTTP adapters over the shared contracts once an adapter is useful to a real consumer

The graph vocabulary should grow only when a concrete diagnostic case cannot be represented with the existing relations.

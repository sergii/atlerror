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

## Non-goals

This RFC does not introduce:

- numeric Bayesian probabilities
- automatic causal discovery from correlations
- a universal DAG requirement
- temporal event instances
- interventions or counterfactual syntax beyond evidence-backed edge conditions
- automatic root-cause ranking

Feedback loops may eventually require cycles, so the validator must not require the graph to be acyclic.

## Future work

Likely next steps:

1. causal path projection for CLI/MCP/HTTP consumers
2. reverse-cause lookup from an observation
3. path ranking using evidence strength and current observations
4. explicit masking/recovery semantics if repeated use cases justify new relation types
5. runtime evidence instances mapped onto the static causal graph

The graph vocabulary should grow only when a concrete diagnostic case cannot be represented with the existing relations.

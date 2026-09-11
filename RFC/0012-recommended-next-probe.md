# RFC 0012: Recommended next-probe projection

Status: accepted

## Summary

RFC 0009 made diagnosis continuous, RFC 0010 exposed the current diagnosis over HTTP, and RFC 0011 exposed the same diagnosis as MCP resources.

The next missing diagnostic layer is not another transport. It is a transparent answer to:

> Given the current ranked causal candidates, which existing probe would produce the most useful unresolved evidence to distinguish them?

This RFC introduces a deterministic `probe_ranking` projection. It recommends read-only or otherwise cataloged probes without executing them.

The resulting reasoning path is:

```text
runtime evidence
  -> causal ranking
  -> unresolved candidate differences
  -> existing probe catalog
  -> deterministic probe ranking
```

## Decision

`scripts/probe_ranking.py` consumes an existing `causal_ranking` document and the canonical concept catalog.

It emits `schema/probe-ranking.schema.json` with:

```yaml
schema_version: "0.1"
kind: probe_ranking
query:
  target: observation.network.tcp_retransmissions
  observed: []
  absent: []
  candidate_hypotheses:
    - hypothesis.network.packet_loss
    - hypothesis.network.packet_corruption
found: true
not_found_reason: null
ranking_method:
  type: deterministic_ordinal
  purpose: discriminate_current_causal_candidates
  priority: [...]
probes:
  - rank: 1
    probe:
      id: probe.network.inspect_tcp_integrity_errors
      kind: probe
      title: Inspect TCP integrity errors
    risk: read_only
    unresolved_observations:
      - observation.network.tcp_integrity_errors
    hypotheses_tested:
      - hypothesis.network.packet_corruption
    outcome_analysis: [...]
    factors: {...}
    reasons: [...]
```

The first probe is the recommended next probe. The complete ordered list remains visible so a consumer can inspect alternatives.

## Candidate probes

A probe becomes eligible only when at least one currently ranked hypothesis explicitly lists it under `tested_by`.

This is a conservative boundary. The projection does not search for arbitrary tools or invent a probe because its name appears related to an observation.

A probe is skipped when all observations it produces are already resolved by the current ranking query. The target observation is also treated as resolved because causal ranking already assumes the target is observed.

A probe is also skipped when none of its unresolved observations can change existing ranking factors differently for at least one pair of candidate hypotheses.

## Candidate effects

For each unresolved observation a probe can produce, the projection derives one effect record per current hypothesis.

The record contains only facts already used by causal ranking or explicit catalog relations:

- whether the observation is an intermediate node on that candidate's causal path
- the hypothesis prediction strength for that observation, if any
- the declared prediction expectation, for explanation only
- whether absence of that observation is an explicit falsifier conflict under current ranking semantics
- the declared falsifier condition, for explanation only
- whether the hypothesis explicitly lists the probe under `tested_by`

No new statistical model is introduced.

## Outcome discrimination

Runtime evidence has two semantic states: `observed` and `absent`.

The probe projection asks whether each state would affect two current candidates differently according to the same factor categories already used by `causal_ranking`.

For an `observed` result, the relevant signature is:

```text
(on causal path, prediction strength)
```

For an `absent` result, the relevant signature is:

```text
(on causal path, explicit falsifier conflict)
```

A candidate pair is discriminated by an outcome when those signatures differ.

A pair is **two-sided** when both `observed` and `absent` results discriminate the pair. Two-sided probes are preferred because either result is expected to change visible ranking factors differently between the candidates.

This does not mean either result guarantees a ranking reversal. Other already-known factors can still dominate the final lexicographic causal ranking.

## Contrast components

To make ties transparent, the projection counts which existing ranking components differ between candidates for a probe observation.

For an observed result:

- causal-path membership difference
- prediction-strength difference

For an absent result:

- causal-path membership difference
- explicit-falsifier difference

These are exposed as integer counts called `contrast_components`.

They are not probabilities, utility scores, information entropy, or hidden weights. They are counts of visible ranking dimensions whose values differ.

## Probe ordering

Probe ordering is deterministic and lexicographic.

The priority is:

1. discriminate more alternatives from the current top candidate in both result directions
2. expose more contrast components against the current top candidate
3. discriminate more alternatives from the current top candidate in at least one direction
4. discriminate more candidate pairs in both directions
5. expose more total contrast components
6. discriminate more candidate pairs in at least one direction
7. prefer lower catalog risk
8. prefer probes explicitly listed by more current hypotheses
9. use probe ID as the deterministic tie-break

The projection emits this priority list directly in `ranking_method.priority`.

There is no aggregate numeric probe score.

## Risk

Probe risk uses the existing catalog values:

```text
read_only < low < state_changing < high
```

Risk is a late tie-break after diagnostic discrimination. A lower-risk probe does not silently outrank a substantially more discriminating probe merely because it is safer.

The projection recommends only. It does not authorize execution even when a probe is `read_only`.

## Capability availability

Probe concepts can declare `requires` and `preferred_tools`.

The projection preserves these fields in output but does not infer whether the current environment actually has the required capability or tool.

This distinction is important:

```text
semantic recommendation != executable availability
```

A future execution layer must verify capabilities explicitly before running any probe.

## Network example

With only TCP retransmissions observed, the current causal ranking is:

```text
1. hypothesis.network.packet_loss
2. hypothesis.network.packet_corruption
```

`probe.network.inspect_tcp_integrity_errors` produces `observation.network.tcp_integrity_errors`.

For packet loss:

- the observation is not on the causal path
- it is not an explicit prediction
- its absence is not an explicit falsifier conflict

For packet corruption:

- the observation is an intermediate causal-path node
- it is a strong prediction
- the probe is explicitly listed under `tested_by`

Therefore an observed integrity error changes both the causal-path-match and strong-prediction factors for corruption, while an absent integrity error creates a path conflict for corruption but not packet loss.

The probe is therefore a two-sided discriminator between the current top two candidates.

Once TCP integrity errors are already observed or absent, that probe is no longer recommended for the same ranking because its output is already resolved.

## No ambiguity is also a result

If fewer than two causal candidates remain, the projection returns:

```yaml
found: false
not_found_reason: fewer_than_two_candidates
probes: []
```

If multiple candidates remain but the current catalog contains no unresolved probe that changes ranking factors differently, it returns:

```yaml
found: false
not_found_reason: no_discriminating_probe
probes: []
```

The system does not invent an action to avoid returning an empty recommendation.

## CLI

The initial CLI consumes a causal ranking JSON document:

```bash
python scripts/causal_ranking.py \
  observation.network.tcp_retransmissions \
  --pretty > /tmp/atlerror-ranking.json

python scripts/probe_ranking.py \
  /tmp/atlerror-ranking.json \
  --pretty
```

This keeps the first slice transport-independent and allows the same projection function to be embedded in live diagnosis later.

## Non-goals

This RFC does not introduce:

- automatic probe execution
- shell command generation
- capability discovery
- tool authentication or authorization
- expected monetary or operational cost
- probabilistic information gain
- entropy calculations
- LLM-selected probes
- automatic creation of new probe concepts
- remediation
- a second causal-ranking algorithm

## Future work

The next useful steps are:

1. compute probe rankings directly from each live diagnosis entry
2. expose the resulting recommendation through the existing HTTP and MCP read-only boundaries
3. show the recommendation in an end-to-end demo with the network ambiguity example
4. add capability-aware filtering before any active probe execution is introduced
5. only then add a narrowly scoped execution tool whose result returns as normal runtime evidence

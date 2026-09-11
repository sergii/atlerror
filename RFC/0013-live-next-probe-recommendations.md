# RFC 0013: Live next-probe recommendations

Status: accepted

## Summary

RFC 0012 introduced a deterministic `probe_ranking` projection over an existing causal ranking. That projection answered which catalog probe would best discriminate between the current causal candidates, but it remained a separate command-level artifact.

This RFC embeds the same validated projection into every diagnosable entry in the live `diagnosis_snapshot`.

The live path becomes:

```text
runtime evidence
  -> causal ranking
  -> probe ranking
  -> diagnosis snapshot
       |-> HTTP
       |-> MCP
```

The HTTP and MCP transports still serve the same diagnosis snapshot. They do not compute recommendations themselves.

## Decision

Every entry in `partitions[].diagnoses[]` now contains:

```yaml
target: observation.network.tcp_retransmissions
ranking:
  kind: causal_ranking
probe_ranking:
  kind: probe_ranking
```

`probe_ranking` is produced by calling the existing `rank_probes()` implementation with the causal ranking that was already generated for that target.

The first item in `probe_ranking.probes` is the current recommended next probe when `found` is true.

If the causal ranking has fewer than two candidates, the embedded projection remains explicit:

```yaml
found: false
not_found_reason: fewer_than_two_candidates
probes: []
```

If multiple candidates remain but no existing catalog probe can discriminate between them, the result is:

```yaml
found: false
not_found_reason: no_discriminating_probe
probes: []
```

No fallback probe is invented.

## Why embed the full projection

A compact `recommended_probe_id` field would be easy to consume but would hide the reason the probe was selected.

Atlerror's contract is explainability-first. Consumers should be able to inspect:

- which hypotheses are being discriminated
- which unresolved observations the probe can produce
- how positive and negative outcomes affect visible ranking factors
- whether discrimination is two-sided
- the probe risk class
- required capabilities
- preferred tools
- the deterministic ordering factors

Embedding the complete `probe_ranking` preserves that information and avoids a second recommendation model.

## Validation

The diagnosis snapshot schema requires `probe_ranking` for every diagnosis entry and validates its top-level shape.

The live diagnosis builder additionally validates each embedded projection with the canonical `schema/probe-ranking.schema.json` contract.

The read-only diagnosis snapshot reader performs the same deep validation before serving a persisted snapshot. A file whose nested probe ranking has been corrupted is therefore rejected rather than being exposed through HTTP or MCP.

This layered validation is intentional: `probe-ranking.schema.json` remains the canonical detailed contract while `diagnosis-snapshot.schema.json` owns composition.

## HTTP exposure

No new HTTP reasoning endpoint is introduced.

`GET /diagnosis` already returns the complete diagnosis snapshot, so each causal diagnosis now includes its current `probe_ranking` directly.

`GET /status` adds:

```text
next_probe_recommendations
```

This is the number of diagnosis entries whose embedded probe ranking currently has `found: true`.

The count is operational metadata only. It does not replace the full recommendation explanation in `/diagnosis`.

## MCP exposure

No new MCP reasoning implementation is introduced.

`atlerror://diagnosis/current` returns the same snapshot served by HTTP and therefore includes the embedded probe rankings automatically.

`atlerror://diagnosis/status` is backed by the same status projection as HTTP and therefore also reports the recommendation count.

This preserves the sibling transport architecture:

```text
                  -> diagnosis HTTP API
 diagnosis file -|
                  -> diagnosis MCP server
```

Neither transport calls `rank_probes()`.

## Example

With only TCP retransmissions observed, causal ranking currently prefers:

```text
1. hypothesis.network.packet_loss
2. hypothesis.network.packet_corruption
```

The embedded probe ranking recommends:

```text
probe.network.inspect_tcp_integrity_errors
```

An observed integrity error adds a causal-path match and a strong corruption prediction. An absent integrity error creates a conflict for the corruption path while not creating the same conflict for packet loss.

Because both outcomes change visible factors differently for the two candidates, this is a two-sided discriminator.

Once the integrity-error observation is already resolved, the same probe is removed from the recommendation set rather than being suggested again.

## Security and execution boundary

A recommendation is not an action.

This RFC does not add:

- MCP tools
- HTTP mutation endpoints
- automatic probe execution
- capability discovery
- shell command construction
- privilege escalation
- remediation actions

The projection may expose `requires`, `preferred_tools`, and `risk` from the catalog, but it does not infer that the environment can or should run the probe.

## Non-goals

This RFC does not introduce:

- probabilistic information gain
- entropy or expected-value scoring
- LLM-selected probes
- automatic retries
- probe scheduling
- remote execution
- remediation
- a second probe-ranking contract

## Consequences

A single live diagnosis read can now answer four questions from one semantic artifact:

```text
What is happening?
What are the plausible causes?
Why are they ranked this way?
What should be inspected next, and why?
```

This is the last reasoning projection needed before an end-to-end read-only demo can present the diagnostic loop as one continuously refreshed object.

## Future work

Useful next steps are:

1. merge independent telemetry sources into the same incident evidence state so one diagnosis can combine traces and metrics
2. build an end-to-end demo harness that drives telemetry through evidence, diagnosis, recommendation, HTTP, and MCP
3. only after the read-only loop is stable, define a permissioned active-probe execution boundary

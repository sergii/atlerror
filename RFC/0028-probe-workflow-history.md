# RFC 0028: Probe workflow history projection

Status: Accepted

## Summary

Atlerror now has a complete local probe workflow lifecycle:

```text
recommendation
  -> begin
  -> active or expired session
  -> finish or abandon
  -> evidence and diagnosis refresh
```

It also has crash recovery for partial persisted workflow state:

```text
partial session/binding pair
  -> workflow_recovery_required
  -> explicit fingerprint-bound reconciliation
```

The missing read-only layer is a machine-readable answer to a different question:

> What probe workflow has Atlerror already performed for this incident?

This RFC adds a deterministic projection over persisted local workflow artifacts and normal runtime evidence.

The projection is available from the CLI and, when runtime evidence is supplied to the MCP server, from:

```text
atlerror://probe-workflow/history
```

## Non-goal: append-only audit log

This first slice is not a durable append-only event store.

It reconstructs history from the current persisted state:

```text
probe execution session
+ MCP binding
+ abandonment marker
+ reconciliation marker
+ runtime evidence
        ↓
probe workflow history projection
```

The distinction is important. If an operator deletes all local workflow artifacts and removes the corresponding runtime evidence, this projection cannot reconstruct deleted history.

A durable append-only audit journal may be added later if operational requirements justify it.

## Contract

The document kind is:

```text
probe_workflow_history
```

with schema version `0.1`.

It contains:

- the incident ID;
- generation timestamp;
- counts by lifecycle status;
- deterministic session records.

The lifecycle states are:

```text
active
expired
completed
abandoned
reconciled_partial
recovery_required
```

### Active

A complete session and binding exist, no probe result evidence exists, no abandonment marker exists, and the baseline is still inside the configured lifecycle window.

### Expired

A complete session and binding exist, no probe result evidence exists, no abandonment marker exists, and the baseline is older than the configured lifecycle window.

### Completed

Normal runtime evidence contains exactly one result instance with the session ID.

The history record projects:

- evidence instance ID;
- observation ID;
- observed or absent state;
- observed timestamp;
- measurement payload.

If workflow files are missing but valid runtime evidence still identifies the probe session, history may retain a reduced `completed` record. It does not invent a target or baseline timestamp that no longer exists.

### Abandoned

A complete session and binding exist and the explicit abandonment marker exists. No diagnostic result is invented.

### Reconciled partial

A session/binding pair was incomplete and an explicit reconciliation marker records that the exact partial state was discarded.

The surviving partial files remain available for audit.

### Recovery required

A current partial session/binding pair is unresolved. The record exposes the same issue kind and SHA-256 fingerprint used by the planner and reconciliation tool.

This is an audit projection only. Reading history never performs reconciliation.

## Incident boundary

History is incident-scoped.

The runtime evidence document establishes the incident being projected. If the caller also supplies an expected incident ID, it must match exactly.

Complete sessions belonging to other incidents are ignored.

Unknown-owner partial state remains visible when the existing reconciliation scan determines that it can affect the current local workflow boundary.

## Determinism

For a fixed set of files, runtime evidence, lifecycle policy, and `as_of` timestamp, the projection is deterministic.

Session records are ordered by their known start time, then terminal time, then session ID.

The projection does not depend on filesystem iteration order.

## Fail-closed rules

The history projection refuses ambiguous or contradictory persisted state, including:

- more than one runtime evidence result for the same session;
- a completed evidence result and abandonment marker for the same complete session;
- invalid complete session/binding semantics;
- invalid terminal marker structure;
- requested incident mismatch.

Unresolved partial state is not an error because it is itself a first-class history state.

## Read-only semantics

Building history does not:

- execute a probe;
- read a probe measurement source;
- create runtime evidence;
- modify diagnosis revision;
- rerank hypotheses or probes;
- reconcile partial state;
- abandon a session;
- delete or rewrite persisted workflow artifacts.

The projection reads only existing workflow metadata and runtime evidence.

## MCP exposure

The MCP resource URI is:

```text
atlerror://probe-workflow/history
```

It is exposed only when the MCP process has an explicit runtime evidence path. This preserves the existing minimal read-only deployment that needs only a diagnosis snapshot.

The resource uses:

```text
ttlMs: 0
cacheScope: private
```

because incident workflow state can change immediately after begin, finish, abandon, or reconciliation.

Both modern and legacy resource reads use the same provider path. No workflow reasoning is duplicated in the MCP transport.

## Relationship to agent plan

The two projections answer different questions:

```text
agent plan
  -> what should happen next?

workflow history
  -> what has already happened?
```

History does not influence causal ranking, probe ranking, executor availability, or planner state.

## Security

The resource exposes metadata already persisted by the local Atlerror workflow plus probe result evidence already present in the incident evidence document.

It does not expose arbitrary files or accept a caller-controlled path.

All paths remain process configuration, not MCP resource parameters.

## Future work

Potential later work includes:

1. append-only workflow events for stronger durable audit guarantees;
2. retention and compaction policy;
3. pagination for long-running incidents;
4. correlation between workflow history and diagnosis revisions;
5. remote multi-host history aggregation once a remote deployment model exists.

None of these are required for the current local agent-native diagnostic loop.

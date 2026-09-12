# RFC 0025: Probe workflow reconciliation

Status: Accepted

## Summary

Two-phase read-only probe execution persists two local workflow documents:

1. the probe execution session containing the captured baseline;
2. the MCP binding connecting that session to the diagnosis target and semantic scope that caused it.

Each file is written atomically, but the pair is not one atomic filesystem transaction. A process crash can therefore happen after one file has been persisted and before the other exists.

This RFC makes that partial state explicit, fail-closed, and recoverable without creating runtime evidence.

## Problem

A normal persisted MCP probe workflow looks like:

```text
probe-session.<id>.json
probe-session.<id>.binding.json
```

Before this RFC, a crash between those writes could leave:

```text
probe-session.<id>.json
```

or, because of manual damage or older tooling, only:

```text
probe-session.<id>.binding.json
```

The first case is especially important. The session contains a real captured baseline, but without the binding Atlerror cannot know which diagnosis target authorized that baseline. Silently ignoring the file could allow a second probe to start while incomplete workflow state still exists.

## Decision

Atlerror now treats a missing member of the persisted session/binding pair as explicit partial workflow state.

The two recognized issue kinds are:

```text
orphan_session
orphan_binding
```

Pending-session discovery checks for unresolved partial state before projecting active or expired sessions.

If an unresolved issue belongs to the current incident, or its incident cannot be safely determined because the remaining file is unreadable, discovery fails closed.

This means `begin_recommended` cannot silently proceed through a crash artifact because it already depends on pending-session discovery while holding its target/scope filesystem claim.

## Reconciliation

A separate local command exposes the issue set:

```bash
python scripts/probe_workflow_reconciliation.py status \
  --session-dir /tmp/atlerror-demo/probe-sessions \
  --pretty
```

A partial state can be discarded explicitly:

```bash
python scripts/probe_workflow_reconciliation.py discard \
  --session-dir /tmp/atlerror-demo/probe-sessions \
  --session-id probe-session.0123456789abcdef \
  --pretty
```

Discarding does not delete the surviving session or binding file. Instead Atlerror writes:

```text
probe-session.<id>.reconciled.json
```

The marker records:

- session ID;
- detected incident ID when available;
- issue kind;
- exact SHA-256 fingerprint of the surviving partial file set;
- reconciliation time;
- resolution `discard_partial_state`.

The original files remain available for audit and debugging.

## Fingerprint binding

A reconciliation marker is valid only for the exact partial bytes that were reviewed.

If the surviving file changes after reconciliation, its fingerprint changes and the issue becomes unresolved again.

Therefore:

```text
reconciled once
  !=
ignore this pathname forever
```

This avoids using a stale marker to hide newly modified or replaced workflow state.

## Runtime evidence boundary

Reconciliation is local workflow recovery only.

It does not:

- create an observed or absent semantic observation;
- finish a probe;
- read the probe source again;
- modify runtime evidence;
- increment evidence revision;
- recompute causal ranking;
- recompute probe ranking;
- modify executor availability;
- perform remediation.

An orphaned baseline is intentionally discarded rather than interpreted as evidence because no trustworthy finish measurement exists.

## Incident filtering

When pending-session discovery runs for one incident, partial state is handled as follows:

- a partial with the same incident ID blocks discovery;
- a partial belonging to another known incident is ignored by that incident;
- a partial whose incident ID cannot be safely read blocks discovery.

Unknown ownership is fail-closed because silently assuming that an unreadable file belongs elsewhere could allow duplicate active work.

## Relationship to filesystem claims

RFC 0024 prevents concurrent cooperating processes from racing through `begin`, `finish`, and `abandon`.

This RFC addresses a different failure mode: process death between individually atomic persistence operations.

Together the layers are:

```text
thread RLock
    ↓
filesystem process claim
    ↓
atomic individual file writes
    ↓
partial-pair detection and reconciliation
```

Filesystem claims are released by the kernel when a process dies. Reconciliation handles workflow bytes that were already persisted before that death.

## Compatibility

Complete legacy session/binding pairs remain valid and unchanged.

Existing abandonment markers remain valid and unchanged.

No schema change is required for runtime evidence, diagnosis snapshots, causal ranking, probe ranking, or probe execution sessions.

## Failure behavior

If partial state is unresolved, Atlerror raises an explicit error instructing the operator to inspect reconciliation status and discard the partial state before continuing.

Atlerror does not guess a missing target, reconstruct a missing baseline, fabricate a binding, or infer a probe result.

## Tests

The integration coverage verifies:

- an orphan session blocks pending-session discovery;
- an orphan binding blocks pending-session discovery;
- explicit reconciliation preserves the surviving file;
- reconciled partial state no longer blocks discovery;
- reconciliation is retry-safe;
- a reconciliation marker is invalidated when the surviving bytes change;
- a complete session/binding pair remains a normal pending session;
- the status and discard CLI commands emit machine-readable JSON.

## Non-goals

This RFC does not add:

- a distributed transaction;
- a database;
- Redis;
- automatic deletion of partial files;
- automatic reconstruction of missing workflow documents;
- semantic evidence from incomplete probes;
- shell execution;
- remediation;
- non-read-only probe execution.

## Future work

A later slice may expose reconciliation state directly through the agent-plan projection so an MCP client receives a structured recovery action instead of a resource-read failure while partial workflow state exists.

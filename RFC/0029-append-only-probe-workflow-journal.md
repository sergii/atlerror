# RFC 0029: Append-only probe workflow journal

- Status: Accepted
- Date: 2026-09-13

## Summary

Atlerror now records successful local probe workflow transitions in an append-only JSONL journal stored next to probe session artifacts:

```text
<probe-session-dir>/probe-workflow.journal.jsonl
```

The first event types are:

```text
begin
finish
abandon
reconcile
```

This journal complements the reconstructed workflow history introduced in RFC 0028. RFC 0028 remains useful for projecting current persisted state, while this RFC adds a durable ordered record of successful MCP workflow transitions.

## Motivation

Before this change, workflow history was reconstructed from current files:

```text
probe sessions
+ MCP bindings
+ abandonment markers
+ reconciliation markers
+ runtime evidence
```

That reconstruction is intentionally truthful, but it cannot remember an action after every artifact representing that action has been deleted.

A local audit trail needs a different property: once a workflow transition has been recorded, later workflow-state cleanup must not rewrite prior audit events.

## Event contract

Every journal line is one compact JSON object validated by `schema/probe-workflow-event.schema.json`.

Each event contains:

```text
schema_version
kind
sequence
event_type
recorded_at
incident_id
session_id
data
previous_hash
event_hash
```

`sequence` starts at 1 and increases by exactly one across the journal file.

`event_hash` is SHA-256 over the canonical JSON representation of the event excluding `event_hash` itself.

`previous_hash` is null for sequence 1 and otherwise must equal the preceding event's `event_hash`.

This produces a simple hash chain:

```text
E1(hash=h1, previous=null)
E2(hash=h2, previous=h1)
E3(hash=h3, previous=h2)
```

The chain is not a cryptographic signature and does not prove who wrote the file. It does make accidental or unsophisticated modification, deletion-in-the-middle, reordering, and content changes detectable during verification.

## Transition semantics

### begin

Recorded after the baseline session and MCP binding have been persisted.

The event data captures immutable workflow facts such as target, probe, exact semantic scope, start/expiry timestamps, diagnosis revision, and the captured baseline.

### finish

Recorded after probe evidence has been persisted and the diagnosis snapshot recomputed.

The event data contains the evidence instance ID and immutable observation result, including state, observation timestamp, and measurement.

### abandon

Recorded after the explicit abandonment marker has been persisted.

No runtime evidence is created.

### reconcile

Recorded after an exact-fingerprint partial-workflow reconciliation marker has been persisted.

The event preserves issue kind, fingerprint, resolution, timestamp, and affected artifact names.

## Ordering and concurrency

Journal append uses the same filesystem-claim mechanism as the active probe workflow.

A single process-safe claim serializes cooperative writers around:

```text
read + verify existing chain
choose next sequence
compute previous_hash
generate event_hash
append one JSONL record
fsync
```

The claim is keyed to the fixed local workflow journal rather than a specific probe session.

This prevents two cooperating Atlerror processes from assigning the same sequence or racing the hash-chain head.

## Retry behavior

There may be at most one event of a given transition type for a given probe session.

A retry with the same semantic transition payload returns the existing event and reports:

```text
already_recorded: true
```

A second event with the same session and event type but different immutable data fails closed.

This is particularly important for retry-safe `finish`, `abandon`, and `reconcile` MCP calls.

## Verification

The journal can be inspected directly:

```bash
python scripts/probe_workflow_journal.py \
  --session-dir /tmp/atlerror-probe-sessions \
  --pretty
```

Verification checks:

1. every non-empty line is valid JSON;
2. every event satisfies the event schema;
3. sequences are contiguous and start at 1;
4. every `previous_hash` matches the prior event;
5. every `event_hash` matches canonical event content.

Any mismatch fails closed.

An optional incident filter changes which verified events are returned, but verification always covers the complete global journal first. The reported `head_hash` therefore remains the actual file head.

## MCP integration

The existing opt-in MCP mutation controller records the journal automatically after successful workflow transitions.

Tool results expose a compact journal acknowledgement:

```json
{
  "journal_event": {
    "sequence": 12,
    "event_hash": "...",
    "previous_hash": "...",
    "already_recorded": false
  }
}
```

No new mutation tool is introduced.

The journal is not written by diagnosis reads, capability discovery, history reads, or agent-plan reads.

## Failure semantics

Workflow state is committed before its corresponding audit event is appended.

If the workflow mutation succeeds but journal append fails, the MCP call fails with an explicit message that the transition committed but audit append failed.

This first slice deliberately does not pretend that multiple filesystem files plus the JSONL append form a transactional database commit.

Existing session, evidence, and recovery projections remain the source of truth for current workflow state. The journal is an append-only audit trail of transitions that were successfully recorded.

A later RFC may add startup reconciliation/backfill for a committed workflow transition whose process crashed before the journal append.

## Security boundary

The journal does not expand execution authority.

It does not:

- execute a probe;
- read a probe measurement source on its own;
- select a probe;
- create diagnosis evidence independently;
- change causal or probe ranking;
- run shell commands;
- accept arbitrary journal paths through MCP;
- sign events with an external identity.

It only records successful transitions already authorized by the existing MCP workflow boundary.

## Non-goals

This RFC does not introduce:

- remote or multi-host journal replication;
- cryptographic signing or TPM-backed attestation;
- immutable object storage;
- a database or event broker;
- journal compaction;
- arbitrary user-authored audit events;
- workflow state reconstruction exclusively from events;
- automatic repair of missing journal events after a crash.

## Next step

The next coherent layer is to merge this verified event stream into `atlerror://probe-workflow/history` so history can retain completed, abandoned, and reconciled workflow facts even after the corresponding mutable session artifacts are no longer present.

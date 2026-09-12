# RFC 0024: Probe filesystem claims for cross-process safety

- Status: Accepted
- Date: 2026-09-12

## Summary

Atlerror active read-only probe execution now uses process-safe filesystem claims in addition to the existing in-process `RLock`.

The goal is to close races between multiple local MCP processes that share the same diagnosis snapshot, runtime evidence file, and probe session directory.

The first slice protects three mutation boundaries:

```text
begin_recommended
  -> target + exact normalized scope claim

finish
  -> session claim
  -> incident mutation claim

abandon
  -> session claim
```

The semantic ranking model is unchanged.

## Problem

Before this RFC, `RecommendedProbeToolController` serialized mutation only inside one Python process with `threading.RLock`.

Two independent MCP processes could still do this:

```text
process A                      process B
---------                      ---------
check pending: none            check pending: none
capture baseline               capture baseline
write session A                write session B
```

That violates the lifecycle invariant introduced in RFC 0023:

```text
at most one unfinished session per target + exact semantic scope
```

The same issue exists during `finish`: two processes can read the same old runtime-evidence document and both attempt to replace it, creating a lost-update risk even if each individual file replacement is atomic.

## Decision

Use advisory exclusive file locks backed by persistent local lock files under:

```text
<probe-session-dir>/.claims/
```

Claim filenames are deterministic hashes of a canonical JSON identity. User-controlled identifiers are never interpolated directly into filesystem paths.

The implementation uses POSIX `flock(2)` through Python's standard-library `fcntl` module.

### Why `flock`

`flock` gives the properties needed for this local-host slice:

- exclusion across independent processes;
- automatic kernel release when a process exits or crashes;
- no Redis, database, daemon, or distributed coordinator;
- no stale lock that remains logically held after process death;
- persistent lock inode, so the lock file itself never needs unsafe unlink/recreate coordination.

The lock file contains best-effort owner metadata for debugging, but file contents never determine ownership. The kernel lock is authoritative.

## Claims

### Target-scope claim

`begin_recommended` claims:

```json
{
  "incident_id": "...",
  "target": "observation.network.tcp_retransmissions",
  "scope": {"...": "exact normalized semantic scope"}
}
```

The controller then reloads the snapshot and evidence while holding the claim, revalidates the current recommendation, checks pending sessions again, captures the baseline, and persists the new session and binding.

This closes the check-then-create race across processes.

### Session claim

`finish` and `abandon` both claim the specific probe session.

Therefore these transitions cannot mutate the same session concurrently:

```text
finish vs finish
finish vs abandon
abandon vs abandon
```

A contending process receives a transient fail-closed tool error and may retry.

### Incident mutation claim

`finish` also claims the incident mutation identity while composing runtime evidence and replacing the diagnosis snapshot.

This serializes cooperating Atlerror MCP writers for the same incident and prevents two probe completions from independently replacing the same old evidence state.

Different incidents remain independent.

## Lock ordering

When an operation needs multiple claims, claims are sorted by deterministic claim path before acquisition.

This keeps lock ordering stable and avoids order-dependent nested acquisition behavior.

Claims are non-blocking. Atlerror fails closed rather than waiting indefinitely for another process.

## Crash behavior

Claim files are intentionally persistent and are not deleted on release.

If a process terminates with `os._exit`, is killed, or crashes, the operating system releases its advisory lock automatically. A later process can acquire the same persistent lock file immediately.

Stale owner metadata can remain in the file, but it is informational only and is overwritten by the next successful holder.

## Platform boundary

This first slice requires POSIX advisory file locking.

If `fcntl` is unavailable, active probe mutation fails closed with an explicit claim-unavailable error. Atlerror does not silently fall back to in-process locking because that would reintroduce the race.

This is a local-filesystem coordination mechanism, not a distributed lease protocol. Network filesystems with non-local or implementation-specific lock semantics are outside the current guarantee.

## Relationship to lifecycle state

Filesystem claims are not persisted probe lifecycle state.

They do not create or change:

- runtime evidence;
- causal ranking;
- probe ranking;
- executor availability;
- `active` / `expired` session classification;
- abandonment markers;
- evidence revision.

They only serialize mutation windows.

## Read paths

Reading diagnosis, capability resources, agent-plan resources, and persisted session state does not acquire mutation claims.

The read-only semantic surface remains unaffected.

## Safety invariants

1. At most one cooperating process may be inside the `begin` critical section for the same incident + target + exact scope.
2. At most one cooperating process may mutate one probe session at a time.
3. At most one cooperating process may replace runtime evidence for one incident through MCP probe completion at a time.
4. Process death releases claims without manual cleanup.
5. Claim metadata is never treated as evidence or authorization.
6. Claim contention never causes Atlerror to execute a different probe.
7. Active execution remains opt-in at the MCP process level.

## Tests

The integration suite uses separate spawned Python processes to verify:

- the same claim is mutually exclusive across processes;
- a process crash releases the kernel claim;
- different target/scope claims can coexist;
- a simultaneous `begin_recommended` race persists exactly one pending session;
- simultaneous `finish` calls create exactly one probe evidence instance and one diagnosis revision increment.

## Non-goals

This RFC does not add:

- distributed coordination;
- Redis or database locks;
- remote leases;
- leader election;
- automatic retries;
- long-running lock waiting;
- shell execution;
- remediation;
- non-read-only probe execution;
- cross-host active-probe scheduling.

## Follow-up

The next concurrency layer should address ownership and topology when active probes eventually run across multiple hosts. At that point local `flock` is deliberately insufficient and a distributed execution contract should be designed explicitly rather than extending this local claim mechanism beyond its guarantees.

# RFC 0023: Probe session lifecycle policy

Status: accepted

## Summary

Atlerror read-only probe execution is intentionally two-phase:

```text
begin
  -> capture baseline
  -> external controlled workload
finish
  -> capture comparison value
  -> runtime evidence
```

Once probe sessions became visible in the agent plan, an unfinished session could remain in `probe_in_progress` forever and callers could accidentally begin another session for the same diagnosis target and semantic scope.

This RFC defines a small workflow lifecycle around persisted probe sessions without changing causal reasoning, semantic probe ranking, executor availability, or runtime evidence semantics.

## Goals

The lifecycle policy must:

1. prevent accidental duplicate unfinished sessions for one `(target, exact semantic scope)`;
2. make the time validity of a captured baseline explicit;
3. refuse to finish a baseline that is too old;
4. provide a safe local abandon operation that creates no diagnostic evidence;
5. keep persisted session and binding files for auditability;
6. let the agent plan recover after an abandoned or expired workflow;
7. remain deterministic and reconstructable after process restart.

## Session states

A persisted session that has neither result evidence nor an abandonment marker is pending.

Pending sessions have one of two lifecycle states:

```text
active
expired
```

`active` means the baseline remains inside the configured execution window.

`expired` means the configured maximum age has elapsed. Expiration is workflow policy, not diagnostic evidence and not a statement about whether the underlying hypothesis is true.

The first policy uses:

```text
DEFAULT_SESSION_MAX_AGE_SECONDS = 900
```

or 15 minutes.

This value belongs to the local executor workflow. It is not part of the canonical Atlerror ontology and does not affect ranking.

The controller accepts an explicit maximum age for deterministic tests and future host policy configuration.

## Expiration boundary

For a session with `started_at`, the execution deadline is:

```text
expires_at = started_at + max_session_age
```

The session becomes expired when:

```text
as_of >= expires_at
```

The persisted probe execution session itself remains unchanged. `expires_at` and `lifecycle_state` are projections derived from the session start time and current lifecycle policy.

## Duplicate prevention

Before `atlerror.probe.begin_recommended` captures another baseline, the controller resolves the current diagnosis target and exact normalized semantic scope, then discovers unfinished sessions for the same incident.

If a pending session already exists for the same:

```text
target + exact normalized scope
```

begin fails closed.

This applies to both active and expired sessions.

An expired session must first be abandoned. This avoids silently accumulating abandoned baselines or creating competing sessions whose results could be confused by an agent.

Different targets or different exact scopes remain independent.

## Finishing sessions

`atlerror.probe.finish` keeps its existing retry-safe completed-result behavior.

If runtime evidence for the session already exists, a repeated finish may return the already completed result without reading the probe source again.

For an unfinished session:

- active session -> finish may execute normally;
- expired session -> finish is refused before the comparison source is read;
- abandoned session -> finish is refused.

The caller must abandon an expired session and begin a new recommendation if the diagnosis still calls for the probe.

## Abandon operation

The opt-in MCP tool surface adds:

```text
atlerror.probe.abandon
```

Input:

```json
{
  "sessionId": "probe-session.0123456789abcdef"
}
```

Abandonment writes a small local marker:

```json
{
  "schema_version": "0.1",
  "kind": "mcp_probe_abandonment",
  "session_id": "probe-session.0123456789abcdef",
  "incident_id": "incident.example",
  "abandoned_at": "2026-09-12T13:00:00Z",
  "reason": "operator_abandoned"
}
```

The marker does not delete the session or binding files.

Abandon is idempotent. Repeating it returns the existing abandonment time.

A completed session cannot be abandoned because result evidence already exists and is part of incident history.

## Evidence boundary

Abandonment is workflow state, not runtime evidence.

Therefore abandon does not:

- add an observation;
- add an absent observation;
- change evidence revision;
- recompute causal ranking;
- recompute probe ranking;
- change executor availability;
- imply probe success or failure.

The next agent-plan read simply stops projecting the abandoned session and reveals the current diagnosis action again.

## Agent plan

The agent plan gains:

```text
probe_session_expired
```

An active pending session remains:

```text
probe_in_progress
  -> atlerror.probe.finish
```

An expired pending session becomes:

```text
probe_session_expired
  -> atlerror.probe.abandon
```

The session projection includes:

```text
started_at
expires_at
lifecycle_state
```

alongside the existing probe ID, executor ID, originating diagnosis revision, and current-recommendation match flag.

If active MCP probe tools are not enabled, both finish and abandon remain visible but unauthorized:

```text
allowed = false
requires_opt_in = true
fallback = enable_readonly_probe_tools
```

Thus:

```text
session exists != operation is authorized
```

continues to hold.

## Restart behavior

Lifecycle state is reconstructable from persisted local state:

```text
session file
+ binding file
+ abandonment marker if present
+ runtime evidence
+ current time
+ max session age policy
```

No in-memory task registry is required.

A process restart therefore does not lose whether a session is unfinished, completed, abandoned, active, or expired.

## Safety properties

The lifecycle policy keeps the existing active-diagnostics boundary:

- no shell commands;
- no arbitrary subprocesses;
- no traffic generation;
- no remediation;
- no dynamic executor loading;
- no execution of non-read-only canonical probes;
- no implicit authorization from capability availability;
- no evidence generated by abandonment.

`atlerror.probe.abandon` mutates only local workflow metadata and is available only when the existing read-only probe tool surface has been explicitly enabled.

## Non-goals

This RFC does not add:

- distributed locks across multiple MCP processes;
- remote session coordination;
- automatic abandonment;
- automatic retry;
- background timers;
- probe execution scheduling;
- state-changing or high-risk probes;
- remediation actions;
- probabilistic session validity.

Cross-process locking may be considered if Atlerror later supports multiple concurrent writers against one session directory.

## Tests

The lifecycle integration tests verify that:

1. a second begin for the same target and scope is refused;
2. a session transitions from active to expired at the configured boundary;
3. expired sessions are shown as `probe_session_expired` in the agent plan;
4. expired finish is refused without adding runtime evidence;
5. abandon is idempotent;
6. abandon adds no runtime evidence and changes no diagnosis revision;
7. an abandoned session disappears from the pending plan;
8. a fresh session can be started after abandonment;
9. finishing an abandoned session is refused.

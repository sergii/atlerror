# RFC 0027: MCP partial workflow reconciliation tool

Status: Accepted

## Summary

Atlerror exposes one narrowly scoped MCP mutation for recovering from partial persisted probe workflow state:

```text
atlerror.probe.reconcile_partial
```

The tool is available only when the existing opt-in MCP probe-tool boundary is enabled. It can mark one current `orphan_session` or `orphan_binding` state as explicitly discarded, using the exact fingerprint published by the current agent plan.

This closes the recovery loop introduced by RFC 0025 and RFC 0026:

```text
partial persisted workflow
  -> workflow_recovery_required
  -> exact session + fingerprint
  -> atlerror.probe.reconcile_partial
  -> reconciliation marker
  -> normal agent plan resumes
```

## Motivation

Before this RFC, Atlerror could detect partial workflow state and project it into `atlerror://diagnosis/agent-plan`, but recovery still required an external CLI invocation.

That left an agent-native workflow incomplete. An MCP client could understand that recovery was required, but it could not complete the recovery through the same explicitly authorized transport.

The missing operation must remain much narrower than a generic filesystem mutation. It must not let an agent choose arbitrary files, delete state, invent evidence, or reconcile a stale condition that has changed since the plan was read.

## Tool contract

The tool accepts exactly:

```json
{
  "sessionId": "probe-session.0123456789abcdef",
  "fingerprint": "<64 lowercase hex characters>"
}
```

The `sessionId` and `fingerprint` are copied directly from the current `workflow_recovery_required` agent-plan step.

The tool does not accept:

- a file path
- an incident override
- an issue kind override
- a resolution mode
- a shell command
- arbitrary JSON to persist

## Agent-plan projection

When recovery is required, the plan keeps the recovery state:

```text
state  = workflow_recovery_required
reason = partial_probe_workflow_state
```

and now also exposes:

```text
operation = atlerror.probe.reconcile_partial
arguments = { sessionId, fingerprint }
```

If opt-in MCP tools are enabled:

```text
allowed         = true
requires_opt_in = false
fallback        = none
```

If they are disabled:

```text
allowed         = false
requires_opt_in = true
fallback        = reconcile_partial_workflow
```

The existing CLI remains a valid manual fallback.

## Exact fingerprint binding

The fingerprint is a SHA-256 digest derived from the issue kind and exact surviving partial-file bytes.

Before writing a reconciliation marker, the tool rescans current state and requires the supplied fingerprint to match the current issue exactly.

Therefore:

```text
agent plan read
  -> partial file changes
  -> stale tool call
  -> refusal
```

The caller must refresh the agent plan and use the new fingerprint.

This prevents a stale plan from authorizing recovery of changed state.

## Incident binding

The tool reads the current validated diagnosis snapshot and only accepts partial state that belongs to that incident, or partial state whose incident ownership cannot be determined.

A partial workflow that explicitly belongs to a different incident is refused.

Unknown ownership remains fail-closed in planning, but an operator or agent with explicit tool opt-in may discard that exact byte-level partial state using its fingerprint.

## Cross-process safety

Reconciliation uses a filesystem-backed claim keyed by session ID:

```text
purpose  = workflow_recovery
identity = { session_id }
```

The claim uses the same POSIX advisory locking layer as active probe workflow operations.

Two cooperating Atlerror processes cannot reconcile the same session concurrently.

The reconciliation marker itself remains retry-safe.

## Idempotency

A repeated call with the same session ID and exact fingerprint returns the existing reconciliation result with:

```text
already_reconciled = true
```

A repeated call with a different fingerprint is refused.

The original partial files remain untouched in both cases.

## Safety boundary

The tool never:

- reads the probe measurement source
- executes a probe
- creates runtime evidence
- changes evidence revision
- recomputes causal ranking
- recomputes probe ranking
- deletes the surviving partial file
- fabricates a missing session or binding
- accepts an arbitrary path
- runs a subprocess
- executes a shell command

Its only mutation is the existing reconciliation marker defined by RFC 0025.

## MCP exposure

The tool is included in the existing opt-in tool catalog alongside:

```text
atlerror.probe.begin_recommended
atlerror.probe.finish
atlerror.probe.abandon
atlerror.probe.reconcile_partial
```

No additional MCP capability family is introduced.

The MCP server version is `0.7.0`.

## Validation

Tests cover:

- tool discovery and schema
- exact agent-plan fingerprint binding
- successful reconciliation
- plan resumption after reconciliation
- no runtime-evidence or diagnosis mutation
- stale fingerprint refusal
- retry idempotency
- explicit rejection of another incident's partial state
- exact-fingerprint recovery of unknown-owner partial state

## Non-goals

This RFC does not add:

- automatic reconciliation
- arbitrary filesystem cleanup
- deletion of orphan files
- remote recovery coordination
- generic repair tools
- remediation commands
- probabilistic recovery policy

## Future work

A later slice may add a compact workflow history or audit projection so agents can explain which probe sessions were completed, abandoned, or reconciled without scanning raw local marker files.

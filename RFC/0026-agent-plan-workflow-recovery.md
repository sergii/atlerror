# RFC 0026: Agent-plan workflow recovery state

Status: Accepted

## Summary

Project unresolved partial persisted probe workflow state directly into the existing MCP agent-plan resource.

A process crash can leave only one member of the logical probe workflow pair:

```text
probe-session.<id>.json
probe-session.<id>.binding.json
```

RFC 0025 made this state detectable and explicitly reconcilable. This RFC makes it first-class agent context instead of allowing the agent-plan resource to fail with an internal projection error.

## Decision

`atlerror://diagnosis/agent-plan` becomes recovery-aware.

When there is no unresolved partial workflow state, the existing planner behavior is unchanged.

When unresolved partial workflow state exists for the current incident, or when the partial file cannot safely prove incident ownership, the planner returns only recovery steps and suppresses normal diagnostic actions until the partial state is explicitly reconciled.

The new planner state is:

```text
workflow_recovery_required
```

with reason:

```text
partial_probe_workflow_state
```

A recovery step contains the exact persisted issue identity:

```json
{
  "scope": null,
  "target": null,
  "state": "workflow_recovery_required",
  "reason": "partial_probe_workflow_state",
  "operation": null,
  "allowed": false,
  "requires_opt_in": false,
  "fallback": "reconcile_partial_workflow",
  "session": null,
  "recovery": {
    "session_id": "probe-session.0123456789abcdef",
    "incident_id": "incident.demo.checkout.stripe",
    "issue_kind": "orphan_binding",
    "fingerprint": "...",
    "files": ["probe-session.0123456789abcdef.binding.json"],
    "resolution": "discard_partial_state"
  }
}
```

The summary adds:

```text
workflow_recovery_required
```

Normal plans report zero. Recovery-gated plans report the number of unresolved partial workflow issues.

## Why recovery gates the plan

The existing probe workflow already fails closed when partial state exists. Returning a normal `actionable` planner step at the same time would be misleading because `begin_recommended` would refuse to continue.

Therefore unresolved recovery state is a planner-level gate:

```text
partial workflow exists
        ↓
WORKFLOW_RECOVERY_REQUIRED
        ↓
explicit reconciliation
        ↓
normal agent plan resumes
```

The semantic diagnosis snapshot is not modified. Only the action projection is gated.

## Why recovery is not a diagnosis target

An orphan session or binding is operational workflow state, not a semantic observation, hypothesis, or causal node. Recovery steps therefore use:

```text
target = null
scope = null
```

and carry their identity in the dedicated `recovery` object.

This avoids inventing a fake ontology concept merely to fit planner syntax.

## Incident ownership

A partial workflow can expose either:

- a known `incident_id` matching the current diagnosis, or
- no safely readable incident identity.

Unknown ownership remains fail-closed. It is projected into the current agent plan rather than ignored because silently starting another probe could overlap with an unfinished workflow whose ownership cannot be proven.

A partial workflow explicitly belonging to another incident is not projected into the current incident plan.

## MCP behavior

The existing resource URI remains:

```text
atlerror://diagnosis/agent-plan
```

No new MCP resource is introduced.

The MCP adapter scans partial workflow state before requesting pending probe sessions. If recovery is required, it skips pending-session discovery and builds the recovery-gated plan. This prevents the lower-level fail-closed exception from turning into an MCP internal error.

The MCP server continues to read the same persisted diagnosis snapshot and does not rerun causal or probe ranking.

## Reconciliation action

This slice intentionally does not add a new MCP mutation tool.

The machine-readable fallback is:

```text
reconcile_partial_workflow
```

The existing explicit local recovery command remains authoritative:

```bash
python scripts/probe_workflow_reconciliation.py discard \
  --session-dir /tmp/atlerror-demo/probe-sessions \
  --session-id probe-session.0123456789abcdef \
  --pretty
```

A future RFC may expose this exact discard operation as a narrowly scoped MCP tool. That tool must preserve the RFC 0025 invariants and must not infer or generate runtime evidence.

## Safety invariants

Agent-plan recovery projection never:

- modifies causal ranking
- modifies probe ranking
- reads a probe source
- creates runtime evidence
- invents a missing target or baseline
- increments evidence revision
- recomputes the diagnosis snapshot
- deletes partial workflow files
- auto-reconciles a partial workflow

The projection reports persisted state only.

## Compatibility

The agent-plan schema remains version `0.1`.

Existing normal planner steps remain valid. The schema adds optional support for:

- `workflow_recovery_required` state
- `partial_probe_workflow_state` reason
- `reconcile_partial_workflow` fallback
- nullable target for recovery-only steps
- optional `recovery` object
- optional recovery count in the summary

Direct users of the older `build_agent_plan()` function remain compatible because the new summary count is optional in the schema. MCP uses the new recovery-aware projection wrapper and always includes the recovery summary count.

## Non-goals

This RFC does not add:

- automatic repair
- deletion of workflow files
- runtime evidence synthesis
- probe re-execution
- an MCP reconciliation tool
- distributed workflow recovery
- database transactions
- new causal semantics
- new probe ranking semantics

## Future work

The next coherent slice can expose `discard_partial_state` as an explicit MCP tool, bound to the exact current recovery fingerprint and protected by the existing filesystem claim layer. After that, the planner can make the recovery step directly actionable without weakening fail-closed behavior.

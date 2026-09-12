# RFC 0022: Agent plan active probe-session state

- Status: Accepted
- Date: 2026-09-12

## Summary

Atlerror's agent plan already projects the next semantic diagnostic action from:

```text
diagnosis
  -> next-probe ranking
  -> host execution annotation
  -> process-level execution opt-in
  -> agent plan
```

This RFC extends that projection with persisted unfinished read-only probe sessions.

After `atlerror.probe.begin_recommended` captures a baseline, the agent plan must stop suggesting another begin for the same diagnostic step and instead expose that a probe is already in progress and that the next workflow operation is `atlerror.probe.finish`.

The resulting loop becomes:

```text
ACTIONABLE
  -> begin recommended probe
  -> baseline captured

PROBE_IN_PROGRESS
  -> run controlled workload externally
  -> finish probe session
  -> runtime evidence
  -> recompute diagnosis
  -> new agent plan
```

## Motivation

A next-action projection is incomplete if it describes only the state before execution starts.

Before this RFC, an agent could read:

```text
state: actionable
operation: atlerror.probe.begin_recommended
```

call the begin tool successfully, and then read the same agent plan again because the diagnosis snapshot itself does not change until the probe is finished.

The persisted probe session already represents meaningful workflow state:

- a specific semantic probe was selected from the current diagnosis;
- an exact semantic scope was bound;
- a baseline was captured;
- a concrete executor was pinned;
- the session is waiting for a controlled external workload and a finish operation.

The planner should expose that state rather than ask agents to remember it outside Atlerror.

## Design boundary

Active probe-session state is workflow state, not diagnostic evidence.

It must not modify:

- causal ranking;
- hypothesis order;
- next-probe ranking;
- probe risk;
- runtime evidence;
- executor availability annotations.

The session overlay happens only after the existing diagnosis-derived agent-plan step is built.

```text
semantic diagnosis
      ↓
semantic next probe
      ↓
host execution annotation
      ↓
base agent-plan step
      ↓
persisted unfinished probe session
      ↓
workflow-state overlay
```

## Session discovery

`scripts/probe_session_state.py` reads persisted MCP probe artifacts from the configured probe-session directory.

For each `probe-session.<id>.binding.json` it validates the corresponding persisted execution session and verifies:

- binding structure and version;
- session ID consistency;
- incident consistency;
- probe consistency;
- normalized semantic scope consistency;
- executor/session semantics through the existing probe-session validator.

The runtime evidence document is then searched for an evidence instance carrying the same `session_id` label.

If such evidence already exists, the session is complete and is not projected as active.

If no such evidence exists, the session is projected as unfinished.

Discovery is read-only. It does not read a new system counter, capture a baseline, finish a probe, mutate evidence, or rewrite the session files.

## Agent plan state

The agent-plan state set gains:

```text
probe_in_progress
```

A session-backed step contains:

```yaml
state: probe_in_progress
reason: ready_to_finish_probe
operation: atlerror.probe.finish
arguments:
  sessionId: probe-session.0123456789abcdef
allowed: true
requires_opt_in: false
session:
  id: probe-session.0123456789abcdef
  probe_id: probe.network.inspect_tcp_integrity_errors
  executor_id: executor.linux.proc_net_snmp.tcp_inerrs
  started_at: "2026-09-11T16:31:20Z"
  diagnosis_revision: 1
  matches_current_recommendation: true
```

The session object intentionally excludes captured baseline values. An agent needs workflow identity and provenance, not the raw baseline, to know what to do next.

## Current recommendation can change

A probe session was authorized against the diagnosis revision recorded at begin time. The diagnosis may later change because of independent evidence while that session remains unfinished.

Atlerror therefore keeps two concepts separate:

```text
session.probe_id
recommended_probe
```

`matches_current_recommendation` makes the relationship explicit.

An unfinished session remains visible even if the current diagnosis no longer recommends the same probe. Finishing an already-started read-only measurement still produces ordinary runtime evidence and does not force the old probe back into the semantic ranking.

## Sessions without a current diagnosis step

An unfinished persisted session may outlive the diagnosis entry that originally created it, for example after freshness transitions or unrelated evidence changes.

The planner still emits a `probe_in_progress` step using the session's bound target and scope.

This prevents a real outstanding workflow from disappearing merely because the current diagnosis projection changed.

## Multiple sessions

The first slice does not silently collapse multiple unfinished sessions for the same target and scope.

If multiple valid persisted sessions exist, each is emitted as its own deterministic `probe_in_progress` step ordered by session ID.

This is preferable to hiding work that was actually started. A future policy may prevent redundant begins earlier in the lifecycle, but session projection itself remains lossless.

## Authorization

An existing probe session does not bypass process-level tool opt-in.

When read-only probe tools are enabled:

```yaml
state: probe_in_progress
reason: ready_to_finish_probe
operation: atlerror.probe.finish
allowed: true
requires_opt_in: false
```

If an active session projection is supplied while execution is disabled:

```yaml
state: probe_in_progress
reason: probe_in_progress_execution_disabled
operation: atlerror.probe.finish
allowed: false
requires_opt_in: true
fallback: enable_readonly_probe_tools
```

Therefore:

```text
session exists
  !=
finish is authorized
```

## MCP behavior

`atlerror://diagnosis/agent-plan` remains the only new consumer-facing projection required for this slice.

When the MCP server is configured with the real `RecommendedProbeToolController`, it automatically reads unfinished sessions from that controller's configured session directory and runtime evidence file.

No separate session mutation resource is introduced.

The resource remains read-only and zero-TTL/private because both diagnosis and active workflow state can change immediately.

## Completion semantics

The existing finish tool is retry-safe.

After `atlerror.probe.finish` appends the probe result to runtime evidence, session discovery sees the evidence instance labeled with that session ID and no longer reports the session as active.

The next agent-plan read therefore returns to normal diagnosis-derived planning using the recomputed evidence revision.

## Failure behavior

Session discovery fails closed for malformed or inconsistent persisted artifacts.

Examples include:

- invalid binding JSON;
- unsupported binding shape;
- binding/session ID mismatch;
- probe mismatch;
- scope mismatch;
- multiple runtime evidence instances for one session.

The MCP agent-plan resource reports an internal projection failure rather than silently omitting uncertain workflow state.

Sessions belonging to another incident are ignored because a shared session directory may contain artifacts from multiple incident-specific executions.

## Non-goals

This RFC does not add:

- automatic workload generation;
- automatic finish calls;
- session cancellation;
- session timeout policy;
- session garbage collection;
- arbitrary shell execution;
- dynamic executors;
- non-read-only probes;
- remediation;
- a new causal or probe ranking algorithm.

## Result

The agent-visible diagnostic lifecycle is now stateful across the full read-only probe loop:

```text
ACTIONABLE
  -> begin

PROBE_IN_PROGRESS
  -> controlled workload
  -> finish

runtime evidence
  -> recompute
  -> next plan
```

Atlerror now tells an agent not only what should start next, but also when diagnostic work is already underway and how to complete it safely.

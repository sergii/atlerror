# RFC 0021: Agent plan projection

- Status: Accepted
- Date: 2026-09-12

## Summary

Atlerror now has enough separate layers to answer four different questions without collapsing them into one score or one opaque agent decision:

```text
What might be causing the symptom?
  -> causal ranking

What observation would best discriminate the current candidates?
  -> probe ranking

Can the recommended probe run on this host?
  -> probe execution annotation

Given the current MCP process policy, what can an agent do next?
  -> agent plan projection
```

This RFC adds the final projection.

The projection is intentionally small. It does not rerun diagnosis, rerank hypotheses, rerank probes, infer host capabilities, execute a probe, or decide between independent diagnosis targets.

## Resource

The MCP server exposes:

```text
atlerror://diagnosis/agent-plan
```

The resource is available in both resource-only mode and active probe-tool mode.

It is derived from the already validated persisted diagnosis snapshot plus one MCP process-local fact:

```text
are read-only probe tools explicitly enabled for this MCP process?
```

Modern MCP reads use:

```text
ttlMs: 0
cacheScope: private
```

because the projection depends on the current diagnosis revision and current process opt-in state.

## Contract

The top-level document is:

```yaml
schema_version: "0.1"
kind: agent_plan
incident_id: incident.example
evidence_revision: 12
active_execution_enabled: true
steps: []
summary:
  actionable: 0
  blocked: 0
  no_executor: 0
  no_probe_needed: 0
  no_discriminating_probe: 0
```

Each diagnosis entry produces exactly one compact step.

The plan preserves the diagnosis partition scope so an agent has the exact arguments needed by `atlerror.probe.begin_recommended` when that operation is allowed.

## States

### `actionable`

The current semantic next probe exists, has a registered executor, is executable on this host, and the MCP process was started with explicit read-only probe-tool opt-in.

```yaml
state: actionable
reason: ready_to_begin_recommended
recommended_probe: probe.network.inspect_tcp_integrity_errors
registered: true
executable_here: true
executor_id: executor.linux.proc_net_snmp.tcp_inerrs
operation: atlerror.probe.begin_recommended
allowed: true
requires_opt_in: false
fallback: none
```

The projection includes the exact `target` and normalized `scope` arguments for the tool.

### `blocked`

Two different conditions use the same high-level blocked state but different machine-readable reasons.

If the executor is locally unavailable:

```yaml
state: blocked
reason: executor_unavailable
allowed: false
fallback: restore_executor_availability
unavailable_reason: "source does not exist: /proc/net/snmp"
```

If the executor is available but active tools were not explicitly enabled:

```yaml
state: blocked
reason: active_execution_disabled
operation: atlerror.probe.begin_recommended
allowed: false
requires_opt_in: true
fallback: enable_readonly_probe_tools
```

This distinction is important. Host capability and process authorization are separate facts.

### `no_executor`

A semantic next probe exists, but Atlerror has no registered executor for it.

```yaml
state: no_executor
reason: no_registered_executor
allowed: false
fallback: gather_external_evidence
```

The semantic recommendation is preserved. The planner does not replace it with a weaker probe merely because a local executor exists for something else.

### `no_probe_needed`

The current causal ranking has fewer than two candidates, so there is no candidate ambiguity for the next-probe layer to discriminate.

```yaml
state: no_probe_needed
reason: fewer_than_two_candidates
fallback: none
```

This does not mean the incident is resolved. It means the semantic next-probe discriminator is unnecessary for this diagnosis entry.

### `no_discriminating_probe`

Multiple candidates remain, but the current canonical probe catalog does not contain an unresolved discriminator under the existing probe-ranking rules.

```yaml
state: no_discriminating_probe
reason: no_discriminating_probe
fallback: gather_external_evidence
```

This state is intentionally distinct from `no_probe_needed`.

## No new ranking layer

The agent plan is not a fourth ranker.

It MUST NOT:

- reorder causal candidates;
- reorder probe candidates;
- score executability;
- prefer a weaker probe because it is easier to execute;
- choose among unrelated diagnosis targets;
- infer probabilities or expected utility;
- use an LLM to select an action.

The current top semantic probe remains the current top semantic probe.

The plan only maps the already selected probe and its execution annotation into a small operational state machine.

## Authorization boundary

`executable_here: true` is not sufficient for `allowed: true`.

The MCP server must also have been started with:

```text
--enable-readonly-probe-tools
```

Without that process-level opt-in, the plan reports:

```text
state = blocked
reason = active_execution_disabled
requires_opt_in = true
```

This prevents host capability discovery from silently becoming execution authorization.

## Tool binding

When a step is actionable, the only operation currently exposed by the plan is:

```text
atlerror.probe.begin_recommended
```

The plan never includes an arbitrary probe ID as a caller-selectable execution argument. It provides the diagnosis target and exact semantic scope. The existing MCP tool controller still reads the current validated snapshot and chooses the current top recommendation itself.

Therefore a stale plan cannot force execution of an old probe if the diagnosis changed before the tool call.

## Fail-closed consistency checks

The projection rejects inconsistent input instead of guessing.

Examples include:

- `probe_ranking.found=true` with no probe candidates;
- top `probe_ranking` probe different from `probe_execution.probe_id`;
- `probe_execution.affects_ranking` not exactly `false`;
- unregistered probe marked executable;
- unavailable executor without an explanatory reason;
- executable registered probe without an executor ID;
- unsupported `not_found_reason`.

## Transport boundary

The agent plan is currently an MCP resource because it includes MCP process-local authorization state and directly names the MCP begin operation.

The persisted diagnosis snapshot remains transport-neutral and continues to contain:

```text
causal ranking
probe ranking
probe execution annotation
```

HTTP continues to expose that persisted snapshot without pretending that HTTP has an active execution tool.

A future HTTP planner endpoint should only be added if HTTP gains an explicit execution policy boundary equivalent to MCP tool opt-in.

## Safety

Reading the plan:

- does not execute a probe;
- does not capture a baseline;
- does not create a probe session;
- does not mutate runtime evidence;
- does not generate traffic;
- does not run shell commands or arbitrary processes;
- does not enable active MCP tools;
- does not perform remediation.

## Future work

Natural next slices are:

1. expose active session state so an agent plan can distinguish `begin` from `finish` when a probe is already in progress;
2. make stale-plan / diagnosis-revision transitions explicit in tool results;
3. add more read-only executors without weakening the registry safety boundary;
4. only later consider carefully bounded low-risk active probes with separate authorization policy.

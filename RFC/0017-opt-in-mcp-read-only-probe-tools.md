# RFC 0017: Opt-in MCP tools for recommended read-only probes

- Status: Accepted
- Date: 2026-09-11

## Context

Atlerror already exposes the current diagnosis through read-only MCP resources and can execute one explicitly registered Linux read-only probe through `scripts/probe_execution.py`. The remaining gap is agent orchestration: an MCP-aware host can read the recommended next probe, but it cannot ask Atlerror to execute that probe through the same bounded interface.

A generic command runner would erase the safety boundary established by RFC 0016. The MCP layer must not accept arbitrary shell commands, arbitrary probe IDs, arbitrary filesystem paths, network mutations, or remediation actions.

The first executable probe is two-phase. `probe.network.inspect_tcp_integrity_errors` captures Linux `Tcp.InErrs` before a controlled workload and compares the counter after that workload. Atlerror does not execute the workload itself.

## Decision

The diagnosis MCP server MAY expose active probe tools only when the operator explicitly enables them at process startup.

The default remains resource-only:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json
```

Active read-only tools require both explicit opt-in and the mutable runtime evidence document:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json \
  --enable-readonly-probe-tools \
  --runtime-evidence /tmp/atlerror-demo/runtime-evidence.json
```

When enabled, the server advertises the MCP `tools` capability and exactly two tools:

```text
atlerror.probe.begin_recommended
atlerror.probe.finish
```

The two tools are one execution capability split across the existing two-phase probe lifecycle.

## Begin tool

`atlerror.probe.begin_recommended` accepts a semantic diagnosis target and, only when necessary, an exact semantic scope.

It does not accept a probe ID.

The server reads the current validated diagnosis snapshot, finds the requested target, and selects the first entry from that diagnosis's current `probe_ranking`. If the target exists in multiple partitions, the caller must provide the exact scope. The supplied scope is validated and normalized before comparison.

The tool proceeds only when the current top recommendation is:

1. declared `risk: read_only` in canonical knowledge;
2. supported by an explicitly registered executor;
3. valid for the exact incident and diagnosis scope.

For the current network slice, the only executable recommendation is:

```text
probe.network.inspect_tcp_integrity_errors
```

The tool captures the baseline and returns an opaque `probe-session.<digest>` handle. The semantic scope used by the session comes from the matched diagnosis partition, not from an independently supplied execution scope.

## Finish tool

`atlerror.probe.finish` accepts only the opaque session ID returned by the begin tool.

It loads the persisted probe session and its MCP binding, validates that the incident, probe, and scope still match, completes the registered read-only executor, and produces standard `runtime_evidence` through the RFC 0016 implementation.

The result is composed with the configured runtime evidence document using the existing deterministic composition layer. Atlerror then rebuilds the diagnosis snapshot using the existing causal and next-probe ranking code and increments `evidence_revision`.

The feedback path is therefore:

```text
MCP diagnosis resource
  -> current top recommended probe
  -> MCP begin tool
  -> external controlled workload
  -> MCP finish tool
  -> standard runtime evidence
  -> runtime evidence composition
  -> existing diagnosis engine
  -> revised MCP diagnosis resource
```

No diagnosis logic is implemented in the MCP tool layer.

## Idempotent finish behavior

A completed probe session is identified in runtime evidence by its stable `session_id` label.

If `atlerror.probe.finish` is retried after a successful write, the controller detects the already persisted evidence instance and returns the existing result instead of reading the counter again or appending duplicate evidence.

If the evidence instance exists but the diagnosis snapshot does not yet reference it, the controller repairs the interrupted state by recomputing the snapshot once.

This makes finish safe to retry after a lost MCP response while preserving evidence instance identity.

## MCP protocol behavior

The server continues to support MCP `2026-07-28` as its primary wire revision and the existing legacy handshake revisions for the supported subset.

When probe tools are disabled, `server/discover` and legacy `initialize` advertise only `resources`. `tools/list` and `tools/call` are unavailable.

When enabled, the capabilities include:

```json
{
  "resources": {},
  "tools": {}
}
```

`tools/list` is deterministic and cacheable. Tool invocation results use normal MCP tool result semantics. Successful calls return both text content and structured JSON content. Expected execution refusals are returned as tool results with `isError: true`, allowing the calling model to correct its request. Unknown tool names remain protocol-level errors.

The primary modern response path carries the required `resultType: "complete"` discriminator and server metadata in the same way as existing resources.

## Tool annotations

Both tools declare:

```text
destructiveHint = false
openWorldHint = false
```

The tools do write Atlerror-local session, evidence, and diagnosis files, so they do not claim `readOnlyHint=true` even though the executed system probe itself is canonically `risk: read_only`.

`atlerror.probe.finish` is marked idempotent because retries return the already persisted session result rather than executing the probe again.

## Security boundary

The MCP tool layer deliberately does not expose:

- a shell or command string;
- arbitrary subprocess execution;
- arbitrary probe selection;
- caller-selected executor IDs;
- caller-selected counter source paths;
- packet generation or workload execution;
- `low`, `state_changing`, or `high` risk probes;
- network configuration changes;
- remediation or fix actions;
- remote HTTP MCP transport.

The built-in MCP path uses the same fixed Linux `/proc/net/snmp` executor as RFC 0016. The CLI remains responsible for process placement and operating-system permissions.

## Consistency model

Runtime evidence and diagnosis snapshots are individually replaced atomically after all new documents have been built and validated in memory. This is not a multi-file transaction.

The retry behavior described above repairs the important partial-write case where evidence was persisted but snapshot replacement did not complete. A future durable incident store may provide a transactional boundary if active diagnostics grow beyond local single-process use.

## Why not one blocking tool call?

The probe requires a meaningful workload interval between baseline and final counter reads. Sleeping inside a tool does not create that workload, and allowing Atlerror to generate arbitrary workload would expand the safety surface substantially.

Two explicit calls preserve the semantic experiment boundary:

```text
begin -> caller-controlled workload -> finish
```

The model sees the session handle and can thread it through the second call without hidden protocol session state.

## Non-goals

This RFC does not add:

- automatic execution immediately after a recommendation;
- MCP multi-round-trip elicitation;
- generic probe plugins;
- remote authorization;
- HTTP mutation endpoints;
- active traffic generation;
- state-changing diagnostics;
- automatic remediation.

## Consequences

Atlerror now supports a bounded agent-controlled diagnostic feedback loop while keeping probe choice, risk classification, semantic scope, evidence representation, and causal reasoning explicit.

The next expansion should add more registered read-only executors and capability discovery before considering any higher-risk probe class.

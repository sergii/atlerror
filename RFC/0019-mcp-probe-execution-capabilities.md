# RFC 0019: MCP probe execution capability resource

- Status: Accepted
- Date: 2026-09-12

## Summary

Atlerror exposes the existing host-local probe executor capability projection as a read-only MCP resource:

```text
atlerror://probe-execution/capabilities
```

The resource lets an agent distinguish three separate facts before requesting active diagnostics:

1. a semantic probe exists in canonical knowledge;
2. a built-in executor is registered for that probe;
3. that executor is currently available on this host.

Reading the resource never executes a probe and does not require the opt-in active MCP tools to be enabled.

## Motivation

RFC 0018 introduced an explicit executor registry and a deterministic capability projection. Before this RFC, that projection was available through the CLI:

```bash
python scripts/probe_execution.py capabilities --pretty
```

An MCP consumer could read the current diagnosis and see a recommended next probe, but it could not inspect the local execution substrate through the same protocol before deciding whether an active step was possible.

That created an unnecessary gap:

```text
recommended semantic probe
        |
        | unknown to MCP consumer
        v
is there a registered executor here?
is the source available here?
what policy will the executor apply?
```

The new resource closes that gap without widening the active execution boundary.

## Architecture

The projection remains owned by the executor runtime:

```text
canonical probe knowledge
        +
executor registry
        +
host platform/source checks
        |
        v
probe_execution_capabilities
        |
        +-> CLI projection
        |
        +-> MCP read-only resource
```

MCP does not implement a second registry, perform independent availability logic, or infer executor support from probe metadata.

The MCP server calls the existing `build_probe_execution_capabilities()` projection.

## Resource contract

URI:

```text
atlerror://probe-execution/capabilities
```

MIME type:

```text
application/json
```

The returned JSON is exactly the existing `probe_execution_capabilities` contract validated by:

```text
schema/probe-execution-capabilities.schema.json
```

Example shape:

```json
{
  "schema_version": "0.1",
  "kind": "probe_execution_capabilities",
  "platform": "linux",
  "executors": [
    {
      "probe": {
        "id": "probe.cpu.inspect_utilization",
        "title": "Inspect CPU utilization",
        "risk": "read_only"
      },
      "executor": {
        "id": "executor.linux.proc_stat.cpu_utilization",
        "platform": "linux"
      },
      "capability": "capability.metrics.query",
      "observation": "observation.cpu.utilization",
      "source": "/proc/stat",
      "available": true,
      "unavailable_reason": null,
      "policy": {
        "observed_threshold_percent": 80.0
      }
    }
  ]
}
```

The resource may report an executor as unavailable. Unavailability is useful information and is not itself a resource error.

## Availability semantics

Capability discovery is observational only. It may inspect local facts required to determine whether the registered executor can run, for example:

- current operating-system platform;
- whether the registered source path exists and is readable.

It does not:

- capture a probe baseline;
- read probe result counters as a diagnostic measurement;
- start a probe session;
- execute workload traffic;
- mutate the target system;
- perform remediation.

Availability is therefore separate from execution.

## Default MCP behavior

The capability resource is available in the normal resource-only MCP server process.

This is intentional. An operator does not need to enable active tools merely to inspect whether active diagnostics would be supported.

Default server shape:

```text
resources:
  atlerror://diagnosis/current
  atlerror://diagnosis/status
  atlerror://probe-execution/capabilities

tools:
  none
```

With explicit active-tool opt-in:

```text
resources:
  same three read-only resources

tools:
  atlerror.probe.begin_recommended
  atlerror.probe.finish
```

The resource does not change the process-level opt-in requirement for tools.

## Cache policy

The resource catalog remains public-cacheable for 60 seconds because the URI set is static for the running server configuration.

The capability resource body uses:

```text
ttlMs: 0
cacheScope: private
```

The body is host-specific and availability can change independently of the MCP resource catalog. Consumers should therefore treat each read as a current host projection rather than a durable global fact.

## Failure behavior

If the capability provider cannot build or validate the projection, the resource read fails explicitly.

For modern MCP requests, this is an internal resource failure rather than an empty capability set. An empty set would incorrectly mean that discovery completed successfully and no executors were registered.

Unknown or unconfigured resource URIs continue to follow the existing MCP resource-not-found semantics.

## Modern and legacy MCP

The resource is available through both supported MCP paths.

Modern `2026-07-28` requests receive the existing complete-result metadata and private zero-TTL cache hints.

Legacy initialized clients receive the same JSON document through the existing `resources/read` response shape without modern result metadata.

No new legacy capability flag is required because this is another resource under the already advertised `resources` capability.

## Security boundary

This RFC does not broaden active diagnostics.

The resource exposes only the same local executor projection already available to a local CLI user. It provides no caller-controlled:

- probe ID execution;
- executor ID selection;
- source-path override;
- command string;
- subprocess;
- traffic generator;
- workload runner;
- remediation action.

The MCP process remains stdio-local and the resource is marked private and zero-TTL.

## Relationship to recommendations

A consumer can now perform a transparent preflight:

```text
current diagnosis
      |
      v
recommended next probe
      |
      v
probe execution capabilities
      |
      +-> executor registered + available
      |      -> active tool may be useful if operator enabled it
      |
      +-> executor unavailable
             -> keep diagnosis read-only or use another evidence source
```

This preserves an important semantic distinction:

```text
recommended != executable here
```

A probe can remain the best semantic next question even when the current host cannot execute it.

## Non-goals

This RFC does not add:

- automatic probe execution;
- dynamic plugin loading;
- remote executor discovery;
- executor installation;
- capability negotiation that changes ranking;
- fallback shell commands;
- state-changing probes;
- remediation.

## Testing

CI covers:

- resource advertisement without active tools;
- host capability projection through MCP;
- zero-TTL private cache semantics;
- absence of the resource when no provider is configured programmatically;
- explicit resource failure when capability discovery fails;
- legacy resource compatibility.

## Future work

A later slice may annotate a diagnosis next-probe recommendation with local execution availability as a projection convenience. That should still derive from this capability layer rather than making executor availability part of semantic causal ranking.

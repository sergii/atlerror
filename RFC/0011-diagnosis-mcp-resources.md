# RFC 0011: Diagnosis MCP resources

Status: accepted

## Summary

RFC 0009 introduced the transport-independent `diagnosis_snapshot` contract and RFC 0010 exposed that contract through a small read-only HTTP API.

This RFC adds a second read-only projection for AI agents and MCP-aware developer tools. The adapter exposes the same validated diagnosis snapshot as Model Context Protocol resources over the standard stdio transport.

The resulting consumer path is:

```text
telemetry
  -> runtime evidence
  -> automatic diagnosis
  -> diagnosis snapshot
  -> MCP resources
  -> agent / IDE / host application
```

The MCP adapter does not run causal ranking, interpret telemetry, or define another diagnosis model.

## Decision

`scripts/diagnosis_mcp_server.py` exposes two fixed resources:

```text
atlerror://diagnosis/current
atlerror://diagnosis/status
```

`atlerror://diagnosis/current` returns the complete validated `diagnosis_snapshot` document as `application/json` text.

`atlerror://diagnosis/status` returns the same operational readiness projection used by the HTTP adapter, including snapshot availability, incident ID, evidence revision, freshness boundary, and diagnosis counts.

Both resources are backed by `DiagnosisSnapshotReader`, so HTTP and MCP use the same schema validation, atomic-replacement detection, and snapshot semantics.

## Why resources rather than tools

The current diagnosis is context, not an action.

MCP resources are application-controlled data that a host can attach to model context. That matches the Atlerror boundary better than a tool call because reading the current diagnosis has no side effect and requires no model-selected action semantics.

The server therefore advertises only the `resources` capability. It exposes no tools, prompts, mutation methods, or remediation actions.

## Transport

The first MCP adapter uses stdio.

The client launches the server as a subprocess:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-diagnosis.json
```

Messages are newline-delimited UTF-8 JSON-RPC. The server writes only MCP JSON-RPC messages to stdout. Optional diagnostics use stderr.

Stdio keeps the initial adapter local, simple, and authentication-free. A remote Streamable HTTP MCP transport is a separate deployment concern and should only be added when a concrete remote consumer requires it.

## MCP 2026-07-28

The primary protocol target is MCP `2026-07-28`.

That revision removed the `initialize` handshake and made requests stateless. Every modern request therefore carries:

```text
_meta.io.modelcontextprotocol/protocolVersion
_meta.io.modelcontextprotocol/clientCapabilities
```

`clientInfo` is accepted when present but is not required.

The server implements `server/discover` and advertises:

```yaml
supportedVersions:
  - "2026-07-28"
capabilities:
  resources: {}
```

Modern responses carry:

- `resultType: complete`
- `_meta.io.modelcontextprotocol/serverInfo`
- cache hints on cacheable resource operations

The fixed resource catalog uses `ttlMs: 60000` and `cacheScope: public` because the list of resource URIs does not depend on the incident.

`resources/read` uses `ttlMs: 0` and `cacheScope: private`. Diagnosis state is incident-specific and can change on the next watcher refresh or freshness transition, so consumers should not reuse a resource body as a fresh diagnosis without another read.

## Legacy compatibility

The server also supports the resource subset of the legacy handshake revisions:

- `2025-11-25`
- `2025-06-18`
- `2025-03-26`
- `2024-11-05`

Legacy clients use `initialize`, then `notifications/initialized`, before normal resource operations.

The implementation echoes a requested supported legacy protocol version. If a legacy initialize request supplies a version outside that set, the server answers with `2025-11-25` so the client can accept or reject the negotiation using normal MCP lifecycle rules.

Modern-only fields such as `resultType`, modern cache hints, and per-response server identity are not emitted on the legacy wire.

This compatibility layer covers only the small resource server surface. It does not claim support for optional legacy sampling, roots, logging, subscriptions, or server-to-client requests.

## Resource discovery

`resources/list` returns the two resources in deterministic URI order.

The current diagnosis resource remains discoverable even before the first snapshot exists. Discovery describes capability, not current readiness.

Consumers can read `atlerror://diagnosis/status` to distinguish:

```text
waiting_for_snapshot
ready
invalid_snapshot
```

No resource templates are currently needed. `resources/templates/list` returns an empty deterministic result for clients that probe that method.

## Read failures

A missing current snapshot is not converted into an empty diagnosis.

For MCP `2026-07-28`, an unavailable or unknown resource URI uses JSON-RPC `Invalid params` (`-32602`) with the requested URI in error data.

For legacy clients, the historical MCP resource-not-found code (`-32002`) is preserved.

A snapshot that exists but fails JSON or schema validation is an internal resource failure. The current diagnosis is not served. The status resource remains readable and reports the degraded state so an operator or agent can distinguish invalid state from a process outage.

## Protocol version failures

A modern request whose per-request protocol version is not `2026-07-28` receives the modern unsupported-version error code `-32022` with:

```yaml
supported:
  - "2026-07-28"
requested: "..."
```

This lets modern clients retry using a supported revision without hidden session state.

## Snapshot boundary

The MCP server reads the diagnosis snapshot directly rather than calling the diagnosis HTTP API.

This is intentional. Both are sibling transport adapters over the same persisted contract:

```text
                  -> diagnosis HTTP API
 diagnosis file -|
                  -> diagnosis MCP server
```

The file boundary keeps either transport optional and avoids making one adapter a runtime dependency of another.

## Security boundary

The stdio server has no network listener and no mutation surface.

The MCP host controls which local snapshot path is passed to the process. The adapter does not discover arbitrary files, follow user-provided file URIs, or expose the filesystem through MCP.

Resource URIs are exact constants and are validated before reads.

## Non-goals

This RFC does not introduce:

- MCP tools
- MCP prompts
- MCP resource subscriptions
- Streamable HTTP MCP transport
- TLS or authentication
- remote multi-user deployment
- automatic remediation
- automatic probe execution
- writes to runtime evidence or diagnosis state
- a second causal-ranking implementation
- a second diagnosis schema

## Future work

Useful next steps are:

1. add a compact recommended-next-probe projection so agents can ask what evidence would discriminate between close candidates
2. expose that projection as another read-only resource before considering any active probe tool
3. add Streamable HTTP MCP only when a remote deployment requires it
4. consider resource-change subscriptions only if polling the zero-TTL current resource becomes operationally expensive
5. correlate logs and metrics into the same incident partitions before adding broader agent actions

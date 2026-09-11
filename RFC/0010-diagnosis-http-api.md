# RFC 0010: Read-only diagnosis HTTP API

Status: accepted

## Summary

RFC 0009 introduced automatic diagnosis snapshots, but consumers still had to read a local JSON file. This RFC adds a deliberately small read-only HTTP boundary over that existing snapshot contract.

The API does not create another diagnosis model. It validates and serves the same `diagnosis_snapshot` document produced by `live_diagnosis_watch.py`.

The resulting path is:

```text
telemetry
  -> runtime evidence
  -> automatic diagnosis
  -> diagnosis snapshot
  -> read-only HTTP API
  -> agent / IDE / UI
```

## Decision

`scripts/diagnosis_http_api.py` serves one snapshot file.

Example:

```bash
python scripts/diagnosis_http_api.py \
  --snapshot /tmp/atlerror-diagnosis.json
```

The default listener is `127.0.0.1:4320`. Loopback is deliberate because this is an integration boundary, not an internet-facing service.

The API exposes:

- `GET /health` - process liveness
- `GET /status` - snapshot readiness and a compact summary
- `GET /diagnosis` - the complete validated diagnosis snapshot
- `HEAD` for the same resources

All mutation methods return HTTP 405 with `Allow: GET, HEAD`.

## Snapshot validation

The API validates the file against `schema/diagnosis-snapshot.schema.json` before serving it.

This preserves one contract across file and HTTP consumers. The API does not accept partially valid diagnosis state merely because it can be parsed as JSON.

Possible states are explicit:

- snapshot missing: `/status` reports `waiting_for_snapshot`, while `/diagnosis` returns 404
- snapshot valid: `/status` reports `ready`, while `/diagnosis` returns 200
- snapshot malformed or schema-invalid: `/status` reports `degraded`, while `/diagnosis` returns 503

`/health` remains a liveness endpoint and returns 200 even while the API is waiting for its first snapshot.

## Atomic replacement compatibility

RFC 0009 writes diagnosis snapshots through atomic file replacement. The HTTP reader is designed around that behavior.

The reader opens the current inode, reads it completely, and uses the file descriptor metadata for cache identity. A replacement therefore becomes visible as one complete document rather than a partially written file.

The reader caches a validated snapshot only while inode, size, and nanosecond modification time remain unchanged. A new atomic replacement is re-read and revalidated automatically.

## Conditional reads

`GET /diagnosis` includes an HTTP `ETag` derived from SHA-256 of the exact snapshot bytes and `Cache-Control: no-cache`.

Clients can send `If-None-Match`. If the snapshot has not changed, the API returns HTTP 304 without retransmitting the diagnosis body.

This is useful for IDEs, local agents, and dashboards that want to poll cheaply without introducing a second revision system. The semantic evidence revision inside the diagnosis snapshot remains the source of truth for diagnosis ordering.

## Status summary

`GET /status` intentionally does not duplicate the full diagnosis document. When ready it reports:

- incident ID
- evidence revision
- generated and as-of timestamps
- next freshness recomputation time
- number of scope partitions
- number of active observed targets
- number of ranked diagnoses
- number of unranked observations
- current ETag

The complete ranking explanations, paths, factors, and evidence context remain under `/diagnosis`.

## Security boundary

The built-in API has no authentication or TLS and binds to loopback by default.

Remote deployments should put an authenticated reverse proxy, service mesh, SSH tunnel, or equivalent boundary in front of it. Binding directly to a public interface is a deployment choice outside this RFC.

The API is read-only and does not expose telemetry ingestion, probe execution, remediation, or filesystem writes.

## Non-goals

This RFC does not introduce:

- authentication or authorization
- TLS termination
- CORS policy
- server-side diagnosis filtering or search
- mutation endpoints
- persistent incident storage
- multi-incident routing
- SSE, WebSocket, or webhook delivery
- MCP transport
- another diagnosis schema

## Future work

The next useful integration steps are:

1. expose the same diagnosis contract as an MCP resource for agents
2. add event-driven notification only when polling becomes a measured problem
3. add a recommended-next-probe projection for ambiguous candidate sets
4. support durable incident lookup when one process must serve multiple incidents
5. add authentication only when the API is intentionally moved beyond loopback

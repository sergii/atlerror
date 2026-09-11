#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from causal_projection import ROOT, load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader, InvalidSnapshot, SnapshotUnavailable
from mcp_probe_tools import ProbeToolInvocationError, RecommendedProbeToolController

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]

CURRENT_DIAGNOSIS_URI = "atlerror://diagnosis/current"
DIAGNOSIS_STATUS_URI = "atlerror://diagnosis/status"

SERVER_INFO = {
    "name": "atlerror-diagnosis",
    "title": "Atlerror Diagnosis",
    "version": "0.2.0",
}
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
LEGACY_RESOURCE_NOT_FOUND = -32002
UNSUPPORTED_PROTOCOL_VERSION = -32022


class McpProtocolError(ValueError):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class DiagnosisMcpServer:
    def __init__(
        self,
        reader: DiagnosisSnapshotReader,
        *,
        probe_tools: RecommendedProbeToolController | None = None,
    ) -> None:
        self.reader = reader
        self.probe_tools = probe_tools
        self._legacy_protocol_version: str | None = None
        self._legacy_initialized = False

    def _capabilities(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {"resources": {}}
        if self.probe_tools is not None:
            capabilities["tools"] = {}
        return capabilities

    def _instructions(self) -> str:
        instructions = (
            f"Read {CURRENT_DIAGNOSIS_URI} for the current diagnosis and "
            f"{DIAGNOSIS_STATUS_URI} for readiness and revision metadata."
        )
        if self.probe_tools is not None:
            instructions += (
                " Opt-in read-only probe tools are enabled. Begin only the current top "
                "recommendation, run the controlled workload externally, then finish the returned "
                "probe session to append evidence and recompute diagnosis."
            )
        return instructions

    @staticmethod
    def _resource_descriptors(*, modern: bool) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = [
            {
                "uri": CURRENT_DIAGNOSIS_URI,
                "name": "current_diagnosis",
                "description": (
                    "Current validated Atlerror diagnosis snapshot with semantic scope, "
                    "observations, ranked causal hypotheses, paths, factors, and evidence context."
                ),
                "mimeType": "application/json",
            },
            {
                "uri": DIAGNOSIS_STATUS_URI,
                "name": "diagnosis_status",
                "description": (
                    "Operational status for the diagnosis snapshot, including readiness, "
                    "incident revision, freshness boundary, and diagnosis counts."
                ),
                "mimeType": "application/json",
            },
        ]
        if modern:
            resources[0]["title"] = "Current Atlerror diagnosis"
            resources[1]["title"] = "Atlerror diagnosis status"
        return sorted(resources, key=lambda resource: resource["uri"])

    @staticmethod
    def _modern_meta() -> dict[str, Any]:
        return {SERVER_INFO_META_KEY: copy.deepcopy(SERVER_INFO)}

    @classmethod
    def _modern_result(
        cls,
        payload: dict[str, Any],
        *,
        cacheable: bool = False,
        ttl_ms: int = 0,
        cache_scope: str = "private",
    ) -> dict[str, Any]:
        result = {
            "resultType": "complete",
            **payload,
            "_meta": cls._modern_meta(),
        }
        if cacheable:
            result["ttlMs"] = ttl_ms
            result["cacheScope"] = cache_scope
        return result

    @staticmethod
    def _json_text(payload: dict[str, Any]) -> str:
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _read_resource(self, uri: str, *, modern: bool) -> dict[str, Any]:
        if uri == DIAGNOSIS_STATUS_URI:
            document = self.reader.status()
        elif uri == CURRENT_DIAGNOSIS_URI:
            try:
                document, _etag = self.reader.read()
            except SnapshotUnavailable as exc:
                code = INVALID_PARAMS if modern else LEGACY_RESOURCE_NOT_FOUND
                raise McpProtocolError(code, str(exc), data={"uri": uri}) from exc
            except InvalidSnapshot as exc:
                raise McpProtocolError(INTERNAL_ERROR, str(exc), data={"uri": uri}) from exc
        else:
            code = INVALID_PARAMS if modern else LEGACY_RESOURCE_NOT_FOUND
            raise McpProtocolError(code, "Resource not found", data={"uri": uri})

        payload = {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": self._json_text(document),
                }
            ]
        }
        if modern:
            return self._modern_result(payload, cacheable=True, ttl_ms=0, cache_scope="private")
        return payload

    @staticmethod
    def _request_params(message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params", {})
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise McpProtocolError(INVALID_PARAMS, "params must be an object")
        return params

    @staticmethod
    def _validate_implementation(value: Any, field: str) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            raise McpProtocolError(INVALID_PARAMS, f"{field} must be an object")
        if not isinstance(value.get("name"), str) or not value["name"]:
            raise McpProtocolError(INVALID_PARAMS, f"{field}.name must be a non-empty string")
        if not isinstance(value.get("version"), str) or not value["version"]:
            raise McpProtocolError(INVALID_PARAMS, f"{field}.version must be a non-empty string")

    def _modern_envelope(self, message: dict[str, Any]) -> dict[str, Any]:
        params = self._request_params(message)
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            raise McpProtocolError(
                INVALID_PARAMS,
                f"{PROTOCOL_VERSION_META_KEY} is required for {MODERN_PROTOCOL_VERSION} requests",
            )

        requested = meta.get(PROTOCOL_VERSION_META_KEY)
        if not isinstance(requested, str):
            raise McpProtocolError(
                INVALID_PARAMS,
                f"{PROTOCOL_VERSION_META_KEY} must be a string",
            )
        if requested != MODERN_PROTOCOL_VERSION:
            raise McpProtocolError(
                UNSUPPORTED_PROTOCOL_VERSION,
                f"unsupported MCP protocol version: {requested}",
                data={"supported": [MODERN_PROTOCOL_VERSION], "requested": requested},
            )

        capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
        if not isinstance(capabilities, dict):
            raise McpProtocolError(
                INVALID_PARAMS,
                f"{CLIENT_CAPABILITIES_META_KEY} must be an object",
            )
        self._validate_implementation(meta.get(CLIENT_INFO_META_KEY), CLIENT_INFO_META_KEY)
        return meta

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise McpProtocolError(INVALID_PARAMS, "protocolVersion must be a string")

        capabilities = params.get("capabilities")
        if not isinstance(capabilities, dict):
            raise McpProtocolError(INVALID_PARAMS, "capabilities must be an object")
        self._validate_implementation(params.get("clientInfo"), "clientInfo")

        negotiated = requested if requested in LEGACY_PROTOCOL_VERSIONS else LATEST_LEGACY_PROTOCOL_VERSION
        self._legacy_protocol_version = negotiated
        self._legacy_initialized = False
        return {
            "protocolVersion": negotiated,
            "capabilities": self._capabilities(),
            "serverInfo": copy.deepcopy(SERVER_INFO),
            "instructions": self._instructions(),
        }

    def _ensure_legacy_ready(self) -> None:
        if self._legacy_protocol_version is None:
            raise McpProtocolError(
                INVALID_REQUEST,
                "legacy MCP requests require initialize before normal operations",
            )
        if not self._legacy_initialized:
            raise McpProtocolError(
                INVALID_REQUEST,
                "legacy MCP client must send notifications/initialized before normal operations",
            )

    def _server_discover(self, message: dict[str, Any]) -> dict[str, Any]:
        self._modern_envelope(message)
        return self._modern_result(
            {
                "supportedVersions": [MODERN_PROTOCOL_VERSION],
                "capabilities": self._capabilities(),
                "instructions": self._instructions(),
            },
            cacheable=True,
            ttl_ms=60_000,
            cache_scope="public",
        )

    def _resources_list(self, message: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        params = self._request_params(message)
        cursor = params.get("cursor")
        if cursor is not None:
            raise McpProtocolError(INVALID_PARAMS, "this resource list does not use pagination")
        payload = {"resources": self._resource_descriptors(modern=modern)}
        if modern:
            return self._modern_result(payload, cacheable=True, ttl_ms=60_000, cache_scope="public")
        return payload

    def _resource_templates_list(self, message: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        params = self._request_params(message)
        cursor = params.get("cursor")
        if cursor is not None:
            raise McpProtocolError(INVALID_PARAMS, "this resource template list does not use pagination")
        payload: dict[str, Any] = {"resourceTemplates": []}
        if modern:
            return self._modern_result(payload, cacheable=True, ttl_ms=60_000, cache_scope="public")
        return payload

    def _resources_read(self, message: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        params = self._request_params(message)
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise McpProtocolError(INVALID_PARAMS, "resources/read requires a non-empty uri")
        return self._read_resource(uri, modern=modern)

    def _tools_list(self, message: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        if self.probe_tools is None:
            raise McpProtocolError(METHOD_NOT_FOUND, "MCP probe tools are not enabled")
        params = self._request_params(message)
        if params.get("cursor") is not None:
            raise McpProtocolError(INVALID_PARAMS, "this tool list does not use pagination")
        payload = {"tools": self.probe_tools.tool_descriptors()}
        if modern:
            return self._modern_result(payload, cacheable=True, ttl_ms=60_000, cache_scope="public")
        return payload

    @classmethod
    def _tool_result(
        cls,
        payload: dict[str, Any] | None,
        *,
        modern: bool,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if error_message is not None:
            result: dict[str, Any] = {
                "content": [{"type": "text", "text": error_message}],
                "isError": True,
            }
        else:
            assert payload is not None
            result = {
                "content": [{"type": "text", "text": cls._json_text(payload)}],
                "structuredContent": copy.deepcopy(payload),
                "isError": False,
            }
        return cls._modern_result(result) if modern else result

    def _tools_call(self, message: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        if self.probe_tools is None:
            raise McpProtocolError(METHOD_NOT_FOUND, "MCP probe tools are not enabled")
        params = self._request_params(message)
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise McpProtocolError(INVALID_PARAMS, "tools/call requires a non-empty name")
        if name not in self.probe_tools.tool_names():
            raise McpProtocolError(INVALID_PARAMS, "Tool not found", data={"name": name})
        try:
            result = self.probe_tools.call(name, params.get("arguments"))
        except ProbeToolInvocationError as exc:
            return self._tool_result(None, modern=modern, error_message=str(exc))
        return self._tool_result(result, modern=modern)

    def _dispatch_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message["method"]
        if method == "server/discover":
            return self._server_discover(message)
        if method == "initialize":
            return self._initialize(self._request_params(message))

        params = self._request_params(message)
        meta = params.get("_meta")
        modern = isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta
        if modern:
            self._modern_envelope(message)
        else:
            self._ensure_legacy_ready()

        if method == "ping":
            return self._modern_result({}) if modern else {}
        if method == "resources/list":
            return self._resources_list(message, modern=modern)
        if method == "resources/templates/list":
            return self._resource_templates_list(message, modern=modern)
        if method == "resources/read":
            return self._resources_read(message, modern=modern)
        if method == "tools/list":
            return self._tools_list(message, modern=modern)
        if method == "tools/call":
            return self._tools_call(message, modern=modern)
        raise McpProtocolError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method == "notifications/initialized":
            if self._legacy_protocol_version is not None:
                self._legacy_initialized = True
            return
        if method in {"notifications/cancelled", "notifications/progress"}:
            return

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return self._error_response(None, INVALID_REQUEST, "JSON-RPC message must be an object")
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self._error_response(request_id, INVALID_REQUEST, "invalid JSON-RPC request")

        if "id" not in message:
            try:
                self._handle_notification(message)
            except McpProtocolError:
                pass
            return None

        try:
            result = self._dispatch_request(message)
        except McpProtocolError as exc:
            return self._error_response(request_id, exc.code, exc.message, data=exc.data)
        except Exception as exc:
            return self._error_response(request_id, INTERNAL_ERROR, f"internal MCP server error: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error_response(
        request_id: Any,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve_stdio(
    server: DiagnosisMcpServer,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    error_stream: TextIO = sys.stderr,
    verbose: bool = False,
) -> int:
    for raw_line in input_stream:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        message: Any = None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = DiagnosisMcpServer._error_response(
                None,
                PARSE_ERROR,
                f"Parse error: {exc.msg}",
            )
        else:
            response = server.handle_message(message)

        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
            output_stream.flush()
        if verbose:
            method = message.get("method") if isinstance(message, dict) else None
            print(f"Atlerror MCP handled {method or 'invalid_message'}", file=error_stream)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expose the current Atlerror diagnosis snapshot as MCP resources over stdio, with "
            "optional explicitly enabled registered read-only probe tools."
        )
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Diagnosis snapshot JSON file produced by live_diagnosis_watch.py",
    )
    parser.add_argument(
        "--enable-readonly-probe-tools",
        action="store_true",
        help="Opt in to MCP tools for registered read-only diagnostic probes",
    )
    parser.add_argument(
        "--runtime-evidence",
        type=Path,
        help="Runtime evidence JSON/YAML file to append probe results to when tools are enabled",
    )
    parser.add_argument(
        "--probe-session-dir",
        type=Path,
        default=Path("/tmp/atlerror-probe-sessions"),
        help="Directory for opaque probe sessions, default /tmp/atlerror-probe-sessions",
    )
    parser.add_argument("--verbose", action="store_true", help="Write request diagnostics to stderr")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.enable_readonly_probe_tools and args.runtime_evidence is None:
        parser.error("--runtime-evidence is required with --enable-readonly-probe-tools")
    try:
        reader = DiagnosisSnapshotReader(
            args.snapshot,
            schema_path=root / "schema" / "diagnosis-snapshot.schema.json",
        )
        probe_tools = None
        if args.enable_readonly_probe_tools:
            probe_tools = RecommendedProbeToolController(
                reader=reader,
                runtime_evidence_path=args.runtime_evidence,
                snapshot_path=args.snapshot,
                concepts=load_concepts(root),
                edges=load_edges(root),
                session_dir=args.probe_session_dir,
            )
        server = DiagnosisMcpServer(reader, probe_tools=probe_tools)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return serve_stdio(server, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

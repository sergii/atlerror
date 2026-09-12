#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from jsonschema import Draft202012Validator

from diagnosis_http_api import DiagnosisSnapshotReader, InvalidSnapshot, SnapshotUnavailable
from live_diagnosis import build_diagnosis_snapshot, normalize_scope, scope_key
from probe_execution import (
    DEFAULT_SOURCE_PATH,
    begin_probe_session,
    finish_probe_session,
    load_probe_session,
)
from probe_filesystem_claim import (
    ProbeFilesystemClaimError,
    acquire_probe_filesystem_claim,
    acquire_probe_filesystem_claims,
    incident_mutation_claim_identity,
    session_claim_identity,
    target_scope_claim_identity,
)
from probe_session_state import (
    DEFAULT_SESSION_MAX_AGE_SECONDS,
    classify_probe_session_lifecycle,
    discover_pending_probe_sessions,
    probe_abandonment_path,
)
from runtime_evidence import (
    format_timestamp,
    load_runtime_evidence,
    validate_runtime_references,
    validate_scope_query,
)
from runtime_evidence_composition import compose_runtime_evidence

BEGIN_TOOL_NAME = "atlerror.probe.begin_recommended"
FINISH_TOOL_NAME = "atlerror.probe.finish"
ABANDON_TOOL_NAME = "atlerror.probe.abandon"
SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")

TARGET_SCOPE_CLAIM = "target_scope"
SESSION_CLAIM = "session"
INCIDENT_MUTATION_CLAIM = "incident_mutation"

_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entities": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "boundaries": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "attributes": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "minProperties": 1,
}

_BEGIN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target"],
    "properties": {
        "target": {
            "type": "string",
            "minLength": 1,
            "description": "Observed semantic target whose current top recommended probe should begin.",
        },
        "scope": {
            **copy.deepcopy(_SCOPE_SCHEMA),
            "description": (
                "Optional exact semantic scope when the same target is diagnosed in multiple partitions. "
                "The server normalizes and matches this against the current diagnosis snapshot."
            ),
        },
    },
}

_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sessionId"],
    "properties": {
        "sessionId": {
            "type": "string",
            "pattern": r"^probe-session\.[0-9a-f]{16}$",
            "description": "Opaque probe session identifier returned by atlerror.probe.begin_recommended.",
        }
    },
}


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class ProbeToolInvocationError(ValueError):
    pass


class RecommendedProbeToolController:
    def __init__(
        self,
        *,
        reader: DiagnosisSnapshotReader,
        runtime_evidence_path: Path,
        snapshot_path: Path,
        concepts: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        session_dir: Path,
        source_path: Path = DEFAULT_SOURCE_PATH,
        clock: Callable[[], datetime] = _default_clock,
        max_session_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
    ) -> None:
        if max_session_age_seconds <= 0:
            raise ValueError("probe session max age must be positive")
        self.reader = reader
        self.runtime_evidence_path = runtime_evidence_path
        self.snapshot_path = snapshot_path
        self.concepts = concepts
        self.edges = edges
        self.session_dir = session_dir
        self.source_path = source_path
        self.clock = clock
        self.max_session_age_seconds = max_session_age_seconds
        self._lock = RLock()

    @staticmethod
    def tool_names() -> tuple[str, str, str]:
        return (ABANDON_TOOL_NAME, BEGIN_TOOL_NAME, FINISH_TOOL_NAME)

    @staticmethod
    def tool_descriptors() -> list[dict[str, Any]]:
        tools = [
            {
                "name": BEGIN_TOOL_NAME,
                "title": "Begin recommended read-only probe",
                "description": (
                    "Capture a baseline for the current top recommended Atlerror probe. "
                    "The server chooses the probe from the validated diagnosis snapshot, refuses "
                    "non-read-only or unregistered executors, binds the session to the exact "
                    "diagnosis scope, and refuses a second unfinished session for the same target "
                    "and scope. A filesystem-backed process claim closes the cross-process race "
                    "between the pending-session check and persistence."
                ),
                "inputSchema": copy.deepcopy(_BEGIN_INPUT_SCHEMA),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": FINISH_TOOL_NAME,
                "title": "Finish read-only probe",
                "description": (
                    "Complete a non-expired registered read-only probe, append its result as "
                    "standard runtime evidence, and recompute the diagnosis snapshot. Filesystem "
                    "claims serialize the session and incident evidence mutation across processes."
                ),
                "inputSchema": copy.deepcopy(_SESSION_INPUT_SCHEMA),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
            {
                "name": ABANDON_TOOL_NAME,
                "title": "Abandon probe session",
                "description": (
                    "Mark an unfinished local probe session as abandoned without reading the probe "
                    "source again, creating runtime evidence, changing diagnosis ranking, or deleting "
                    "its persisted audit files. A per-session process claim prevents finish/abandon races."
                ),
                "inputSchema": copy.deepcopy(_SESSION_INPUT_SCHEMA),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
        ]
        return sorted(tools, key=lambda tool: tool["name"])

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProbeToolInvocationError("tool arguments must be an object")
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ProbeToolInvocationError(
                "invalid tool arguments: " + "; ".join(error.message for error in errors)
            )
        return arguments

    def _load_snapshot(self) -> tuple[dict[str, Any], str]:
        try:
            return self.reader.read()
        except (SnapshotUnavailable, InvalidSnapshot) as exc:
            raise ProbeToolInvocationError(str(exc)) from exc

    def _load_runtime_evidence(self) -> dict[str, Any]:
        try:
            document = load_runtime_evidence(self.runtime_evidence_path)
            validate_runtime_references(document, self.concepts)
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(
                f"cannot load runtime evidence {self.runtime_evidence_path}: {exc}"
            ) from exc
        return document

    @staticmethod
    def _ensure_same_incident(snapshot: dict[str, Any], evidence: dict[str, Any]) -> None:
        if snapshot["incident_id"] != evidence["incident_id"]:
            raise ProbeToolInvocationError(
                "diagnosis snapshot and runtime evidence belong to different incidents"
            )

    def _normalize_requested_scope(self, scope: Any) -> dict[str, Any] | None:
        if scope is None:
            return None
        if not isinstance(scope, dict):
            raise ProbeToolInvocationError("scope must be an object")
        try:
            validate_scope_query(scope, self.concepts)
        except ValueError as exc:
            raise ProbeToolInvocationError(str(exc)) from exc
        return normalize_scope(scope, self.concepts)

    def _find_recommendation(
        self,
        snapshot: dict[str, Any],
        *,
        target: str,
        requested_scope: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        requested_key = scope_key(requested_scope) if requested_scope is not None else None
        matches: list[tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]] = []
        for partition in snapshot.get("partitions", []):
            partition_scope = partition.get("scope")
            if requested_key is not None and scope_key(partition_scope) != requested_key:
                continue
            for diagnosis in partition.get("diagnoses", []):
                if diagnosis.get("target") != target:
                    continue
                ranking = diagnosis.get("probe_ranking", {})
                probes = ranking.get("probes", []) if ranking.get("found") is True else []
                if probes:
                    matches.append((partition_scope, diagnosis, probes[0]))
        if not matches:
            raise ProbeToolInvocationError(
                f"no current next-probe recommendation exists for target {target} in the requested scope"
            )
        if len(matches) > 1:
            raise ProbeToolInvocationError(
                f"target {target} has recommendations in multiple scopes; provide the exact scope"
            )
        return matches[0]

    def _session_path(self, session_id: str) -> Path:
        if SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ProbeToolInvocationError("invalid probe session id")
        return self.session_dir / f"{session_id}.json"

    def _binding_path(self, session_id: str) -> Path:
        if SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ProbeToolInvocationError("invalid probe session id")
        return self.session_dir / f"{session_id}.binding.json"

    def _abandonment_path(self, session_id: str) -> Path:
        try:
            return probe_abandonment_path(self.session_dir, session_id)
        except ValueError as exc:
            raise ProbeToolInvocationError(str(exc)) from exc

    def _write_binding(
        self,
        *,
        session: dict[str, Any],
        target: str,
        scope: dict[str, Any] | None,
        diagnosis_revision: int,
        diagnosis_etag: str,
    ) -> None:
        _write_json_atomic(
            self._binding_path(session["session_id"]),
            {
                "schema_version": "0.1",
                "kind": "mcp_probe_binding",
                "session_id": session["session_id"],
                "incident_id": session["incident_id"],
                "target": target,
                "probe_id": session["probe"]["id"],
                "scope": copy.deepcopy(scope),
                "diagnosis_revision": diagnosis_revision,
                "diagnosis_etag": diagnosis_etag,
            },
        )

    def _load_binding(self, session_id: str) -> dict[str, Any]:
        path = self._binding_path(session_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeToolInvocationError(f"probe binding is unavailable for {session_id}") from exc
        except json.JSONDecodeError as exc:
            raise ProbeToolInvocationError(f"probe binding is invalid JSON for {session_id}") from exc
        required = {
            "schema_version",
            "kind",
            "session_id",
            "incident_id",
            "target",
            "probe_id",
            "scope",
            "diagnosis_revision",
            "diagnosis_etag",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise ProbeToolInvocationError("probe binding has an unexpected structure")
        if document["schema_version"] != "0.1" or document["kind"] != "mcp_probe_binding":
            raise ProbeToolInvocationError("unsupported probe binding version or kind")
        if document["session_id"] != session_id:
            raise ProbeToolInvocationError("probe binding session id does not match requested session")
        for field in ("incident_id", "target", "probe_id", "diagnosis_etag"):
            if not isinstance(document[field], str) or not document[field]:
                raise ProbeToolInvocationError(f"probe binding {field} must be a non-empty string")
        if not isinstance(document["diagnosis_revision"], int) or document["diagnosis_revision"] < 0:
            raise ProbeToolInvocationError("probe binding diagnosis_revision is invalid")
        if document["scope"] is not None:
            try:
                validate_scope_query(document["scope"], self.concepts)
            except ValueError as exc:
                raise ProbeToolInvocationError(str(exc)) from exc
            document["scope"] = normalize_scope(document["scope"], self.concepts)
        return document

    def _load_abandonment(self, session_id: str, incident_id: str) -> dict[str, Any] | None:
        path = self._abandonment_path(session_id)
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeToolInvocationError(f"cannot read probe abandonment for {session_id}") from exc
        except json.JSONDecodeError as exc:
            raise ProbeToolInvocationError(f"probe abandonment is invalid JSON for {session_id}") from exc
        required = {
            "schema_version",
            "kind",
            "session_id",
            "incident_id",
            "abandoned_at",
            "reason",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise ProbeToolInvocationError("probe abandonment has an unexpected structure")
        if document["schema_version"] != "0.1" or document["kind"] != "mcp_probe_abandonment":
            raise ProbeToolInvocationError("unsupported probe abandonment version or kind")
        if document["session_id"] != session_id or document["incident_id"] != incident_id:
            raise ProbeToolInvocationError("probe abandonment does not match probe session")
        if document["reason"] != "operator_abandoned":
            raise ProbeToolInvocationError("unsupported probe abandonment reason")
        return document

    @staticmethod
    def _session_evidence_instance(
        evidence: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any] | None:
        matches = [
            instance
            for instance in evidence.get("instances", [])
            if instance.get("labels", {}).get("session_id") == session_id
        ]
        if len(matches) > 1:
            raise ProbeToolInvocationError(
                f"runtime evidence contains multiple instances for probe session {session_id}"
            )
        return copy.deepcopy(matches[0]) if matches else None

    @staticmethod
    def _snapshot_mentions_instance(snapshot: dict[str, Any], instance_id: str) -> bool:
        return any(
            instance_id in partition.get(field, [])
            for partition in snapshot.get("partitions", [])
            for field in ("active_instance_ids", "stale_instance_ids", "future_instance_ids")
        )

    @staticmethod
    def _diagnosis_projection(
        snapshot: dict[str, Any],
        *,
        target: str,
        scope: dict[str, Any] | None,
    ) -> dict[str, Any]:
        wanted_scope = scope_key(scope)
        for partition in snapshot.get("partitions", []):
            if scope_key(partition.get("scope")) != wanted_scope:
                continue
            for diagnosis in partition.get("diagnoses", []):
                if diagnosis.get("target") != target:
                    continue
                candidates = diagnosis.get("ranking", {}).get("candidates", [])
                probes = diagnosis.get("probe_ranking", {})
                next_probe = None
                if probes.get("found") is True and probes.get("probes"):
                    next_probe = probes["probes"][0]["probe"]["id"]
                return {
                    "top_hypothesis": candidates[0]["source"]["id"] if candidates else None,
                    "next_probe": next_probe,
                }
        return {"top_hypothesis": None, "next_probe": None}

    def _finish_result(
        self,
        *,
        binding: dict[str, Any],
        instance: dict[str, Any],
        snapshot: dict[str, Any],
        already_completed: bool,
    ) -> dict[str, Any]:
        return {
            "status": "completed",
            "already_completed": already_completed,
            "session_id": binding["session_id"],
            "incident_id": binding["incident_id"],
            "target": binding["target"],
            "probe_id": binding["probe_id"],
            "scope": copy.deepcopy(binding["scope"]),
            "evidence_instance_id": instance["id"],
            "observation": {
                "id": instance["observation"],
                "state": instance["state"],
                "measurement": copy.deepcopy(instance.get("measurement")),
            },
            "diagnosis_revision": snapshot["evidence_revision"],
            **self._diagnosis_projection(
                snapshot,
                target=binding["target"],
                scope=binding["scope"],
            ),
        }

    def _pending_sessions(self) -> list[dict[str, Any]]:
        try:
            return discover_pending_probe_sessions(
                session_dir=self.session_dir,
                runtime_evidence_path=self.runtime_evidence_path,
                concepts=self.concepts,
                as_of=self.clock(),
                max_age_seconds=self.max_session_age_seconds,
            )
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(f"cannot discover pending probe sessions: {exc}") from exc

    def _claim_failure(self, exc: ProbeFilesystemClaimError) -> ProbeToolInvocationError:
        return ProbeToolInvocationError(str(exc))

    def begin(self, arguments: Any) -> dict[str, Any]:
        arguments = self._validate_arguments(_BEGIN_INPUT_SCHEMA, arguments)
        target = arguments["target"]
        requested_scope = self._normalize_requested_scope(arguments.get("scope"))

        with self._lock:
            snapshot, _etag = self._load_snapshot()
            evidence = self._load_runtime_evidence()
            self._ensure_same_incident(snapshot, evidence)
            partition_scope, _diagnosis, _top_probe = self._find_recommendation(
                snapshot,
                target=target,
                requested_scope=requested_scope,
            )
            claim_identity = target_scope_claim_identity(
                incident_id=snapshot["incident_id"],
                target=target,
                scope=partition_scope,
            )
            try:
                with acquire_probe_filesystem_claim(
                    self.session_dir,
                    purpose=TARGET_SCOPE_CLAIM,
                    identity=claim_identity,
                    acquired_at=self.clock(),
                ):
                    return self._begin_claimed(target=target, partition_scope=partition_scope)
            except ProbeFilesystemClaimError as exc:
                raise self._claim_failure(exc) from exc

    def _begin_claimed(
        self,
        *,
        target: str,
        partition_scope: dict[str, Any] | None,
    ) -> dict[str, Any]:
        snapshot, etag = self._load_snapshot()
        evidence = self._load_runtime_evidence()
        self._ensure_same_incident(snapshot, evidence)
        partition_scope, _diagnosis, top_probe = self._find_recommendation(
            snapshot,
            target=target,
            requested_scope=partition_scope,
        )
        pending = [
            session
            for session in self._pending_sessions()
            if session["target"] == target
            and scope_key(session.get("scope")) == scope_key(partition_scope)
        ]
        if pending:
            existing = pending[0]
            raise ProbeToolInvocationError(
                "an unfinished probe session already exists for this target and scope: "
                f"{existing['session_id']} ({existing['lifecycle_state']}); "
                "finish or abandon it before beginning another"
            )

        probe_id = top_probe["probe"]["id"]
        if top_probe.get("risk") != "read_only":
            raise ProbeToolInvocationError(f"current top recommendation is not read-only: {probe_id}")
        try:
            session = begin_probe_session(
                incident_id=snapshot["incident_id"],
                probe_id=probe_id,
                concepts=self.concepts,
                source_path=self.source_path,
                scope=partition_scope,
                started_at=self.clock(),
            )
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(str(exc)) from exc

        session_path = self._session_path(session["session_id"])
        binding_path = self._binding_path(session["session_id"])
        abandonment_path = self._abandonment_path(session["session_id"])
        if session_path.exists() or binding_path.exists() or abandonment_path.exists():
            raise ProbeToolInvocationError(f"probe session already exists: {session['session_id']}")
        _write_json_atomic(session_path, session)
        self._write_binding(
            session=session,
            target=target,
            scope=partition_scope,
            diagnosis_revision=snapshot["evidence_revision"],
            diagnosis_etag=etag,
        )
        lifecycle_state, expires_at = classify_probe_session_lifecycle(
            session,
            as_of=self.clock(),
            max_age_seconds=self.max_session_age_seconds,
        )
        return {
            "status": "baseline_captured",
            "session_id": session["session_id"],
            "incident_id": session["incident_id"],
            "target": target,
            "probe_id": probe_id,
            "scope": copy.deepcopy(partition_scope),
            "started_at": session["started_at"],
            "expires_at": expires_at,
            "lifecycle_state": lifecycle_state,
            "baseline": copy.deepcopy(session["baseline"]),
            "diagnosis_revision": snapshot["evidence_revision"],
            "next_action": (
                "Run the controlled workload outside Atlerror, then call "
                f"{FINISH_TOOL_NAME} with this sessionId before the session expires."
            ),
        }

    def finish(self, arguments: Any) -> dict[str, Any]:
        arguments = self._validate_arguments(_SESSION_INPUT_SCHEMA, arguments)
        session_id = arguments["sessionId"]
        with self._lock:
            binding = self._load_binding(session_id)
            claims = [
                (
                    INCIDENT_MUTATION_CLAIM,
                    incident_mutation_claim_identity(incident_id=binding["incident_id"]),
                ),
                (
                    SESSION_CLAIM,
                    session_claim_identity(
                        incident_id=binding["incident_id"],
                        session_id=session_id,
                    ),
                ),
            ]
            try:
                with acquire_probe_filesystem_claims(
                    self.session_dir,
                    claims,
                    acquired_at=self.clock(),
                ):
                    return self._finish_claimed(session_id)
            except ProbeFilesystemClaimError as exc:
                raise self._claim_failure(exc) from exc

    def _finish_claimed(self, session_id: str) -> dict[str, Any]:
        binding = self._load_binding(session_id)
        try:
            session = load_probe_session(self._session_path(session_id), self.concepts)
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(str(exc)) from exc
        if session["incident_id"] != binding["incident_id"]:
            raise ProbeToolInvocationError("probe session and binding incident ids differ")
        if session["probe"]["id"] != binding["probe_id"]:
            raise ProbeToolInvocationError("probe session and binding probe ids differ")
        if normalize_scope(session.get("scope"), self.concepts) != binding["scope"]:
            raise ProbeToolInvocationError("probe session and binding scopes differ")

        evidence = self._load_runtime_evidence()
        snapshot, _etag = self._load_snapshot()
        self._ensure_same_incident(snapshot, evidence)
        if session["incident_id"] != snapshot["incident_id"]:
            raise ProbeToolInvocationError(
                "probe session belongs to a different incident than the current diagnosis"
            )
        if self._load_abandonment(session_id, session["incident_id"]) is not None:
            raise ProbeToolInvocationError(f"probe session was abandoned: {session_id}")

        existing = self._session_evidence_instance(evidence, session_id)
        if existing is not None:
            if not self._snapshot_mentions_instance(snapshot, existing["id"]):
                snapshot = build_diagnosis_snapshot(
                    evidence,
                    self.concepts,
                    self.edges,
                    as_of=self.clock(),
                    evidence_revision=snapshot["evidence_revision"] + 1,
                )
                _write_json_atomic(self.snapshot_path, snapshot)
            return self._finish_result(
                binding=binding,
                instance=existing,
                snapshot=snapshot,
                already_completed=True,
            )

        lifecycle_state, expires_at = classify_probe_session_lifecycle(
            session,
            as_of=self.clock(),
            max_age_seconds=self.max_session_age_seconds,
        )
        if lifecycle_state == "expired":
            raise ProbeToolInvocationError(
                f"probe session expired at {expires_at}; abandon it and begin a new probe"
            )
        try:
            probe_evidence = finish_probe_session(
                session,
                self.concepts,
                finished_at=self.clock(),
            )
            composed = compose_runtime_evidence([evidence, probe_evidence], self.concepts)
            updated_snapshot = build_diagnosis_snapshot(
                composed,
                self.concepts,
                self.edges,
                as_of=self.clock(),
                evidence_revision=snapshot["evidence_revision"] + 1,
            )
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(str(exc)) from exc

        instance = probe_evidence["instances"][0]
        _write_json_atomic(self.runtime_evidence_path, composed)
        _write_json_atomic(self.snapshot_path, updated_snapshot)
        return self._finish_result(
            binding=binding,
            instance=instance,
            snapshot=updated_snapshot,
            already_completed=False,
        )

    def abandon(self, arguments: Any) -> dict[str, Any]:
        arguments = self._validate_arguments(_SESSION_INPUT_SCHEMA, arguments)
        session_id = arguments["sessionId"]
        with self._lock:
            binding = self._load_binding(session_id)
            identity = session_claim_identity(
                incident_id=binding["incident_id"],
                session_id=session_id,
            )
            try:
                with acquire_probe_filesystem_claim(
                    self.session_dir,
                    purpose=SESSION_CLAIM,
                    identity=identity,
                    acquired_at=self.clock(),
                ):
                    return self._abandon_claimed(session_id)
            except ProbeFilesystemClaimError as exc:
                raise self._claim_failure(exc) from exc

    def _abandon_claimed(self, session_id: str) -> dict[str, Any]:
        binding = self._load_binding(session_id)
        try:
            session = load_probe_session(self._session_path(session_id), self.concepts)
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(str(exc)) from exc
        if session["incident_id"] != binding["incident_id"]:
            raise ProbeToolInvocationError("probe session and binding incident ids differ")

        evidence = self._load_runtime_evidence()
        snapshot, _etag = self._load_snapshot()
        self._ensure_same_incident(snapshot, evidence)
        if session["incident_id"] != snapshot["incident_id"]:
            raise ProbeToolInvocationError(
                "probe session belongs to a different incident than the current diagnosis"
            )
        if self._session_evidence_instance(evidence, session_id) is not None:
            raise ProbeToolInvocationError(f"completed probe session cannot be abandoned: {session_id}")

        existing = self._load_abandonment(session_id, session["incident_id"])
        if existing is not None:
            return {
                "status": "abandoned",
                "already_abandoned": True,
                "session_id": session_id,
                "incident_id": binding["incident_id"],
                "target": binding["target"],
                "probe_id": binding["probe_id"],
                "scope": copy.deepcopy(binding["scope"]),
                "abandoned_at": existing["abandoned_at"],
            }

        marker = {
            "schema_version": "0.1",
            "kind": "mcp_probe_abandonment",
            "session_id": session_id,
            "incident_id": binding["incident_id"],
            "abandoned_at": format_timestamp(self.clock()),
            "reason": "operator_abandoned",
        }
        _write_json_atomic(self._abandonment_path(session_id), marker)
        return {
            "status": "abandoned",
            "already_abandoned": False,
            "session_id": session_id,
            "incident_id": binding["incident_id"],
            "target": binding["target"],
            "probe_id": binding["probe_id"],
            "scope": copy.deepcopy(binding["scope"]),
            "abandoned_at": marker["abandoned_at"],
        }

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name == BEGIN_TOOL_NAME:
            return self.begin(arguments)
        if name == FINISH_TOOL_NAME:
            return self.finish(arguments)
        if name == ABANDON_TOOL_NAME:
            return self.abandon(arguments)
        raise KeyError(name)

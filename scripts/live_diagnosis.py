#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from jsonschema import Draft202012Validator

from causal_projection import ROOT
from causal_ranking import rank_causes
from runtime_evidence import format_timestamp, parse_timestamp, resolve_runtime_evidence

SCHEMA_PATH = ROOT / "schema" / "diagnosis-snapshot.schema.json"


def _implied_boundary_entities(
    boundary_ids: set[str],
    concepts: dict[str, dict[str, Any]],
) -> set[str]:
    entities: set[str] = set()
    for boundary_id in boundary_ids:
        boundary = concepts.get(boundary_id)
        if not isinstance(boundary, dict) or boundary.get("kind") != "boundary":
            continue
        for field in ("source", "target"):
            value = boundary.get(field)
            if isinstance(value, str) and value:
                entities.add(value)
    return entities


def normalize_scope(
    scope: dict[str, Any] | None,
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(scope, dict) or not scope:
        return None

    boundaries = set(scope.get("boundaries", []))
    entities = set(scope.get("entities", []))
    entities -= _implied_boundary_entities(boundaries, concepts)
    attributes = scope.get("attributes", {})

    normalized: dict[str, Any] = {}
    if entities:
        normalized["entities"] = sorted(entities)
    if boundaries:
        normalized["boundaries"] = sorted(boundaries)
    if isinstance(attributes, dict) and attributes:
        normalized["attributes"] = {
            str(key): str(value) for key, value in sorted(attributes.items())
        }
    return normalized or None


def scope_key(scope: dict[str, Any] | None) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def partition_runtime_evidence(
    document: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    partitions: dict[str, dict[str, Any]] = {}
    scopes: dict[str, dict[str, Any] | None] = {}

    for instance in document.get("instances", []):
        normalized_scope = normalize_scope(instance.get("scope"), concepts)
        key = scope_key(normalized_scope)
        scopes[key] = normalized_scope
        partitions.setdefault(key, {})[instance["id"]] = copy.deepcopy(instance)

    output: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
    for key in sorted(partitions):
        partition_document: dict[str, Any] = {
            "schema_version": document["schema_version"],
            "kind": document["kind"],
            "incident_id": document["incident_id"],
            "instances": sorted(
                partitions[key].values(),
                key=lambda instance: instance["id"],
            ),
        }
        if "description" in document:
            partition_document["description"] = document["description"]
        output.append((copy.deepcopy(scopes[key]), partition_document))
    return output


def next_evidence_transition(
    document: dict[str, Any],
    *,
    as_of: datetime,
) -> datetime | None:
    if as_of.utcoffset() is None:
        raise ValueError("diagnosis as_of must include a timezone")

    transitions: list[datetime] = []
    for instance in document.get("instances", []):
        observed_at = parse_timestamp(
            instance["observed_at"],
            f"{instance['id']}.observed_at",
        )
        if observed_at > as_of:
            transitions.append(observed_at)
        expires_at_value = instance.get("expires_at")
        if expires_at_value is not None:
            expires_at = parse_timestamp(
                expires_at_value,
                f"{instance['id']}.expires_at",
            )
            if expires_at > as_of:
                transitions.append(expires_at)
    return min(transitions) if transitions else None


def build_diagnosis_snapshot(
    document: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    as_of: datetime,
    evidence_revision: int,
    max_depth: int | None = None,
) -> dict[str, Any]:
    if as_of.utcoffset() is None:
        raise ValueError("diagnosis as_of must include a timezone")
    if evidence_revision < 0:
        raise ValueError("evidence_revision must not be negative")
    if max_depth is not None and max_depth < 1:
        raise ValueError("diagnosis max_depth must be at least 1")

    partitions: list[dict[str, Any]] = []
    for scope, partition_document in partition_runtime_evidence(document, concepts):
        observed, absent, evidence_context = resolve_runtime_evidence(
            partition_document,
            concepts,
            as_of=as_of,
            scope_query=scope,
        )
        diagnoses: list[dict[str, Any]] = []
        unranked_observations: list[str] = []

        for target in sorted(observed):
            ranking = rank_causes(
                target,
                edges,
                concepts,
                observed=observed,
                absent=absent,
                max_depth=max_depth,
                evidence_context=evidence_context,
            )
            if ranking["found"]:
                diagnoses.append({"target": target, "ranking": ranking})
            else:
                unranked_observations.append(target)

        partitions.append(
            {
                "scope": copy.deepcopy(scope),
                "observed": sorted(observed),
                "absent": sorted(absent),
                "active_instance_ids": sorted(
                    instance["id"] for instance in evidence_context["active_instances"]
                ),
                "stale_instance_ids": sorted(evidence_context["stale_instance_ids"]),
                "future_instance_ids": sorted(evidence_context["future_instance_ids"]),
                "diagnoses": diagnoses,
                "unranked_observations": sorted(unranked_observations),
            }
        )

    next_transition = next_evidence_transition(document, as_of=as_of)
    snapshot = {
        "schema_version": "0.1",
        "kind": "diagnosis_snapshot",
        "incident_id": document["incident_id"],
        "generated_at": format_timestamp(as_of),
        "as_of": format_timestamp(as_of),
        "evidence_revision": evidence_revision,
        "next_recompute_at": (
            format_timestamp(next_transition) if next_transition is not None else None
        ),
        "partitions": partitions,
    }
    validate_diagnosis_snapshot(snapshot)
    return snapshot


def validate_diagnosis_snapshot(snapshot: dict[str, Any]) -> None:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(snapshot),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(
            "diagnosis snapshot schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class LiveDiagnosisEngine:
    def __init__(
        self,
        *,
        concepts: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        snapshot_path: Path | None = None,
        max_depth: int | None = None,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        if max_depth is not None and max_depth < 1:
            raise ValueError("diagnosis max_depth must be at least 1")
        self.concepts = concepts
        self.edges = edges
        self.snapshot_path = snapshot_path
        self.max_depth = max_depth
        self.clock = clock
        self._lock = RLock()
        self._cached: dict[str, Any] | None = None
        self._cached_revision: int | None = None
        self._runs_total = 0
        self._last_run_at: str | None = None

    def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.snapshot_path is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_name(self.snapshot_path.name + ".tmp")
        temporary.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.snapshot_path)

    def refresh(
        self,
        document: dict[str, Any],
        *,
        evidence_revision: int,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        now = self.clock() if as_of is None else as_of
        snapshot = build_diagnosis_snapshot(
            document,
            self.concepts,
            self.edges,
            as_of=now,
            evidence_revision=evidence_revision,
            max_depth=self.max_depth,
        )
        with self._lock:
            self._write_snapshot(snapshot)
            self._cached = snapshot
            self._cached_revision = evidence_revision
            self._runs_total += 1
            self._last_run_at = snapshot["generated_at"]
            return copy.deepcopy(snapshot)

    def current(
        self,
        document: dict[str, Any],
        *,
        evidence_revision: int,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        now = self.clock() if as_of is None else as_of
        with self._lock:
            cached = copy.deepcopy(self._cached)
            cached_revision = self._cached_revision

        if cached is None or cached_revision != evidence_revision:
            return self.refresh(
                document,
                evidence_revision=evidence_revision,
                as_of=now,
            )

        cached_as_of = parse_timestamp(cached["as_of"], "diagnosis.as_of")
        next_recompute = cached.get("next_recompute_at")
        transition_due = False
        if next_recompute is not None:
            transition_due = now >= parse_timestamp(
                next_recompute,
                "diagnosis.next_recompute_at",
            )
        if now < cached_as_of or transition_due:
            return self.refresh(
                document,
                evidence_revision=evidence_revision,
                as_of=now,
            )
        return cached

    def status(self) -> dict[str, Any]:
        with self._lock:
            next_recompute = (
                self._cached.get("next_recompute_at") if self._cached is not None else None
            )
            return {
                "diagnosis_enabled": True,
                "diagnosis_runs_total": self._runs_total,
                "diagnosis_revision": self._cached_revision,
                "last_diagnosis_at": self._last_run_at,
                "next_diagnosis_recompute_at": next_recompute,
            }

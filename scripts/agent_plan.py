#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from causal_projection import ROOT

SCHEMA_PATH = ROOT / "schema" / "agent-plan.schema.json"
BEGIN_RECOMMENDED_OPERATION = "atlerror.probe.begin_recommended"

STATES = (
    "actionable",
    "blocked",
    "no_executor",
    "no_probe_needed",
    "no_discriminating_probe",
)


def validate_agent_plan(document: dict[str, Any], *, schema_path: Path = SCHEMA_PATH) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(
            "agent plan schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def _scope_sort_key(scope: dict[str, Any] | None) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def _tool_arguments(target: str, scope: dict[str, Any] | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"target": target}
    if scope is not None:
        arguments["scope"] = copy.deepcopy(scope)
    return arguments


def _no_recommendation_step(
    *,
    scope: dict[str, Any] | None,
    target: str,
    not_found_reason: Any,
) -> dict[str, Any]:
    if not_found_reason == "fewer_than_two_candidates":
        state = "no_probe_needed"
        reason = "fewer_than_two_candidates"
        fallback = "none"
    elif not_found_reason == "no_discriminating_probe":
        state = "no_discriminating_probe"
        reason = "no_discriminating_probe"
        fallback = "gather_external_evidence"
    else:
        raise ValueError(
            f"unsupported probe ranking not_found_reason for {target}: {not_found_reason}"
        )

    return {
        "scope": copy.deepcopy(scope),
        "target": target,
        "state": state,
        "reason": reason,
        "recommended_probe": None,
        "registered": None,
        "executable_here": None,
        "executor_id": None,
        "operation": None,
        "arguments": None,
        "allowed": False,
        "requires_opt_in": False,
        "unavailable_reason": None,
        "fallback": fallback,
    }


def _recommended_step(
    *,
    scope: dict[str, Any] | None,
    target: str,
    probe_ranking: dict[str, Any],
    probe_execution: dict[str, Any],
    active_execution_enabled: bool,
) -> dict[str, Any]:
    probes = probe_ranking.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError(f"probe ranking for {target} is found but has no probes")
    top_probe = probes[0].get("probe", {}).get("id")
    if not isinstance(top_probe, str) or not top_probe:
        raise ValueError(f"top recommended probe for {target} must include probe.id")

    annotated_probe = probe_execution.get("probe_id")
    if annotated_probe != top_probe:
        raise ValueError(
            f"probe execution annotation for {target} does not match top recommendation: "
            f"{annotated_probe} != {top_probe}"
        )
    if probe_execution.get("affects_ranking") is not False:
        raise ValueError(f"probe execution annotation for {target} must not affect ranking")

    registered = probe_execution.get("registered")
    executable_here = probe_execution.get("executable_here")
    executor = probe_execution.get("executor")
    executor_id = executor.get("id") if isinstance(executor, dict) else None
    unavailable_reason = probe_execution.get("unavailable_reason")

    base = {
        "scope": copy.deepcopy(scope),
        "target": target,
        "recommended_probe": top_probe,
        "registered": registered,
        "executable_here": executable_here,
        "executor_id": executor_id,
        "operation": None,
        "arguments": None,
        "allowed": False,
        "requires_opt_in": False,
        "unavailable_reason": unavailable_reason,
        "fallback": "none",
    }

    if registered is False:
        if executable_here is not False:
            raise ValueError(f"unregistered recommended probe {top_probe} must not be executable")
        return {
            **base,
            "state": "no_executor",
            "reason": "no_registered_executor",
            "fallback": "gather_external_evidence",
        }

    if registered is not True:
        raise ValueError(f"recommended probe {top_probe} must declare registration state")

    if executable_here is False:
        if not isinstance(unavailable_reason, str) or not unavailable_reason:
            raise ValueError(
                f"unavailable recommended probe {top_probe} must explain why it is unavailable"
            )
        return {
            **base,
            "state": "blocked",
            "reason": "executor_unavailable",
            "fallback": "restore_executor_availability",
        }

    if executable_here is not True:
        raise ValueError(f"registered recommended probe {top_probe} has invalid executable state")
    if executor_id is None:
        raise ValueError(f"executable recommended probe {top_probe} must include executor.id")

    operation = BEGIN_RECOMMENDED_OPERATION
    arguments = _tool_arguments(target, scope)
    if active_execution_enabled:
        return {
            **base,
            "state": "actionable",
            "reason": "ready_to_begin_recommended",
            "operation": operation,
            "arguments": arguments,
            "allowed": True,
        }

    return {
        **base,
        "state": "blocked",
        "reason": "active_execution_disabled",
        "operation": operation,
        "arguments": arguments,
        "requires_opt_in": True,
        "fallback": "enable_readonly_probe_tools",
    }


def build_agent_plan(
    snapshot: dict[str, Any],
    *,
    active_execution_enabled: bool,
) -> dict[str, Any]:
    if snapshot.get("kind") != "diagnosis_snapshot":
        raise ValueError("agent plan requires a diagnosis_snapshot")
    incident_id = snapshot.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        raise ValueError("diagnosis snapshot must include incident_id")
    evidence_revision = snapshot.get("evidence_revision")
    if not isinstance(evidence_revision, int) or evidence_revision < 0:
        raise ValueError("diagnosis snapshot must include a non-negative evidence_revision")

    partitions = snapshot.get("partitions")
    if not isinstance(partitions, list):
        raise ValueError("diagnosis snapshot partitions must be an array")

    steps: list[dict[str, Any]] = []
    ordered_partitions = sorted(
        partitions,
        key=lambda partition: _scope_sort_key(partition.get("scope"))
        if isinstance(partition, dict)
        else "",
    )
    for partition in ordered_partitions:
        if not isinstance(partition, dict):
            raise ValueError("diagnosis partition must be an object")
        scope = partition.get("scope")
        diagnoses = partition.get("diagnoses")
        if not isinstance(diagnoses, list):
            raise ValueError("diagnosis partition diagnoses must be an array")
        for diagnosis in sorted(
            diagnoses,
            key=lambda item: item.get("target", "") if isinstance(item, dict) else "",
        ):
            if not isinstance(diagnosis, dict):
                raise ValueError("diagnosis entry must be an object")
            target = diagnosis.get("target")
            if not isinstance(target, str) or not target:
                raise ValueError("diagnosis target must be a non-empty string")
            probe_ranking = diagnosis.get("probe_ranking")
            probe_execution = diagnosis.get("probe_execution")
            if not isinstance(probe_ranking, dict) or not isinstance(probe_execution, dict):
                raise ValueError(f"diagnosis {target} must include probe ranking and execution annotation")

            if probe_ranking.get("found") is True:
                step = _recommended_step(
                    scope=scope,
                    target=target,
                    probe_ranking=probe_ranking,
                    probe_execution=probe_execution,
                    active_execution_enabled=active_execution_enabled,
                )
            else:
                if probe_execution.get("probe_id") is not None:
                    raise ValueError(
                        f"diagnosis {target} has no recommended probe but execution annotation has probe_id"
                    )
                step = _no_recommendation_step(
                    scope=scope,
                    target=target,
                    not_found_reason=probe_ranking.get("not_found_reason"),
                )
            steps.append(step)

    summary = {state: 0 for state in STATES}
    for step in steps:
        summary[step["state"]] += 1

    document = {
        "schema_version": "0.1",
        "kind": "agent_plan",
        "incident_id": incident_id,
        "evidence_revision": evidence_revision,
        "active_execution_enabled": active_execution_enabled,
        "steps": steps,
        "summary": summary,
    }
    validate_agent_plan(document)
    return document

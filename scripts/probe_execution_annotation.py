#!/usr/bin/env python3

from __future__ import annotations

import copy
from typing import Any


def annotate_recommended_probe_execution(
    probe_ranking: dict[str, Any],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Describe host-local executability without changing semantic probe ranking."""
    platform = capabilities.get("platform")
    if not isinstance(platform, str) or not platform:
        raise ValueError("probe execution capabilities must include a non-empty platform")

    executors = capabilities.get("executors")
    if not isinstance(executors, list):
        raise ValueError("probe execution capabilities executors must be an array")

    entries_by_probe: dict[str, dict[str, Any]] = {}
    for entry in executors:
        if not isinstance(entry, dict):
            raise ValueError("probe execution capability entry must be an object")
        probe_id = entry.get("probe", {}).get("id")
        if not isinstance(probe_id, str) or not probe_id:
            raise ValueError("probe execution capability entry must include probe.id")
        if probe_id in entries_by_probe:
            raise ValueError(f"duplicate probe execution capability entry: {probe_id}")
        entries_by_probe[probe_id] = entry

    probes = probe_ranking.get("probes", [])
    if probe_ranking.get("found") is not True or not isinstance(probes, list) or not probes:
        return {
            "schema_version": "0.1",
            "kind": "probe_execution_annotation",
            "affects_ranking": False,
            "platform": platform,
            "probe_id": None,
            "registered": None,
            "executable_here": None,
            "unavailable_reason": "no_recommended_probe",
            "executor": None,
            "capability": None,
            "observation": None,
            "source": None,
            "policy": {},
        }

    top = probes[0]
    probe_id = top.get("probe", {}).get("id")
    if not isinstance(probe_id, str) or not probe_id:
        raise ValueError("top recommended probe must include probe.id")

    entry = entries_by_probe.get(probe_id)
    if entry is None:
        return {
            "schema_version": "0.1",
            "kind": "probe_execution_annotation",
            "affects_ranking": False,
            "platform": platform,
            "probe_id": probe_id,
            "registered": False,
            "executable_here": False,
            "unavailable_reason": "no_registered_executor",
            "executor": None,
            "capability": None,
            "observation": None,
            "source": None,
            "policy": {},
        }

    available = entry.get("available")
    if not isinstance(available, bool):
        raise ValueError(f"probe execution capability availability is invalid for {probe_id}")
    reason = entry.get("unavailable_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"probe execution capability unavailable_reason is invalid for {probe_id}")

    executor = entry.get("executor")
    if not isinstance(executor, dict):
        raise ValueError(f"probe execution capability executor is invalid for {probe_id}")

    return {
        "schema_version": "0.1",
        "kind": "probe_execution_annotation",
        "affects_ranking": False,
        "platform": platform,
        "probe_id": probe_id,
        "registered": True,
        "executable_here": available,
        "unavailable_reason": reason,
        "executor": copy.deepcopy(executor),
        "capability": entry.get("capability"),
        "observation": entry.get("observation"),
        "source": entry.get("source"),
        "policy": copy.deepcopy(entry.get("policy", {})),
    }

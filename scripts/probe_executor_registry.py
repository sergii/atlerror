#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from causal_projection import ROOT

TCP_INTEGRITY_PROBE_ID = "probe.network.inspect_tcp_integrity_errors"
TCP_INTEGRITY_CAPABILITY_ID = "capability.network.inspect_tcp_integrity_errors"
TCP_INTEGRITY_OBSERVATION_ID = "observation.network.tcp_integrity_errors"
TCP_INTEGRITY_EXECUTOR_ID = "executor.linux.proc_net_snmp.tcp_inerrs"
TCP_INTEGRITY_SOURCE_PATH = Path("/proc/net/snmp")

CPU_UTILIZATION_PROBE_ID = "probe.cpu.inspect_utilization"
CPU_UTILIZATION_CAPABILITY_ID = "capability.metrics.query"
CPU_UTILIZATION_OBSERVATION_ID = "observation.cpu.utilization"
CPU_UTILIZATION_EXECUTOR_ID = "executor.linux.proc_stat.cpu_utilization"
CPU_UTILIZATION_SOURCE_PATH = Path("/proc/stat")
CPU_UTILIZATION_THRESHOLD_PCT = 80.0

CAPABILITIES_SCHEMA_PATH = ROOT / "schema" / "probe-execution-capabilities.schema.json"

CaptureFn = Callable[[Path], dict[str, int]]
EvaluateFn = Callable[[dict[str, int], dict[str, int], dict[str, Any]], dict[str, Any]]


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {label} from {path}: {exc}") from exc


def parse_proc_net_snmp_counter(text: str, *, section: str, counter: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    prefix = section + ":"
    for index in range(len(lines) - 1):
        header_line = lines[index]
        value_line = lines[index + 1]
        if not header_line.startswith(prefix) or not value_line.startswith(prefix):
            continue

        headers = header_line.split()[1:]
        values = value_line.split()[1:]
        if len(headers) != len(values):
            raise ValueError(f"/proc/net/snmp {section} header and value columns have different lengths")
        try:
            value_index = headers.index(counter)
        except ValueError as exc:
            raise ValueError(f"/proc/net/snmp {section} section does not expose {counter}") from exc
        try:
            value = int(values[value_index])
        except ValueError as exc:
            raise ValueError(f"/proc/net/snmp {section}.{counter} must be an integer") from exc
        if value < 0:
            raise ValueError(f"/proc/net/snmp {section}.{counter} must not be negative")
        return value
    raise ValueError(f"/proc/net/snmp does not contain a {section} header/value pair")


def parse_tcp_inerrs(text: str) -> int:
    return parse_proc_net_snmp_counter(text, section="Tcp", counter="InErrs")


def read_tcp_inerrs(path: Path = TCP_INTEGRITY_SOURCE_PATH) -> int:
    return parse_tcp_inerrs(_read_text(path, "TCP counters"))


def parse_proc_stat_cpu(text: str) -> dict[str, int]:
    aggregate: list[str] | None = None
    for raw_line in text.splitlines():
        fields = raw_line.split()
        if fields and fields[0] == "cpu":
            aggregate = fields[1:]
            break
    if aggregate is None:
        raise ValueError("/proc/stat does not contain the aggregate cpu row")
    if len(aggregate) < 4:
        raise ValueError("/proc/stat aggregate cpu row must contain at least four counters")

    values: list[int] = []
    for raw_value in aggregate:
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError("/proc/stat aggregate cpu counters must be integers") from exc
        if value < 0:
            raise ValueError("/proc/stat aggregate cpu counters must not be negative")
        values.append(value)

    user = values[0]
    nice = values[1] if len(values) > 1 else 0
    system = values[2] if len(values) > 2 else 0
    idle = values[3] if len(values) > 3 else 0
    iowait = values[4] if len(values) > 4 else 0
    irq = values[5] if len(values) > 5 else 0
    softirq = values[6] if len(values) > 6 else 0
    steal = values[7] if len(values) > 7 else 0

    total = user + nice + system + idle + iowait + irq + softirq + steal
    idle_total = idle + iowait
    if total < idle_total:
        raise ValueError("/proc/stat aggregate cpu counters are inconsistent")
    return {"idle": idle_total, "total": total}


def read_proc_stat_cpu(path: Path = CPU_UTILIZATION_SOURCE_PATH) -> dict[str, int]:
    return parse_proc_stat_cpu(_read_text(path, "CPU counters"))


def _capture_tcp_integrity(path: Path) -> dict[str, int]:
    return {"counter": read_tcp_inerrs(path)}


def _evaluate_tcp_integrity(
    baseline: dict[str, int],
    current: dict[str, int],
    _policy: dict[str, Any],
) -> dict[str, Any]:
    if set(baseline) != {"counter"} or set(current) != {"counter"}:
        raise ValueError("TCP integrity executor requires one counter value")
    baseline_value = baseline["counter"]
    current_value = current["counter"]
    if current_value < baseline_value:
        raise ValueError(
            "Tcp.InErrs decreased during the probe session; the counter may have reset or "
            "the source may have changed"
        )
    delta = current_value - baseline_value
    return {
        "state": "observed" if delta > 0 else "absent",
        "measurement": {
            "value": current_value,
            "baseline": baseline_value,
            "delta": delta,
            "unit": "segments",
            "comparison": "changed" if delta > 0 else "equal",
        },
        "note": (
            "Linux Tcp.InErrs is broader than checksum-only failure accounting, and checksum "
            "offload can affect what reaches this counter. Interpret the result with host and "
            "interface context."
        ),
    }


def _capture_cpu_utilization(path: Path) -> dict[str, int]:
    return read_proc_stat_cpu(path)


def _evaluate_cpu_utilization(
    baseline: dict[str, int],
    current: dict[str, int],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if set(baseline) != {"idle", "total"} or set(current) != {"idle", "total"}:
        raise ValueError("CPU utilization executor requires idle and total counters")
    if current["total"] < baseline["total"] or current["idle"] < baseline["idle"]:
        raise ValueError(
            "/proc/stat CPU counters decreased during the probe session; the counters may have "
            "reset or the source may have changed"
        )

    delta_total = current["total"] - baseline["total"]
    delta_idle = current["idle"] - baseline["idle"]
    if delta_total <= 0:
        raise ValueError("/proc/stat CPU counters did not advance during the probe session")
    if delta_idle > delta_total:
        raise ValueError("/proc/stat CPU idle delta exceeds total delta")

    busy_delta = delta_total - delta_idle
    utilization_pct = round((busy_delta / delta_total) * 100.0, 3)
    threshold_pct = float(policy["observed_threshold_pct"])
    delta_from_threshold = round(utilization_pct - threshold_pct, 3)
    if utilization_pct > threshold_pct:
        comparison = "above_baseline"
    elif utilization_pct < threshold_pct:
        comparison = "below_baseline"
    else:
        comparison = "equal"

    return {
        "state": "observed" if utilization_pct >= threshold_pct else "absent",
        "measurement": {
            "value": utilization_pct,
            "baseline": threshold_pct,
            "delta": delta_from_threshold,
            "unit": "percent",
            "comparison": comparison,
        },
        "note": (
            "This built-in executor measures aggregate Linux host CPU utilization from /proc/stat. "
            f"The {threshold_pct:g}% observed threshold is executor policy, not a universal "
            "Atlerror ontology threshold. Preserve host scope when interpreting the result."
        ),
    }


@dataclass(frozen=True)
class ProbeExecutor:
    id: str
    probe_id: str
    capability_id: str
    observation_id: str
    platform: str
    source_path: Path
    baseline_metric: str
    policy: dict[str, Any]
    capture_fn: CaptureFn
    evaluate_fn: EvaluateFn

    def with_source(self, source_path: Path) -> "ProbeExecutor":
        return replace(self, source_path=source_path)

    def capture(self) -> dict[str, Any]:
        return {
            "metric": self.baseline_metric,
            "values": self.capture_fn(self.source_path),
        }

    def evaluate(self, baseline: dict[str, Any]) -> dict[str, Any]:
        if baseline.get("metric") != self.baseline_metric:
            raise ValueError(
                f"probe baseline metric changed for {self.probe_id}: "
                f"expected {self.baseline_metric}, got {baseline.get('metric')}"
            )
        baseline_values = baseline.get("values")
        if not isinstance(baseline_values, dict):
            raise ValueError("probe baseline values must be an object")
        current_values = self.capture_fn(self.source_path)
        return self.evaluate_fn(baseline_values, current_values, copy.deepcopy(self.policy))

    def availability(self) -> tuple[bool, str | None]:
        if self.platform == "linux" and not sys.platform.startswith("linux"):
            return False, f"executor requires linux, current platform is {sys.platform}"
        if not self.source_path.exists():
            return False, f"source does not exist: {self.source_path}"
        if not self.source_path.is_file():
            return False, f"source is not a file: {self.source_path}"
        if not os.access(self.source_path, os.R_OK):
            return False, f"source is not readable: {self.source_path}"
        return True, None


class ProbeExecutorRegistry:
    def __init__(self, executors: list[ProbeExecutor]) -> None:
        by_probe: dict[str, ProbeExecutor] = {}
        by_executor: dict[str, ProbeExecutor] = {}
        for executor in executors:
            if executor.probe_id in by_probe:
                raise ValueError(f"duplicate probe executor registration: {executor.probe_id}")
            if executor.id in by_executor:
                raise ValueError(f"duplicate executor id: {executor.id}")
            by_probe[executor.probe_id] = executor
            by_executor[executor.id] = executor
        self._by_probe = by_probe
        self._by_executor = by_executor

    def with_source_override(self, probe_id: str, source_path: Path) -> "ProbeExecutorRegistry":
        if probe_id not in self._by_probe:
            raise ValueError(f"no built-in read-only executor is registered for probe: {probe_id}")
        executors = [
            executor.with_source(source_path) if executor.probe_id == probe_id else executor
            for executor in self.executors()
        ]
        return ProbeExecutorRegistry(executors)

    def executors(self) -> list[ProbeExecutor]:
        return [self._by_probe[probe_id] for probe_id in sorted(self._by_probe)]

    def resolve(
        self,
        probe_id: str,
        concepts: dict[str, dict[str, Any]],
    ) -> ProbeExecutor:
        probe = concepts.get(probe_id)
        if probe is None:
            raise ValueError(f"unknown probe concept: {probe_id}")
        if probe.get("kind") != "probe":
            raise ValueError(f"execution target must be a probe concept: {probe_id}")

        risk = probe.get("risk")
        if risk != "read_only":
            raise ValueError(
                f"active probe execution refuses non-read-only probe {probe_id}: risk={risk}"
            )

        executor = self._by_probe.get(probe_id)
        if executor is None:
            raise ValueError(f"no built-in read-only executor is registered for probe: {probe_id}")

        requires = set(probe.get("requires", []))
        produces = set(probe.get("produces", []))
        if executor.capability_id not in requires:
            raise ValueError(
                f"{probe_id} must require {executor.capability_id} for executor {executor.id}"
            )
        if executor.observation_id not in produces:
            raise ValueError(
                f"{probe_id} must produce {executor.observation_id} for executor {executor.id}"
            )
        return executor

    def resolve_executor_id(
        self,
        executor_id: str,
        concepts: dict[str, dict[str, Any]],
    ) -> ProbeExecutor:
        executor = self._by_executor.get(executor_id)
        if executor is None:
            raise ValueError(f"unsupported probe executor: {executor_id}")
        return self.resolve(executor.probe_id, concepts)

    def capability_projection(
        self,
        concepts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for registered in self.executors():
            executor = self.resolve(registered.probe_id, concepts)
            probe = concepts[executor.probe_id]
            available, reason = executor.availability()
            entries.append(
                {
                    "probe": {
                        "id": executor.probe_id,
                        "title": probe.get("title", executor.probe_id),
                        "risk": "read_only",
                    },
                    "executor": {
                        "id": executor.id,
                        "platform": executor.platform,
                    },
                    "capability": executor.capability_id,
                    "observation": executor.observation_id,
                    "source": str(executor.source_path),
                    "available": available,
                    "unavailable_reason": reason,
                    "policy": copy.deepcopy(executor.policy),
                }
            )
        return {
            "schema_version": "0.1",
            "kind": "probe_execution_capabilities",
            "platform": sys.platform,
            "executors": entries,
        }


def default_executor_registry(
    source_overrides: dict[str, Path] | None = None,
) -> ProbeExecutorRegistry:
    executors = [
        ProbeExecutor(
            id=TCP_INTEGRITY_EXECUTOR_ID,
            probe_id=TCP_INTEGRITY_PROBE_ID,
            capability_id=TCP_INTEGRITY_CAPABILITY_ID,
            observation_id=TCP_INTEGRITY_OBSERVATION_ID,
            platform="linux",
            source_path=TCP_INTEGRITY_SOURCE_PATH,
            baseline_metric="Tcp.InErrs",
            policy={"classification": "delta_positive"},
            capture_fn=_capture_tcp_integrity,
            evaluate_fn=_evaluate_tcp_integrity,
        ),
        ProbeExecutor(
            id=CPU_UTILIZATION_EXECUTOR_ID,
            probe_id=CPU_UTILIZATION_PROBE_ID,
            capability_id=CPU_UTILIZATION_CAPABILITY_ID,
            observation_id=CPU_UTILIZATION_OBSERVATION_ID,
            platform="linux",
            source_path=CPU_UTILIZATION_SOURCE_PATH,
            baseline_metric="proc.stat.cpu",
            policy={
                "classification": "utilization_threshold",
                "observed_threshold_pct": CPU_UTILIZATION_THRESHOLD_PCT,
            },
            capture_fn=_capture_cpu_utilization,
            evaluate_fn=_evaluate_cpu_utilization,
        ),
    ]
    registry = ProbeExecutorRegistry(executors)
    for probe_id, source_path in sorted((source_overrides or {}).items()):
        registry = registry.with_source_override(probe_id, source_path)
    return registry


def write_projection_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"

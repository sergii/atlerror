#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from datetime import datetime

from mcp_probe_tools import ProbeToolInvocationError, RecommendedProbeToolController
from probe_execution import build_probe_execution_capabilities


class RegistryRecommendedProbeToolController(RecommendedProbeToolController):
    def __init__(
        self,
        *,
        reader,
        runtime_evidence_path: Path,
        snapshot_path: Path,
        concepts: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        session_dir: Path,
        source_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "reader": reader,
            "runtime_evidence_path": runtime_evidence_path,
            "snapshot_path": snapshot_path,
            "concepts": concepts,
            "edges": edges,
            "session_dir": session_dir,
            "source_path": source_path,
        }
        if clock is not None:
            kwargs["clock"] = clock
        super().__init__(**kwargs)

    def capabilities(self) -> dict[str, Any]:
        return build_probe_execution_capabilities(self.concepts)


__all__ = [
    "ProbeToolInvocationError",
    "RegistryRecommendedProbeToolController",
]

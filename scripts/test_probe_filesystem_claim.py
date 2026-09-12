#!/usr/bin/env python3

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from causal_projection import load_concepts, load_edges
from demo_checkout_stripe import DEFAULT_AS_OF, build_demo
from diagnosis_http_api import DiagnosisSnapshotReader
from live_diagnosis import build_diagnosis_snapshot
from mcp_probe_tools import ProbeToolInvocationError, RecommendedProbeToolController
from probe_executor_registry import default_executor_registry
from probe_filesystem_claim import (
    ProbeFilesystemClaimBusy,
    acquire_probe_filesystem_claim,
    target_scope_claim_identity,
)
from probe_session_state import discover_pending_probe_sessions

ROOT = Path(__file__).resolve().parents[1]
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"
START = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


def _hold_claim_worker(session_dir: str, ready: Any, release: Any) -> None:
    identity = {"incident_id": "incident.test", "target": TARGET, "scope": None}
    with acquire_probe_filesystem_claim(
        Path(session_dir),
        purpose="target_scope",
        identity=identity,
        acquired_at=START,
    ):
        ready.set()
        release.wait(10)


def _crash_with_claim_worker(session_dir: str, ready: Any) -> None:
    identity = {"incident_id": "incident.test", "target": TARGET, "scope": None}
    with acquire_probe_filesystem_claim(
        Path(session_dir),
        purpose="target_scope",
        identity=identity,
        acquired_at=START,
    ):
        ready.set()
        os._exit(0)


def _controller_for_directory(directory: Path, started_at: datetime) -> RecommendedProbeToolController:
    concepts = load_concepts(ROOT)
    edges = load_edges(ROOT)
    snapshot_path = directory / "diagnosis.json"
    evidence_path = directory / "runtime-evidence.json"
    source_path = directory / "proc-net-snmp"
    return RecommendedProbeToolController(
        reader=DiagnosisSnapshotReader(snapshot_path),
        runtime_evidence_path=evidence_path,
        snapshot_path=snapshot_path,
        concepts=concepts,
        edges=edges,
        session_dir=directory / "sessions",
        source_path=source_path,
        clock=lambda: started_at,
    )


def _begin_worker(
    directory_name: str,
    started_at_iso: str,
    barrier: Any,
    queue: Any,
) -> None:
    started_at = datetime.fromisoformat(started_at_iso)
    controller = _controller_for_directory(Path(directory_name), started_at)
    barrier.wait()
    try:
        result = controller.begin({"target": TARGET})
    except Exception as exc:  # Return the exact cross-process outcome to the parent test.
        queue.put({"ok": False, "error": str(exc), "type": type(exc).__name__})
    else:
        queue.put({"ok": True, "result": result})


def _finish_worker(
    directory_name: str,
    session_id: str,
    finished_at_iso: str,
    barrier: Any,
    queue: Any,
) -> None:
    finished_at = datetime.fromisoformat(finished_at_iso)
    controller = _controller_for_directory(Path(directory_name), finished_at)
    barrier.wait()
    try:
        result = controller.finish({"sessionId": session_id})
    except Exception as exc:
        queue.put({"ok": False, "error": str(exc), "type": type(exc).__name__})
    else:
        queue.put({"ok": True, "result": result})


class ProbeFilesystemClaimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict[str, Any]) -> None:
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def prepare_runtime(self, directory: Path) -> None:
        demo = build_demo(root=ROOT)
        evidence_path = directory / "runtime-evidence.json"
        snapshot_path = directory / "diagnosis.json"
        source_path = directory / "proc-net-snmp"
        source_path.write_text(tcp_snmp(0), encoding="utf-8")
        snapshot = build_diagnosis_snapshot(
            demo["runtime_evidence"],
            self.concepts,
            self.edges,
            as_of=DEFAULT_AS_OF,
            evidence_revision=1,
            executor_registry=default_executor_registry({PROBE: source_path}),
        )
        self.write_json(evidence_path, demo["runtime_evidence"])
        self.write_json(snapshot_path, snapshot)

    def test_same_claim_is_exclusive_across_processes_and_reusable_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_claim_worker,
                args=(directory_name, ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(10))

            identity = {"incident_id": "incident.test", "target": TARGET, "scope": None}
            with self.assertRaises(ProbeFilesystemClaimBusy):
                with acquire_probe_filesystem_claim(
                    Path(directory_name),
                    purpose="target_scope",
                    identity=identity,
                    acquired_at=START,
                ):
                    pass

            release.set()
            process.join(10)
            self.assertEqual(0, process.exitcode)
            with acquire_probe_filesystem_claim(
                Path(directory_name),
                purpose="target_scope",
                identity=identity,
                acquired_at=START,
            ):
                pass

    def test_process_crash_releases_kernel_claim_without_deleting_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            process = context.Process(
                target=_crash_with_claim_worker,
                args=(directory_name, ready),
            )
            process.start()
            self.assertTrue(ready.wait(10))
            process.join(10)
            self.assertEqual(0, process.exitcode)

            identity = {"incident_id": "incident.test", "target": TARGET, "scope": None}
            with acquire_probe_filesystem_claim(
                Path(directory_name),
                purpose="target_scope",
                identity=identity,
                acquired_at=START + timedelta(seconds=1),
            ):
                pass
            self.assertTrue(any((Path(directory_name) / ".claims").glob("*.lock")))

    def test_different_target_scope_claims_do_not_block_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir = Path(directory_name)
            first = target_scope_claim_identity(
                incident_id="incident.test",
                target="observation.a",
                scope={"attributes": {"service": "a"}},
            )
            second = target_scope_claim_identity(
                incident_id="incident.test",
                target="observation.b",
                scope={"attributes": {"service": "a"}},
            )
            with acquire_probe_filesystem_claim(
                session_dir,
                purpose="target_scope",
                identity=first,
                acquired_at=START,
            ):
                with acquire_probe_filesystem_claim(
                    session_dir,
                    purpose="target_scope",
                    identity=second,
                    acquired_at=START,
                ):
                    pass

    def test_cross_process_begin_race_persists_exactly_one_pending_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare_runtime(directory)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_begin_worker,
                    args=(
                        directory_name,
                        (START + timedelta(microseconds=index)).isoformat(),
                        barrier,
                        queue,
                    ),
                )
                for index in range(2)
            ]
            for process in processes:
                process.start()
            outcomes = [queue.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(0, process.exitcode)

            self.assertEqual(1, sum(1 for outcome in outcomes if outcome["ok"]))
            errors = [outcome["error"] for outcome in outcomes if not outcome["ok"]]
            self.assertEqual(1, len(errors))
            self.assertTrue(
                "filesystem claim is busy" in errors[0]
                or "unfinished probe session already exists" in errors[0]
            )

            pending = discover_pending_probe_sessions(
                session_dir=directory / "sessions",
                runtime_evidence_path=directory / "runtime-evidence.json",
                concepts=self.concepts,
                as_of=START + timedelta(seconds=1),
            )
            self.assertEqual(1, len(pending))
            self.assertEqual(TARGET, pending[0]["target"])

    def test_cross_process_finish_race_never_duplicates_probe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            self.prepare_runtime(directory)
            controller = _controller_for_directory(directory, START)
            begun = controller.begin({"target": TARGET})
            session_id = begun["session_id"]
            (directory / "proc-net-snmp").write_text(tcp_snmp(4), encoding="utf-8")

            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_finish_worker,
                    args=(
                        directory_name,
                        session_id,
                        (START + timedelta(seconds=1)).isoformat(),
                        barrier,
                        queue,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            outcomes = [queue.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(0, process.exitcode)

            successes = [outcome["result"] for outcome in outcomes if outcome["ok"]]
            self.assertGreaterEqual(len(successes), 1)
            self.assertEqual(
                1,
                sum(1 for result in successes if result["already_completed"] is False),
            )
            for outcome in outcomes:
                if not outcome["ok"]:
                    self.assertIn("filesystem claim is busy", outcome["error"])

            evidence = json.loads((directory / "runtime-evidence.json").read_text(encoding="utf-8"))
            instances = [
                instance
                for instance in evidence["instances"]
                if instance.get("labels", {}).get("session_id") == session_id
            ]
            self.assertEqual(1, len(instances))
            snapshot = json.loads((directory / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertEqual(2, snapshot["evidence_revision"])


if __name__ == "__main__":
    unittest.main()

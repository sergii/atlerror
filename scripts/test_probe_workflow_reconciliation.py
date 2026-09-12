#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from causal_projection import load_concepts
from demo_checkout_stripe import build_demo
from probe_execution import begin_probe_session
from probe_session_state import discover_pending_probe_sessions
from probe_workflow_reconciliation import (
    ProbeWorkflowPartialStateError,
    main,
    reconcile_partial_probe_workflow,
    reconciliation_path,
    scan_partial_probe_workflows,
)

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)
PROBE = "probe.network.inspect_tcp_integrity_errors"
TARGET = "observation.network.tcp_retransmissions"
SCOPE = {
    "boundaries": ["boundary.application.external_dependency"],
    "attributes": {"service": "checkout-api", "dependency": "stripe"},
}


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class ProbeWorkflowReconciliationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def setup_workflow(self, directory: Path) -> tuple[dict, Path, Path, dict, dict]:
        demo = build_demo(root=ROOT)
        evidence = demo["runtime_evidence"]
        evidence_path = directory / "runtime-evidence.json"
        session_dir = directory / "sessions"
        source_path = directory / "proc-net-snmp"
        source_path.write_text(tcp_snmp(0), encoding="utf-8")
        self.write_json(evidence_path, evidence)

        session = begin_probe_session(
            incident_id=evidence["incident_id"],
            probe_id=PROBE,
            concepts=self.concepts,
            source_path=source_path,
            scope=SCOPE,
            started_at=START,
        )
        binding = {
            "schema_version": "0.1",
            "kind": "mcp_probe_binding",
            "session_id": session["session_id"],
            "incident_id": evidence["incident_id"],
            "target": TARGET,
            "probe_id": PROBE,
            "scope": SCOPE,
            "diagnosis_revision": 1,
            "diagnosis_etag": "test-etag",
        }
        return evidence, evidence_path, session_dir, session, binding

    def test_orphan_session_blocks_discovery_until_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            evidence, evidence_path, session_dir, session, _binding = self.setup_workflow(directory)
            session_path = session_dir / f"{session['session_id']}.json"
            self.write_json(session_path, session)

            issues = scan_partial_probe_workflows(
                session_dir,
                incident_id=evidence["incident_id"],
            )
            self.assertEqual(1, len(issues))
            self.assertEqual("orphan_session", issues[0]["issue_kind"])
            self.assertEqual(session["session_id"], issues[0]["session_id"])

            with self.assertRaisesRegex(
                ProbeWorkflowPartialStateError,
                "partial probe workflow state requires reconciliation",
            ):
                discover_pending_probe_sessions(
                    session_dir=session_dir,
                    runtime_evidence_path=evidence_path,
                    concepts=self.concepts,
                    as_of=START + timedelta(seconds=1),
                )

            result = reconcile_partial_probe_workflow(
                session_dir,
                session["session_id"],
                reconciled_at=START + timedelta(seconds=2),
            )
            self.assertFalse(result["already_reconciled"])
            self.assertEqual("discard_partial_state", result["resolution"])
            self.assertTrue(session_path.exists())
            self.assertTrue(reconciliation_path(session_dir, session["session_id"]).exists())
            self.assertEqual([], scan_partial_probe_workflows(session_dir))
            self.assertEqual(
                [],
                discover_pending_probe_sessions(
                    session_dir=session_dir,
                    runtime_evidence_path=evidence_path,
                    concepts=self.concepts,
                    as_of=START + timedelta(seconds=3),
                ),
            )

            repeated = reconcile_partial_probe_workflow(session_dir, session["session_id"])
            self.assertTrue(repeated["already_reconciled"])

    def test_reconciliation_marker_is_bound_to_exact_file_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            evidence, _evidence_path, session_dir, session, _binding = self.setup_workflow(directory)
            session_path = session_dir / f"{session['session_id']}.json"
            self.write_json(session_path, session)
            reconcile_partial_probe_workflow(
                session_dir,
                session["session_id"],
                reconciled_at=START,
            )
            self.assertEqual([], scan_partial_probe_workflows(session_dir))

            session_path.write_text(
                session_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            issues = scan_partial_probe_workflows(
                session_dir,
                incident_id=evidence["incident_id"],
            )
            self.assertEqual(1, len(issues))
            self.assertEqual("orphan_session", issues[0]["issue_kind"])

    def test_orphan_binding_can_be_discarded_without_inventing_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            evidence, evidence_path, session_dir, session, binding = self.setup_workflow(directory)
            binding_path = session_dir / f"{session['session_id']}.binding.json"
            self.write_json(binding_path, binding)

            issues = scan_partial_probe_workflows(
                session_dir,
                incident_id=evidence["incident_id"],
            )
            self.assertEqual(1, len(issues))
            self.assertEqual("orphan_binding", issues[0]["issue_kind"])

            reconcile_partial_probe_workflow(session_dir, session["session_id"], reconciled_at=START)
            self.assertTrue(binding_path.exists())
            self.assertFalse((session_dir / f"{session['session_id']}.json").exists())
            self.assertEqual(
                [],
                discover_pending_probe_sessions(
                    session_dir=session_dir,
                    runtime_evidence_path=evidence_path,
                    concepts=self.concepts,
                    as_of=START + timedelta(seconds=1),
                ),
            )

    def test_complete_session_binding_pair_remains_a_normal_pending_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            _evidence, evidence_path, session_dir, session, binding = self.setup_workflow(directory)
            self.write_json(session_dir / f"{session['session_id']}.json", session)
            self.write_json(session_dir / f"{session['session_id']}.binding.json", binding)

            self.assertEqual([], scan_partial_probe_workflows(session_dir))
            pending = discover_pending_probe_sessions(
                session_dir=session_dir,
                runtime_evidence_path=evidence_path,
                concepts=self.concepts,
                as_of=START + timedelta(seconds=1),
            )
            self.assertEqual(1, len(pending))
            self.assertEqual(session["session_id"], pending[0]["session_id"])
            self.assertEqual("active", pending[0]["lifecycle_state"])

    def test_cli_status_and_discard_emit_machine_readable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            _evidence, _evidence_path, session_dir, session, _binding = self.setup_workflow(directory)
            self.write_json(session_dir / f"{session['session_id']}.json", session)

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["status", "--session-dir", str(session_dir)])
            self.assertEqual(0, exit_code)
            status = json.loads(output.getvalue())
            self.assertTrue(status["recovery_required"])
            self.assertEqual(1, status["issue_count"])

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "discard",
                        "--session-dir",
                        str(session_dir),
                        "--session-id",
                        session["session_id"],
                    ]
                )
            self.assertEqual(0, exit_code)
            discarded = json.loads(output.getvalue())
            self.assertEqual("discard_partial_state", discarded["resolution"])
            self.assertFalse(discarded["already_reconciled"])


if __name__ == "__main__":
    unittest.main()

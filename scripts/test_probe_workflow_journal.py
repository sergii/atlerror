#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from causal_projection import load_concepts, load_edges
from demo_checkout_stripe import build_demo
from diagnosis_http_api import DiagnosisSnapshotReader
from mcp_probe_recovery_tool import RecoveryAwareProbeToolController
from mcp_probe_tools import ABANDON_TOOL_NAME, BEGIN_TOOL_NAME, FINISH_TOOL_NAME
from probe_workflow_journal import (
    append_probe_workflow_event,
    journal_path,
    verify_probe_workflow_journal,
)
from probe_workflow_reconciliation import scan_partial_probe_workflows

ROOT = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
SESSION_A = "probe-session.0123456789abcdef"
SESSION_B = "probe-session.fedcba9876543210"


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class ProbeWorkflowJournalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def build_controller(self, directory: Path) -> tuple[RecoveryAwareProbeToolController, Path]:
        demo = build_demo(root=ROOT)
        evidence_path = directory / "runtime-evidence.json"
        snapshot_path = directory / "diagnosis.json"
        source_path = directory / "proc-net-snmp"
        session_dir = directory / "sessions"
        self.write_json(evidence_path, demo["runtime_evidence"])
        self.write_json(snapshot_path, demo["diagnosis"])
        source_path.write_text(tcp_snmp(0), encoding="utf-8")
        controller = RecoveryAwareProbeToolController(
            reader=DiagnosisSnapshotReader(snapshot_path),
            runtime_evidence_path=evidence_path,
            snapshot_path=snapshot_path,
            concepts=self.concepts,
            edges=self.edges,
            session_dir=session_dir,
            source_path=source_path,
            clock=lambda: CLOCK,
        )
        return controller, source_path

    def test_append_is_hash_chained_verified_and_retry_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir = Path(directory_name)
            first = append_probe_workflow_event(
                session_dir,
                event_type="begin",
                incident_id="incident.test",
                session_id=SESSION_A,
                data={"target": TARGET},
                recorded_at=CLOCK,
            )
            second = append_probe_workflow_event(
                session_dir,
                event_type="finish",
                incident_id="incident.test",
                session_id=SESSION_A,
                data={"evidence_instance_id": "evidence.test"},
                recorded_at=CLOCK + timedelta(seconds=2),
            )
            repeated = append_probe_workflow_event(
                session_dir,
                event_type="finish",
                incident_id="incident.test",
                session_id=SESSION_A,
                data={"evidence_instance_id": "evidence.test"},
                recorded_at=CLOCK + timedelta(seconds=20),
            )

            self.assertEqual(1, first["sequence"])
            self.assertIsNone(first["previous_hash"])
            self.assertEqual(2, second["sequence"])
            self.assertEqual(first["event_hash"], second["previous_hash"])
            self.assertTrue(repeated["already_recorded"])
            self.assertEqual(second["event_hash"], repeated["event_hash"])

            journal = verify_probe_workflow_journal(session_dir)
            self.assertTrue(journal["verified"])
            self.assertEqual(2, journal["event_count"])
            self.assertEqual(second["event_hash"], journal["head_hash"])

    def test_conflicting_duplicate_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir = Path(directory_name)
            append_probe_workflow_event(
                session_dir,
                event_type="begin",
                incident_id="incident.test",
                session_id=SESSION_A,
                data={"target": "observation.one"},
                recorded_at=CLOCK,
            )
            with self.assertRaisesRegex(ValueError, "conflicting begin event"):
                append_probe_workflow_event(
                    session_dir,
                    event_type="begin",
                    incident_id="incident.test",
                    session_id=SESSION_A,
                    data={"target": "observation.two"},
                    recorded_at=CLOCK,
                )

    def test_tampering_breaks_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir = Path(directory_name)
            append_probe_workflow_event(
                session_dir,
                event_type="begin",
                incident_id="incident.test",
                session_id=SESSION_A,
                data={"target": TARGET},
                recorded_at=CLOCK,
            )
            path = journal_path(session_dir)
            event = json.loads(path.read_text(encoding="utf-8"))
            event["data"]["target"] = "observation.tampered"
            path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "event_hash mismatch"):
                verify_probe_workflow_journal(session_dir)

    def test_incident_filter_preserves_global_chain_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir = Path(directory_name)
            one = append_probe_workflow_event(
                session_dir,
                event_type="begin",
                incident_id="incident.one",
                session_id=SESSION_A,
                data={},
                recorded_at=CLOCK,
            )
            two = append_probe_workflow_event(
                session_dir,
                event_type="begin",
                incident_id="incident.two",
                session_id=SESSION_B,
                data={},
                recorded_at=CLOCK + timedelta(seconds=1),
            )
            filtered = verify_probe_workflow_journal(session_dir, incident_id="incident.one")
            self.assertEqual(1, filtered["event_count"])
            self.assertEqual(2, filtered["total_event_count"])
            self.assertEqual(two["event_hash"], filtered["head_hash"])
            self.assertEqual(one["event_hash"], filtered["events"][0]["event_hash"])

    def test_mcp_begin_and_finish_write_exactly_one_event_each(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            controller, source_path = self.build_controller(directory)

            begun = controller.call(BEGIN_TOOL_NAME, {"target": TARGET})
            self.assertEqual(1, begun["journal_event"]["sequence"])
            self.assertFalse(begun["journal_event"]["already_recorded"])
            session_id = begun["session_id"]

            source_path.write_text(tcp_snmp(3), encoding="utf-8")
            finished = controller.call(FINISH_TOOL_NAME, {"sessionId": session_id})
            self.assertEqual(2, finished["journal_event"]["sequence"])
            self.assertFalse(finished["journal_event"]["already_recorded"])

            retried = controller.call(FINISH_TOOL_NAME, {"sessionId": session_id})
            self.assertTrue(retried["already_completed"])
            self.assertTrue(retried["journal_event"]["already_recorded"])

            journal = verify_probe_workflow_journal(controller.session_dir)
            self.assertEqual(["begin", "finish"], [event["event_type"] for event in journal["events"]])
            self.assertEqual(
                finished["evidence_instance_id"],
                journal["events"][1]["data"]["evidence_instance_id"],
            )
            self.assertEqual("observed", journal["events"][1]["data"]["observation"]["state"])

    def test_abandon_and_reconcile_are_journaled_without_creating_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            controller, _source_path = self.build_controller(directory)
            evidence_before = controller.runtime_evidence_path.read_bytes()

            begun = controller.call(BEGIN_TOOL_NAME, {"target": TARGET})
            abandoned = controller.call(ABANDON_TOOL_NAME, {"sessionId": begun["session_id"]})
            self.assertFalse(abandoned["journal_event"]["already_recorded"])
            self.assertEqual(evidence_before, controller.runtime_evidence_path.read_bytes())

            orphan_id = SESSION_A
            binding = {
                "schema_version": "0.1",
                "kind": "mcp_probe_binding",
                "session_id": orphan_id,
                "incident_id": json.loads(evidence_before)["incident_id"],
                "target": TARGET,
                "probe_id": "probe.network.inspect_tcp_integrity_errors",
                "scope": None,
                "diagnosis_revision": 1,
                "diagnosis_etag": "test-etag",
            }
            self.write_json(controller.session_dir / f"{orphan_id}.binding.json", binding)
            issue = next(
                issue
                for issue in scan_partial_probe_workflows(controller.session_dir)
                if issue["session_id"] == orphan_id
            )
            reconciled = controller.call(
                "atlerror.probe.reconcile_partial",
                {"sessionId": orphan_id, "fingerprint": issue["fingerprint"]},
            )
            self.assertFalse(reconciled["journal_event"]["already_recorded"])
            self.assertEqual(evidence_before, controller.runtime_evidence_path.read_bytes())

            journal = verify_probe_workflow_journal(controller.session_dir)
            self.assertEqual(
                ["begin", "abandon", "reconcile"],
                [event["event_type"] for event in journal["events"]],
            )


if __name__ == "__main__":
    unittest.main()

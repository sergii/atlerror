#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from causal_projection import load_concepts
from demo_checkout_stripe import build_demo
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    CLIENT_CAPABILITIES_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROBE_WORKFLOW_HISTORY_URI,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from probe_execution import begin_probe_session, finish_probe_session
from probe_session_state import probe_abandonment_path
from probe_workflow_history import build_probe_workflow_history, validate_probe_workflow_history
from probe_workflow_reconciliation import reconcile_partial_probe_workflow
from runtime_evidence import format_timestamp

ROOT = Path(__file__).resolve().parents[1]
INCIDENT_ID = "incident.history.test"
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"
AS_OF = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class ProbeWorkflowHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_pair(
        self,
        session_dir: Path,
        source_path: Path,
        *,
        started_at: datetime,
        diagnosis_revision: int,
    ) -> dict:
        session = begin_probe_session(
            incident_id=INCIDENT_ID,
            probe_id=PROBE,
            concepts=self.concepts,
            source_path=source_path,
            started_at=started_at,
        )
        self.write_json(session_dir / f"{session['session_id']}.json", session)
        binding = {
            "schema_version": "0.1",
            "kind": "mcp_probe_binding",
            "session_id": session["session_id"],
            "incident_id": INCIDENT_ID,
            "target": TARGET,
            "probe_id": PROBE,
            "scope": None,
            "diagnosis_revision": diagnosis_revision,
            "diagnosis_etag": f"etag-{diagnosis_revision}",
        }
        self.write_json(session_dir / f"{session['session_id']}.binding.json", binding)
        return session

    def build_fixture(self, directory: Path) -> tuple[Path, Path, dict[str, str]]:
        session_dir = directory / "sessions"
        evidence_path = directory / "runtime-evidence.json"
        source_path = directory / "proc-net-snmp"
        source_path.write_text(tcp_snmp(0), encoding="utf-8")

        completed = self.write_pair(
            session_dir,
            source_path,
            started_at=AS_OF - timedelta(minutes=10),
            diagnosis_revision=1,
        )
        source_path.write_text(tcp_snmp(3), encoding="utf-8")
        completed_evidence = finish_probe_session(
            completed,
            self.concepts,
            finished_at=AS_OF - timedelta(minutes=9),
        )
        self.write_json(evidence_path, completed_evidence)

        source_path.write_text(tcp_snmp(3), encoding="utf-8")
        abandoned = self.write_pair(
            session_dir,
            source_path,
            started_at=AS_OF - timedelta(minutes=8),
            diagnosis_revision=2,
        )
        self.write_json(
            probe_abandonment_path(session_dir, abandoned["session_id"]),
            {
                "schema_version": "0.1",
                "kind": "mcp_probe_abandonment",
                "session_id": abandoned["session_id"],
                "incident_id": INCIDENT_ID,
                "abandoned_at": format_timestamp(AS_OF - timedelta(minutes=7)),
                "reason": "operator_abandoned",
            },
        )

        active = self.write_pair(
            session_dir,
            source_path,
            started_at=AS_OF - timedelta(minutes=5),
            diagnosis_revision=3,
        )
        expired = self.write_pair(
            session_dir,
            source_path,
            started_at=AS_OF - timedelta(minutes=20),
            diagnosis_revision=4,
        )

        reconciled_id = "probe-session.1111111111111111"
        self.write_json(
            session_dir / f"{reconciled_id}.binding.json",
            {
                "schema_version": "0.1",
                "kind": "mcp_probe_binding",
                "session_id": reconciled_id,
                "incident_id": INCIDENT_ID,
                "target": TARGET,
                "probe_id": PROBE,
                "scope": None,
                "diagnosis_revision": 5,
                "diagnosis_etag": "etag-5",
            },
        )
        reconcile_partial_probe_workflow(
            session_dir,
            reconciled_id,
            reconciled_at=AS_OF - timedelta(minutes=3),
        )

        recovery = begin_probe_session(
            incident_id=INCIDENT_ID,
            probe_id=PROBE,
            concepts=self.concepts,
            source_path=source_path,
            started_at=AS_OF - timedelta(minutes=2),
        )
        self.write_json(session_dir / f"{recovery['session_id']}.json", recovery)

        return session_dir, evidence_path, {
            "completed": completed["session_id"],
            "abandoned": abandoned["session_id"],
            "active": active["session_id"],
            "expired": expired["session_id"],
            "reconciled": reconciled_id,
            "recovery": recovery["session_id"],
        }

    def test_history_projects_all_workflow_terminal_and_pending_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            session_dir, evidence_path, ids = self.build_fixture(directory)
            evidence_before = evidence_path.read_bytes()
            artifacts_before = {
                path.name: path.read_bytes()
                for path in session_dir.iterdir()
                if path.is_file()
            }

            history = build_probe_workflow_history(
                session_dir=session_dir,
                runtime_evidence_path=evidence_path,
                concepts=self.concepts,
                incident_id=INCIDENT_ID,
                as_of=AS_OF,
            )
            validate_probe_workflow_history(history)
            by_id = {entry["session_id"]: entry for entry in history["sessions"]}

            self.assertEqual("completed", by_id[ids["completed"]]["status"])
            self.assertEqual("observed", by_id[ids["completed"]]["outcome"]["state"])
            self.assertEqual("abandoned", by_id[ids["abandoned"]]["status"])
            self.assertEqual("operator_abandoned", by_id[ids["abandoned"]]["abandonment"]["reason"])
            self.assertEqual("active", by_id[ids["active"]]["status"])
            self.assertEqual("expired", by_id[ids["expired"]]["status"])
            self.assertEqual("reconciled_partial", by_id[ids["reconciled"]]["status"])
            self.assertEqual("orphan_binding", by_id[ids["reconciled"]]["recovery"]["issue_kind"])
            self.assertEqual("recovery_required", by_id[ids["recovery"]]["status"])
            self.assertEqual("orphan_session", by_id[ids["recovery"]]["recovery"]["issue_kind"])

            self.assertEqual(
                {
                    "active": 1,
                    "expired": 1,
                    "completed": 1,
                    "abandoned": 1,
                    "reconciled_partial": 1,
                    "recovery_required": 1,
                },
                history["summary"],
            )
            self.assertEqual(evidence_before, evidence_path.read_bytes())
            artifacts_after = {
                path.name: path.read_bytes()
                for path in session_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(artifacts_before, artifacts_after)

            repeated = build_probe_workflow_history(
                session_dir=session_dir,
                runtime_evidence_path=evidence_path,
                concepts=self.concepts,
                incident_id=INCIDENT_ID,
                as_of=AS_OF,
            )
            self.assertEqual(history, repeated)

    def test_history_refuses_incident_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            session_dir, evidence_path, _ids = self.build_fixture(Path(directory_name))
            with self.assertRaisesRegex(ValueError, "requested history incident differs"):
                build_probe_workflow_history(
                    session_dir=session_dir,
                    runtime_evidence_path=evidence_path,
                    concepts=self.concepts,
                    incident_id="incident.other",
                    as_of=AS_OF,
                )

    def test_mcp_history_resource_is_conditional_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            session_dir, evidence_path, _ids = self.build_fixture(directory)
            demo = build_demo(root=ROOT)
            snapshot = demo["diagnosis"]
            snapshot["incident_id"] = INCIDENT_ID
            snapshot_path = directory / "diagnosis.json"
            self.write_json(snapshot_path, snapshot)
            reader = DiagnosisSnapshotReader(snapshot_path)

            without_history = DiagnosisMcpServer(reader)
            listed_without = without_history.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/list",
                    "params": {
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                            CLIENT_CAPABILITIES_META_KEY: {},
                        }
                    },
                }
            )
            self.assertNotIn(
                PROBE_WORKFLOW_HISTORY_URI,
                [resource["uri"] for resource in listed_without["result"]["resources"]],
            )

            server = DiagnosisMcpServer(
                reader,
                probe_history_provider=lambda incident_id: build_probe_workflow_history(
                    session_dir=session_dir,
                    runtime_evidence_path=evidence_path,
                    concepts=self.concepts,
                    incident_id=incident_id,
                    as_of=AS_OF,
                ),
            )
            listed = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/list",
                    "params": {
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                            CLIENT_CAPABILITIES_META_KEY: {},
                        }
                    },
                }
            )
            self.assertIn(
                PROBE_WORKFLOW_HISTORY_URI,
                [resource["uri"] for resource in listed["result"]["resources"]],
            )

            read = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/read",
                    "params": {
                        "uri": PROBE_WORKFLOW_HISTORY_URI,
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                            CLIENT_CAPABILITIES_META_KEY: {},
                        },
                    },
                }
            )
            self.assertNotIn("error", read)
            self.assertEqual(0, read["result"]["ttlMs"])
            self.assertEqual("private", read["result"]["cacheScope"])
            history = json.loads(read["result"]["contents"][0]["text"])
            self.assertEqual("probe_workflow_history", history["kind"])
            self.assertEqual(INCIDENT_ID, history["incident_id"])
            self.assertEqual(6, len(history["sessions"]))


if __name__ == "__main__":
    unittest.main()

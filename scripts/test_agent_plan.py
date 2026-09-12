#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent_plan import BEGIN_RECOMMENDED_OPERATION, build_agent_plan
from causal_projection import load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    AGENT_PLAN_URI,
    CLIENT_CAPABILITIES_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from live_diagnosis import build_diagnosis_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"
EXECUTOR = "executor.linux.proc_net_snmp.tcp_inerrs"


class _EnabledProbeTools:
    pass


class AgentPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            evidence = yaml.safe_load(handle)
        cls.evidence = copy.deepcopy(evidence)
        cls.evidence["instances"] = [
            instance
            for instance in cls.evidence["instances"]
            if instance["observation"] == TARGET
        ]

    def build_snapshot(self) -> dict:
        snapshot = build_diagnosis_snapshot(
            copy.deepcopy(self.evidence),
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=9,
        )
        diagnosis = self.retransmission_diagnosis(snapshot)
        annotation = diagnosis["probe_execution"]
        annotation.update(
            {
                "probe_id": PROBE,
                "registered": True,
                "executable_here": True,
                "unavailable_reason": None,
                "executor": {"id": EXECUTOR, "platform": "linux"},
                "capability": "capability.network.inspect_tcp_integrity_errors",
                "observation": "observation.network.tcp_integrity_errors",
                "source": "/proc/net/snmp",
                "policy": {"classification": "delta_positive"},
            }
        )
        return snapshot

    @staticmethod
    def retransmission_diagnosis(snapshot: dict) -> dict:
        for partition in snapshot["partitions"]:
            for diagnosis in partition["diagnoses"]:
                if diagnosis["target"] == TARGET:
                    return diagnosis
        raise AssertionError("retransmission diagnosis not found")

    @staticmethod
    def retransmission_step(plan: dict) -> dict:
        return next(step for step in plan["steps"] if step["target"] == TARGET)

    @staticmethod
    def write_snapshot(path: Path, snapshot: dict) -> None:
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
        }

    def test_executable_recommendation_is_blocked_until_active_tools_are_enabled(self) -> None:
        plan = build_agent_plan(self.build_snapshot(), active_execution_enabled=False)
        step = self.retransmission_step(plan)

        self.assertEqual("agent_plan", plan["kind"])
        self.assertFalse(plan["active_execution_enabled"])
        self.assertEqual("blocked", step["state"])
        self.assertEqual("active_execution_disabled", step["reason"])
        self.assertEqual(PROBE, step["recommended_probe"])
        self.assertEqual(EXECUTOR, step["executor_id"])
        self.assertEqual(BEGIN_RECOMMENDED_OPERATION, step["operation"])
        self.assertEqual(TARGET, step["arguments"]["target"])
        self.assertEqual(
            self.build_snapshot()["partitions"][0]["scope"],
            step["arguments"]["scope"],
        )
        self.assertFalse(step["allowed"])
        self.assertTrue(step["requires_opt_in"])
        self.assertEqual("enable_readonly_probe_tools", step["fallback"])

    def test_executable_recommendation_becomes_actionable_when_tools_are_enabled(self) -> None:
        plan = build_agent_plan(self.build_snapshot(), active_execution_enabled=True)
        step = self.retransmission_step(plan)

        self.assertEqual("actionable", step["state"])
        self.assertEqual("ready_to_begin_recommended", step["reason"])
        self.assertTrue(step["allowed"])
        self.assertFalse(step["requires_opt_in"])
        self.assertEqual("none", step["fallback"])
        self.assertEqual(1, plan["summary"]["actionable"])

    def test_unavailable_executor_is_blocked_without_changing_recommendation(self) -> None:
        snapshot = self.build_snapshot()
        diagnosis = self.retransmission_diagnosis(snapshot)
        diagnosis["probe_execution"]["executable_here"] = False
        diagnosis["probe_execution"]["unavailable_reason"] = "source does not exist: /proc/net/snmp"

        plan = build_agent_plan(snapshot, active_execution_enabled=True)
        step = self.retransmission_step(plan)
        self.assertEqual("blocked", step["state"])
        self.assertEqual("executor_unavailable", step["reason"])
        self.assertEqual(PROBE, step["recommended_probe"])
        self.assertFalse(step["allowed"])
        self.assertEqual("restore_executor_availability", step["fallback"])
        self.assertIn("source does not exist", step["unavailable_reason"])

    def test_unregistered_probe_requests_external_evidence(self) -> None:
        snapshot = self.build_snapshot()
        diagnosis = self.retransmission_diagnosis(snapshot)
        diagnosis["probe_execution"].update(
            {
                "registered": False,
                "executable_here": False,
                "unavailable_reason": "no_registered_executor",
                "executor": None,
                "capability": None,
                "observation": None,
                "source": None,
                "policy": {},
            }
        )

        step = self.retransmission_step(
            build_agent_plan(snapshot, active_execution_enabled=True)
        )
        self.assertEqual("no_executor", step["state"])
        self.assertEqual("no_registered_executor", step["reason"])
        self.assertIsNone(step["operation"])
        self.assertEqual("gather_external_evidence", step["fallback"])

    def test_no_probe_states_distinguish_single_candidate_from_catalog_gap(self) -> None:
        snapshot = self.build_snapshot()
        diagnosis = self.retransmission_diagnosis(snapshot)
        diagnosis["probe_ranking"]["found"] = False
        diagnosis["probe_ranking"]["probes"] = []
        diagnosis["probe_ranking"]["not_found_reason"] = "fewer_than_two_candidates"
        diagnosis["probe_execution"].update(
            {
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
        )
        step = self.retransmission_step(
            build_agent_plan(snapshot, active_execution_enabled=True)
        )
        self.assertEqual("no_probe_needed", step["state"])
        self.assertEqual("none", step["fallback"])

        diagnosis["probe_ranking"]["not_found_reason"] = "no_discriminating_probe"
        step = self.retransmission_step(
            build_agent_plan(snapshot, active_execution_enabled=True)
        )
        self.assertEqual("no_discriminating_probe", step["state"])
        self.assertEqual("gather_external_evidence", step["fallback"])

    def test_mcp_agent_plan_reflects_process_level_tool_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnosis.json"
            self.write_snapshot(path, self.build_snapshot())
            reader = DiagnosisSnapshotReader(path)

            resource_only = DiagnosisMcpServer(reader)
            response = resource_only.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/read",
                    "params": {"uri": AGENT_PLAN_URI, "_meta": self.modern_meta()},
                }
            )
            document = json.loads(response["result"]["contents"][0]["text"])
            self.assertEqual("blocked", self.retransmission_step(document)["state"])
            self.assertFalse(document["active_execution_enabled"])
            self.assertEqual(0, response["result"]["ttlMs"])
            self.assertEqual("private", response["result"]["cacheScope"])

            active = DiagnosisMcpServer(reader, probe_tools=_EnabledProbeTools())
            response = active.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": AGENT_PLAN_URI, "_meta": self.modern_meta()},
                }
            )
            document = json.loads(response["result"]["contents"][0]["text"])
            self.assertEqual("actionable", self.retransmission_step(document)["state"])
            self.assertTrue(document["active_execution_enabled"])


if __name__ == "__main__":
    unittest.main()

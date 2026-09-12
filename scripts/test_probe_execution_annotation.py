#!/usr/bin/env python3

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from causal_projection import load_concepts, load_edges
from live_diagnosis import build_diagnosis_snapshot
from probe_execution_annotation import annotate_recommended_probe_execution
from probe_executor_registry import (
    TCP_INTEGRITY_PROBE_ID,
    ProbeExecutorRegistry,
    default_executor_registry,
)
from probe_executor_runtime import build_probe_execution_capabilities

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"


def snmp_text(inerrs: int = 0) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class ProbeExecutionAnnotationTest(unittest.TestCase):
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

    @staticmethod
    def diagnosis(snapshot: dict) -> dict:
        return next(
            diagnosis
            for partition in snapshot["partitions"]
            for diagnosis in partition["diagnoses"]
            if diagnosis["target"] == TARGET
        )

    def build_snapshot(self, registry: ProbeExecutorRegistry) -> dict:
        return build_diagnosis_snapshot(
            copy.deepcopy(self.evidence),
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=1,
            executor_registry=registry,
        )

    def test_live_diagnosis_marks_top_recommended_probe_executable_here(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(), encoding="utf-8")
            registry = default_executor_registry({TCP_INTEGRITY_PROBE_ID: source})
            diagnosis = self.diagnosis(self.build_snapshot(registry))

        self.assertEqual(
            TCP_INTEGRITY_PROBE_ID,
            diagnosis["probe_ranking"]["probes"][0]["probe"]["id"],
        )
        execution = diagnosis["probe_execution"]
        self.assertFalse(execution["affects_ranking"])
        self.assertEqual(TCP_INTEGRITY_PROBE_ID, execution["probe_id"])
        self.assertTrue(execution["registered"])
        self.assertTrue(execution["executable_here"])
        self.assertIsNone(execution["unavailable_reason"])
        self.assertEqual("executor.linux.proc_net_snmp.tcp_inerrs", execution["executor"]["id"])
        self.assertEqual("capability.network.inspect_tcp_integrity_errors", execution["capability"])
        self.assertEqual("observation.network.tcp_integrity_errors", execution["observation"])
        self.assertEqual(str(source), execution["source"])

    def test_unavailable_executor_does_not_change_probe_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            available_source = Path(directory) / "snmp"
            available_source.write_text(snmp_text(), encoding="utf-8")
            missing_source = Path(directory) / "missing-snmp"

            available = self.diagnosis(
                self.build_snapshot(
                    default_executor_registry({TCP_INTEGRITY_PROBE_ID: available_source})
                )
            )
            unavailable = self.diagnosis(
                self.build_snapshot(
                    default_executor_registry({TCP_INTEGRITY_PROBE_ID: missing_source})
                )
            )

        self.assertEqual(available["probe_ranking"], unavailable["probe_ranking"])
        self.assertTrue(available["probe_execution"]["executable_here"])
        self.assertFalse(unavailable["probe_execution"]["executable_here"])
        self.assertIn("source does not exist", unavailable["probe_execution"]["unavailable_reason"])

    def test_unregistered_top_probe_is_explicit_without_affecting_ranking(self) -> None:
        base = default_executor_registry()
        without_integrity = ProbeExecutorRegistry(
            [executor for executor in base.executors() if executor.probe_id != TCP_INTEGRITY_PROBE_ID]
        )
        diagnosis = self.diagnosis(self.build_snapshot(without_integrity))

        self.assertEqual(TCP_INTEGRITY_PROBE_ID, diagnosis["probe_ranking"]["probes"][0]["probe"]["id"])
        execution = diagnosis["probe_execution"]
        self.assertFalse(execution["affects_ranking"])
        self.assertEqual(TCP_INTEGRITY_PROBE_ID, execution["probe_id"])
        self.assertFalse(execution["registered"])
        self.assertFalse(execution["executable_here"])
        self.assertEqual("no_registered_executor", execution["unavailable_reason"])
        self.assertIsNone(execution["executor"])
        self.assertIsNone(execution["source"])

    def test_no_recommendation_uses_explicit_not_applicable_annotation(self) -> None:
        capabilities = build_probe_execution_capabilities(self.concepts)
        annotation = annotate_recommended_probe_execution(
            {
                "schema_version": "0.1",
                "kind": "probe_ranking",
                "found": False,
                "probes": [],
            },
            capabilities,
        )
        self.assertFalse(annotation["affects_ranking"])
        self.assertIsNone(annotation["probe_id"])
        self.assertIsNone(annotation["registered"])
        self.assertIsNone(annotation["executable_here"])
        self.assertEqual("no_recommended_probe", annotation["unavailable_reason"])


if __name__ == "__main__":
    unittest.main()

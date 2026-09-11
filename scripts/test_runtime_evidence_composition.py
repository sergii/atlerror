#!/usr/bin/env python3

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path

from causal_projection import load_concepts, load_edges
from live_diagnosis import build_diagnosis_snapshot
from opentelemetry_trace_adapter import build_runtime_evidence as build_trace_evidence
from opentelemetry_trace_adapter import load_adapter as load_trace_adapter
from opentelemetry_trace_adapter import load_payload
from prometheus_adapter import build_runtime_evidence as build_prometheus_evidence
from prometheus_adapter import load_adapter as load_prometheus_adapter
from prometheus_adapter import load_response_file
from runtime_evidence_composition import compose_runtime_evidence

ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS_ADAPTER = ROOT / "examples" / "adapters" / "prometheus" / "external-dependency.yaml"
PROMETHEUS_RESPONSE = (
    ROOT
    / "examples"
    / "telemetry"
    / "prometheus"
    / "external-dependency-tcp-retransmissions.json"
)
TRACE_ADAPTER = ROOT / "examples" / "adapters" / "opentelemetry" / "external-dependency.yaml"
TRACE_PAYLOAD = ROOT / "examples" / "telemetry" / "opentelemetry" / "external-dependency-trace.json"
AS_OF = datetime(2026, 9, 11, 16, 31, tzinfo=timezone.utc)
INCIDENT_ID = "incident.checkout.multi_source"


class RuntimeEvidenceCompositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        cls.prometheus_adapter = load_prometheus_adapter(PROMETHEUS_ADAPTER)
        cls.trace_adapter = load_trace_adapter(TRACE_ADAPTER)
        cls.trace_payload = load_payload(TRACE_PAYLOAD)
        cls.prometheus_response = load_response_file(PROMETHEUS_RESPONSE)

    def prometheus_evidence(self) -> dict:
        return build_prometheus_evidence(
            self.prometheus_adapter,
            {"tcp_retransmissions_rate": self.prometheus_response},
            self.concepts,
            incident_id=INCIDENT_ID,
            source_uri="http://prometheus.example",
        )

    def trace_evidence(self) -> dict:
        return build_trace_evidence(
            self.trace_adapter,
            self.trace_payload,
            self.concepts,
            incident_id=INCIDENT_ID,
            source_uri="otlp://checkout-api",
        )

    def test_prometheus_and_trace_evidence_compose_into_one_diagnosis_scope(self) -> None:
        composed = compose_runtime_evidence(
            [self.prometheus_evidence(), self.trace_evidence()],
            self.concepts,
        )

        self.assertEqual(INCIDENT_ID, composed["incident_id"])
        self.assertEqual(3, len(composed["instances"]))
        self.assertEqual(
            {"metric", "trace"},
            {instance["source"]["type"] for instance in composed["instances"]},
        )
        self.assertEqual(
            sorted(instance["id"] for instance in composed["instances"]),
            [instance["id"] for instance in composed["instances"]],
        )

        snapshot = build_diagnosis_snapshot(
            composed,
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=1,
        )
        self.assertEqual(1, len(snapshot["partitions"]))
        partition = snapshot["partitions"][0]
        self.assertEqual(
            {
                "boundaries": ["boundary.application.external_dependency"],
                "attributes": {"dependency": "stripe", "service": "checkout-api"},
            },
            partition["scope"],
        )
        self.assertEqual(
            {
                "observation.dependency.latency",
                "observation.network.connection_timeout",
                "observation.network.tcp_retransmissions",
            },
            set(partition["observed"]),
        )

        diagnoses = {item["target"]: item for item in partition["diagnoses"]}
        retransmission = diagnoses["observation.network.tcp_retransmissions"]
        self.assertEqual(
            "hypothesis.network.packet_loss",
            retransmission["ranking"]["candidates"][0]["source"]["id"],
        )
        self.assertTrue(retransmission["probe_ranking"]["found"])
        self.assertEqual(
            "probe.network.inspect_tcp_integrity_errors",
            retransmission["probe_ranking"]["probes"][0]["probe"]["id"],
        )
        self.assertEqual(
            "hypothesis.latency.external_dependency",
            diagnoses["observation.dependency.latency"]["ranking"]["candidates"][0]["source"]["id"],
        )
        self.assertEqual(
            "hypothesis.network.connection_timeout",
            diagnoses["observation.network.connection_timeout"]["ranking"]["candidates"][0]["source"]["id"],
        )

    def test_composition_is_order_independent(self) -> None:
        prometheus = self.prometheus_evidence()
        trace = self.trace_evidence()
        self.assertEqual(
            compose_runtime_evidence([prometheus, trace], self.concepts),
            compose_runtime_evidence([trace, prometheus], self.concepts),
        )

    def test_identical_instance_ids_are_deduplicated(self) -> None:
        prometheus = self.prometheus_evidence()
        composed = compose_runtime_evidence(
            [prometheus, copy.deepcopy(prometheus)],
            self.concepts,
        )
        self.assertEqual(1, len(composed["instances"]))

    def test_instance_id_collision_with_different_content_fails(self) -> None:
        prometheus = self.prometheus_evidence()
        changed = copy.deepcopy(prometheus)
        changed["instances"][0]["state"] = "absent"
        with self.assertRaisesRegex(ValueError, "instance id collision"):
            compose_runtime_evidence([prometheus, changed], self.concepts)

    def test_mismatched_incident_ids_fail_closed(self) -> None:
        prometheus = self.prometheus_evidence()
        trace = self.trace_evidence()
        trace["incident_id"] = "incident.other"
        with self.assertRaisesRegex(ValueError, "same incident_id"):
            compose_runtime_evidence([prometheus, trace], self.concepts)

    def test_composition_requires_multiple_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least two"):
            compose_runtime_evidence([self.prometheus_evidence()], self.concepts)


if __name__ == "__main__":
    unittest.main()

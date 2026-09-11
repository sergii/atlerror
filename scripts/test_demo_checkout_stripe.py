#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demo_checkout_stripe import DEFAULT_AS_OF, build_demo, run_demo

ROOT = Path(__file__).resolve().parents[1]


class CheckoutStripeDemoTest(unittest.TestCase):
    def test_build_demo_combines_metric_and_trace_evidence_into_one_diagnosis(self) -> None:
        result = build_demo(root=ROOT)

        prometheus = result["prometheus_evidence"]
        trace = result["trace_evidence"]
        composed = result["runtime_evidence"]
        diagnosis = result["diagnosis"]

        self.assertEqual("incident.demo.checkout.stripe", composed["incident_id"])
        self.assertEqual(1, len(prometheus["instances"]))
        self.assertEqual(2, len(trace["instances"]))
        self.assertEqual(3, len(composed["instances"]))
        self.assertEqual(
            {"metric", "trace"},
            {instance["source"]["type"] for instance in composed["instances"]},
        )

        self.assertEqual(1, len(diagnosis["partitions"]))
        partition = diagnosis["partitions"][0]
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

        by_target = {item["target"]: item for item in partition["diagnoses"]}
        self.assertEqual(
            "hypothesis.latency.external_dependency",
            by_target["observation.dependency.latency"]["ranking"]["candidates"][0]["source"]["id"],
        )
        self.assertEqual(
            "hypothesis.network.connection_timeout",
            by_target["observation.network.connection_timeout"]["ranking"]["candidates"][0]["source"]["id"],
        )
        retransmission = by_target["observation.network.tcp_retransmissions"]
        self.assertEqual(
            "hypothesis.network.packet_loss",
            retransmission["ranking"]["candidates"][0]["source"]["id"],
        )
        self.assertTrue(retransmission["probe_ranking"]["found"])
        self.assertEqual(
            "probe.network.inspect_tcp_integrity_errors",
            retransmission["probe_ranking"]["probes"][0]["probe"]["id"],
        )
        self.assertEqual([], partition["unranked_observations"])

    def test_run_demo_writes_artifacts_and_verifies_http_and_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            result = run_demo(output_dir, root=ROOT)

            self.assertTrue(result["transport_status"]["http_verified"])
            self.assertTrue(result["transport_status"]["mcp_verified"])
            self.assertEqual("ready", result["transport_status"]["http_state"])
            self.assertEqual("ready", result["transport_status"]["mcp_state"])

            expected_files = {
                "prometheus-evidence.json",
                "opentelemetry-evidence.json",
                "runtime-evidence.json",
                "diagnosis.json",
                "demo-summary.json",
            }
            self.assertEqual(expected_files, {path.name for path in output_dir.iterdir()})

            persisted_diagnosis = json.loads(
                (output_dir / "diagnosis.json").read_text(encoding="utf-8")
            )
            persisted_summary = json.loads(
                (output_dir / "demo-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["diagnosis"], persisted_diagnosis)
            self.assertEqual(result["summary"], persisted_summary)

    def test_demo_is_deterministic_for_fixed_fixture_time(self) -> None:
        first = build_demo(root=ROOT, as_of=DEFAULT_AS_OF)
        second = build_demo(root=ROOT, as_of=DEFAULT_AS_OF)
        self.assertEqual(first, second)

    def test_summary_exposes_top_hypotheses_and_next_probe_without_transport_details(self) -> None:
        summary = build_demo(root=ROOT)["summary"]
        self.assertEqual("demo_summary", summary["kind"])
        self.assertEqual("2026-09-11T16:31:00Z", summary["as_of"])
        self.assertNotIn("paths", summary)
        self.assertNotIn("transport_status", summary)

        diagnoses = {
            item["target"]: item
            for item in summary["partitions"][0]["diagnoses"]
        }
        self.assertEqual(
            "probe.network.inspect_tcp_integrity_errors",
            diagnoses["observation.network.tcp_retransmissions"]["next_probe"],
        )
        self.assertEqual(
            "hypothesis.network.packet_loss",
            diagnoses["observation.network.tcp_retransmissions"]["top_hypothesis"],
        )


if __name__ == "__main__":
    unittest.main()

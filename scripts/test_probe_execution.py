#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from causal_projection import ROOT, load_concepts, load_edges
from demo_checkout_stripe import DEFAULT_INCIDENT_ID, build_demo
from live_diagnosis import build_diagnosis_snapshot
from probe_execution import (
    SESSION_SCHEMA_PATH,
    SUPPORTED_OBSERVATION_ID,
    SUPPORTED_PROBE_ID,
    begin_probe_session,
    finish_probe_session,
    parse_tcp_inerrs,
)
from runtime_evidence import build_scope_query
from runtime_evidence_composition import compose_runtime_evidence


def snmp_text(inerrs: int) -> str:
    return (
        "Ip: Forwarding DefaultTTL InReceives\n"
        "Ip: 2 64 100\n"
        "Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails "
        "EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors\n"
        f"Tcp: 1 200 120000 -1 1 2 0 0 3 100 110 4 {inerrs} 0 0\n"
    )


class ProbeExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    def test_session_schema_exists(self) -> None:
        schema = json.loads(SESSION_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual("probe_execution_session", schema["properties"]["kind"]["const"])

    def test_parse_tcp_inerrs(self) -> None:
        self.assertEqual(7, parse_tcp_inerrs(snmp_text(7)))
        with self.assertRaisesRegex(ValueError, "does not contain a Tcp"):
            parse_tcp_inerrs("Ip: Forwarding\nIp: 2\n")

    def test_refuses_non_read_only_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(0), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refuses non-read-only probe"):
                begin_probe_session(
                    incident_id="incident.test",
                    probe_id="probe.network.measure_packet_delivery",
                    concepts=self.concepts,
                    source_path=source,
                    started_at=datetime(2026, 9, 11, 16, 31, tzinfo=timezone.utc),
                )

    def test_counter_delta_emits_observed_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(10), encoding="utf-8")
            scope = build_scope_query(
                boundaries=["boundary.application.external_dependency"],
                attributes=[("service", "checkout-api"), ("dependency", "stripe")],
            )
            session = begin_probe_session(
                incident_id="incident.test",
                probe_id=SUPPORTED_PROBE_ID,
                concepts=self.concepts,
                source_path=source,
                scope=scope,
                started_at=datetime(2026, 9, 11, 16, 31, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(10, session["baseline"]["value"])
            self.assertEqual("read_only", session["probe"]["risk"])

            source.write_text(snmp_text(13), encoding="utf-8")
            evidence = finish_probe_session(
                session,
                self.concepts,
                finished_at=datetime(2026, 9, 11, 16, 31, 10, tzinfo=timezone.utc),
            )
            instance = evidence["instances"][0]
            self.assertEqual(SUPPORTED_OBSERVATION_ID, instance["observation"])
            self.assertEqual("observed", instance["state"])
            self.assertEqual(3, instance["measurement"]["delta"])
            self.assertEqual("changed", instance["measurement"]["comparison"])
            self.assertEqual("probe", instance["source"]["type"])
            self.assertEqual(scope, instance["scope"])

    def test_unchanged_counter_emits_explicit_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(10), encoding="utf-8")
            session = begin_probe_session(
                incident_id="incident.test",
                probe_id=SUPPORTED_PROBE_ID,
                concepts=self.concepts,
                source_path=source,
                started_at=datetime(2026, 9, 11, 16, 31, 0, tzinfo=timezone.utc),
            )
            evidence = finish_probe_session(
                session,
                self.concepts,
                finished_at=datetime(2026, 9, 11, 16, 31, 10, tzinfo=timezone.utc),
            )
            instance = evidence["instances"][0]
            self.assertEqual("absent", instance["state"])
            self.assertEqual(0, instance["measurement"]["delta"])
            self.assertEqual("equal", instance["measurement"]["comparison"])

    def test_counter_reset_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(10), encoding="utf-8")
            session = begin_probe_session(
                incident_id="incident.test",
                probe_id=SUPPORTED_PROBE_ID,
                concepts=self.concepts,
                source_path=source,
                started_at=datetime(2026, 9, 11, 16, 31, 0, tzinfo=timezone.utc),
            )
            source.write_text(snmp_text(2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "counter may have reset"):
                finish_probe_session(
                    session,
                    self.concepts,
                    finished_at=datetime(2026, 9, 11, 16, 31, 10, tzinfo=timezone.utc),
                )

    def test_probe_result_closes_the_demo_feedback_loop(self) -> None:
        demo = build_demo(root=ROOT)
        scope = build_scope_query(
            boundaries=["boundary.application.external_dependency"],
            attributes=[("service", "checkout-api"), ("dependency", "stripe")],
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snmp"
            source.write_text(snmp_text(100), encoding="utf-8")
            session = begin_probe_session(
                incident_id=DEFAULT_INCIDENT_ID,
                probe_id=SUPPORTED_PROBE_ID,
                concepts=self.concepts,
                source_path=source,
                scope=scope,
                started_at=datetime(2026, 9, 11, 16, 31, 5, tzinfo=timezone.utc),
            )
            source.write_text(snmp_text(102), encoding="utf-8")
            probe_evidence = finish_probe_session(
                session,
                self.concepts,
                finished_at=datetime(2026, 9, 11, 16, 31, 10, tzinfo=timezone.utc),
            )

        composed = compose_runtime_evidence(
            [demo["runtime_evidence"], probe_evidence],
            self.concepts,
        )
        diagnosis = build_diagnosis_snapshot(
            composed,
            self.concepts,
            self.edges,
            as_of=datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc),
            evidence_revision=2,
        )
        self.assertEqual(1, len(diagnosis["partitions"]))
        partition = diagnosis["partitions"][0]
        self.assertIn(SUPPORTED_OBSERVATION_ID, partition["observed"])

        retransmission = next(
            item
            for item in partition["diagnoses"]
            if item["target"] == "observation.network.tcp_retransmissions"
        )
        self.assertEqual(
            "hypothesis.network.packet_corruption",
            retransmission["ranking"]["candidates"][0]["source"]["id"],
        )
        recommended_ids = [
            candidate["probe"]["id"]
            for candidate in retransmission["probe_ranking"]["probes"]
        ]
        self.assertNotIn(SUPPORTED_PROBE_ID, recommended_ids)


if __name__ == "__main__":
    unittest.main()

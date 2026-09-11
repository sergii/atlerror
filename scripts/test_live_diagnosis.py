#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges
from live_diagnosis import LiveDiagnosisEngine, build_diagnosis_snapshot
from live_diagnosis_watch import DiagnosisWatcher
from opentelemetry_trace_adapter import build_runtime_evidence, load_adapter, load_payload

ROOT = Path(__file__).resolve().parents[1]
NETWORK_EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
OTEL_ADAPTER_PATH = ROOT / "examples" / "adapters" / "opentelemetry" / "external-dependency.yaml"
OTEL_PAYLOAD_PATH = ROOT / "examples" / "telemetry" / "opentelemetry" / "external-dependency-trace.json"
RANKING_SCHEMA_PATH = ROOT / "schema" / "causal-ranking.schema.json"
NETWORK_AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)
OTEL_AS_OF = datetime(2026, 9, 11, 16, 31, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class LiveDiagnosisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with RANKING_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.ranking_schema = json.load(handle)
        cls.ranking_validator = Draft202012Validator(cls.ranking_schema)

    def network_evidence(self) -> dict:
        with NETWORK_EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def otel_evidence(self) -> dict:
        return build_runtime_evidence(
            load_adapter(OTEL_ADAPTER_PATH),
            load_payload(OTEL_PAYLOAD_PATH),
            self.concepts,
            incident_id="incident.checkout.stripe_timeout",
            source_uri="file://external-dependency-trace.json",
        )

    def assert_rankings_validate(self, snapshot: dict) -> None:
        for partition in snapshot["partitions"]:
            for diagnosis in partition["diagnoses"]:
                errors = sorted(
                    self.ranking_validator.iter_errors(diagnosis["ranking"]),
                    key=lambda error: list(error.path),
                )
                self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_network_evidence_becomes_one_semantic_scope_partition(self) -> None:
        snapshot = build_diagnosis_snapshot(
            self.network_evidence(),
            self.concepts,
            self.edges,
            as_of=NETWORK_AS_OF,
            evidence_revision=1,
        )
        self.assertEqual("diagnosis_snapshot", snapshot["kind"])
        self.assertEqual(1, len(snapshot["partitions"]))
        partition = snapshot["partitions"][0]
        self.assertEqual(
            {"boundaries": ["boundary.application.external_dependency"]},
            partition["scope"],
        )
        self.assertEqual(
            {
                "observation.network.tcp_integrity_errors",
                "observation.network.tcp_retransmissions",
            },
            set(partition["observed"]),
        )
        self.assertEqual(
            ["evidence.network.packet_loss.stale"],
            partition["stale_instance_ids"],
        )

        by_target = {item["target"]: item for item in partition["diagnoses"]}
        retransmission = by_target["observation.network.tcp_retransmissions"]["ranking"]
        self.assertEqual(
            "hypothesis.network.packet_corruption",
            retransmission["candidates"][0]["source"]["id"],
        )
        self.assert_rankings_validate(snapshot)

    def test_otel_trace_produces_latency_and_timeout_diagnoses(self) -> None:
        snapshot = build_diagnosis_snapshot(
            self.otel_evidence(),
            self.concepts,
            self.edges,
            as_of=OTEL_AS_OF,
            evidence_revision=4,
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
        by_target = {item["target"]: item for item in partition["diagnoses"]}

        latency = by_target["observation.dependency.latency"]["ranking"]
        self.assertEqual(
            "hypothesis.latency.external_dependency",
            latency["candidates"][0]["source"]["id"],
        )

        timeout = by_target["observation.network.connection_timeout"]["ranking"]
        self.assertEqual(
            "hypothesis.network.connection_timeout",
            timeout["candidates"][0]["source"]["id"],
        )
        self.assertEqual([], partition["unranked_observations"])
        self.assert_rankings_validate(snapshot)

    def test_absent_runtime_state_clears_active_diagnosis(self) -> None:
        evidence = self.otel_evidence()
        for instance in evidence["instances"]:
            instance["state"] = "absent"
        snapshot = build_diagnosis_snapshot(
            evidence,
            self.concepts,
            self.edges,
            as_of=OTEL_AS_OF,
            evidence_revision=5,
        )
        partition = snapshot["partitions"][0]
        self.assertEqual([], partition["observed"])
        self.assertEqual(
            {
                "observation.dependency.latency",
                "observation.network.connection_timeout",
            },
            set(partition["absent"]),
        )
        self.assertEqual([], partition["diagnoses"])

    def test_engine_reruns_on_evidence_revision_and_freshness_transition(self) -> None:
        clock = MutableClock(NETWORK_AS_OF)
        engine = LiveDiagnosisEngine(
            concepts=self.concepts,
            edges=self.edges,
            clock=clock,
        )
        evidence = self.network_evidence()

        first = engine.current(evidence, evidence_revision=1)
        self.assertEqual(1, engine.status()["diagnosis_runs_total"])
        self.assertTrue(first["partitions"][0]["diagnoses"])

        clock.value = datetime(2026, 9, 11, 14, 49, tzinfo=timezone.utc)
        engine.current(evidence, evidence_revision=1)
        self.assertEqual(1, engine.status()["diagnosis_runs_total"])

        engine.current(evidence, evidence_revision=2)
        self.assertEqual(2, engine.status()["diagnosis_runs_total"])

        clock.value = datetime(2026, 9, 11, 15, 2, tzinfo=timezone.utc)
        expired = engine.current(evidence, evidence_revision=2)
        self.assertEqual(3, engine.status()["diagnosis_runs_total"])
        self.assertEqual([], expired["partitions"][0]["observed"])
        self.assertEqual([], expired["partitions"][0]["diagnoses"])

    def test_engine_atomically_writes_diagnosis_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            engine = LiveDiagnosisEngine(
                concepts=self.concepts,
                edges=self.edges,
                snapshot_path=snapshot_path,
                clock=MutableClock(OTEL_AS_OF),
            )
            expected = engine.refresh(self.otel_evidence(), evidence_revision=1)
            self.assertEqual(
                expected,
                json.loads(snapshot_path.read_text(encoding="utf-8")),
            )
            self.assertFalse(snapshot_path.with_name("diagnosis.json.tmp").exists())

    def test_watcher_refreshes_only_for_changed_evidence_or_freshness(self) -> None:
        clock = MutableClock(NETWORK_AS_OF)
        engine = LiveDiagnosisEngine(
            concepts=self.concepts,
            edges=self.edges,
            clock=clock,
        )
        watcher = DiagnosisWatcher(
            receiver_url="http://receiver.example",
            engine=engine,
        )
        first = self.network_evidence()
        changed = copy.deepcopy(first)
        changed["description"] = "same incident with a changed evidence document"

        with patch(
            "live_diagnosis_watch.fetch_evidence",
            side_effect=[first, first, changed],
        ):
            first_status = watcher.tick()
            second_status = watcher.tick()
            third_status = watcher.tick()

        self.assertEqual("evidence_changed", first_status["state"])
        self.assertEqual("unchanged", second_status["state"])
        self.assertEqual("evidence_changed", third_status["state"])
        self.assertEqual(2, third_status["evidence_revision"])
        self.assertEqual(2, third_status["evidence_changes_total"])
        self.assertEqual(2, third_status["diagnosis_runs_total"])

    def test_watcher_waits_cleanly_when_receiver_has_no_evidence(self) -> None:
        engine = LiveDiagnosisEngine(
            concepts=self.concepts,
            edges=self.edges,
            clock=MutableClock(NETWORK_AS_OF),
        )
        watcher = DiagnosisWatcher(
            receiver_url="http://receiver.example",
            engine=engine,
        )
        with patch("live_diagnosis_watch.fetch_evidence", return_value=None):
            status = watcher.tick()
        self.assertEqual("waiting_for_evidence", status["state"])
        self.assertEqual(0, status["evidence_revision"])
        self.assertEqual(0, status["diagnosis_runs_total"])


if __name__ == "__main__":
    unittest.main()

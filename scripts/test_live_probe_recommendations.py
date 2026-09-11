#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from causal_projection import load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader, InvalidSnapshot
from diagnosis_mcp_server import (
    CLIENT_CAPABILITIES_META_KEY,
    CURRENT_DIAGNOSIS_URI,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from live_diagnosis import build_diagnosis_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)


class LiveProbeRecommendationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            cls.evidence = yaml.safe_load(handle)

    def retransmission_only_evidence(self) -> dict:
        document = copy.deepcopy(self.evidence)
        document["instances"] = [
            instance
            for instance in document["instances"]
            if instance["observation"] == "observation.network.tcp_retransmissions"
        ]
        return document

    def build_snapshot(self, revision: int = 1) -> dict:
        return build_diagnosis_snapshot(
            self.retransmission_only_evidence(),
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=revision,
        )

    @staticmethod
    def write_snapshot(path: Path, snapshot: dict) -> None:
        path.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def retransmission_diagnosis(snapshot: dict) -> dict:
        for partition in snapshot["partitions"]:
            for diagnosis in partition["diagnoses"]:
                if diagnosis["target"] == "observation.network.tcp_retransmissions":
                    return diagnosis
        raise AssertionError("retransmission diagnosis not found")

    def test_live_snapshot_embeds_recommended_next_probe(self) -> None:
        snapshot = self.build_snapshot()
        diagnosis = self.retransmission_diagnosis(snapshot)

        self.assertEqual(
            [
                "hypothesis.network.packet_loss",
                "hypothesis.network.packet_corruption",
            ],
            [candidate["source"]["id"] for candidate in diagnosis["ranking"]["candidates"]],
        )
        probe_ranking = diagnosis["probe_ranking"]
        self.assertTrue(probe_ranking["found"])
        self.assertIsNone(probe_ranking["not_found_reason"])
        self.assertEqual(
            "probe.network.inspect_tcp_integrity_errors",
            probe_ranking["probes"][0]["probe"]["id"],
        )
        self.assertEqual(
            ["observation.network.tcp_integrity_errors"],
            probe_ranking["probes"][0]["factors"]["discriminating_observations"],
        )

    def test_resolved_integrity_observation_is_not_recommended_again(self) -> None:
        snapshot = build_diagnosis_snapshot(
            copy.deepcopy(self.evidence),
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=2,
        )
        diagnosis = self.retransmission_diagnosis(snapshot)
        recommended_ids = [
            probe["probe"]["id"] for probe in diagnosis["probe_ranking"]["probes"]
        ]
        self.assertNotIn("probe.network.inspect_tcp_integrity_errors", recommended_ids)

    def test_http_reader_validates_and_summarizes_embedded_probe_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            snapshot = self.build_snapshot(revision=3)
            self.write_snapshot(snapshot_path, snapshot)
            reader = DiagnosisSnapshotReader(snapshot_path)

            loaded, _etag = reader.read()
            self.assertEqual(snapshot, loaded)
            status = reader.status()
            self.assertEqual(1, status["next_probe_recommendations"])

            broken = copy.deepcopy(snapshot)
            diagnosis = self.retransmission_diagnosis(broken)
            diagnosis["probe_ranking"]["probes"][0]["risk"] = "impossible"
            self.write_snapshot(snapshot_path, broken)
            with self.assertRaisesRegex(InvalidSnapshot, "embedded probe ranking validation failed"):
                reader.read()

    def test_mcp_current_resource_exposes_same_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            self.write_snapshot(snapshot_path, self.build_snapshot(revision=4))
            server = DiagnosisMcpServer(DiagnosisSnapshotReader(snapshot_path))
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/read",
                    "params": {
                        "uri": CURRENT_DIAGNOSIS_URI,
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                            CLIENT_CAPABILITIES_META_KEY: {},
                        },
                    },
                }
            )
            self.assertIsNotNone(response)
            document = json.loads(response["result"]["contents"][0]["text"])
            diagnosis = self.retransmission_diagnosis(document)
            self.assertEqual(
                "probe.network.inspect_tcp_integrity_errors",
                diagnosis["probe_ranking"]["probes"][0]["probe"]["id"],
            )


if __name__ == "__main__":
    unittest.main()

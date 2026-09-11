#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges, project_causes, project_path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "causal-projection.schema.json"


class CausalProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.edges = load_edges(ROOT)
        cls.concepts = load_concepts(ROOT)
        with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.schema = json.load(handle)
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assertValidProjection(self, projection: dict) -> None:
        errors = sorted(self.validator.iter_errors(projection), key=lambda error: list(error.path))
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_packet_corruption_to_transport_latency_path(self) -> None:
        projection = project_path(
            "hypothesis.network.packet_corruption",
            "observation.network.transport_latency",
            self.edges,
            self.concepts,
        )

        self.assertTrue(projection["found"])
        self.assertValidProjection(projection)
        path = projection["paths"][0]
        self.assertEqual(3, path["distance"])
        self.assertEqual(
            [
                "hypothesis.network.packet_corruption",
                "observation.network.tcp_integrity_errors",
                "observation.network.tcp_retransmissions",
                "observation.network.transport_latency",
            ],
            [node["id"] for node in path["nodes"]],
        )

    def test_reverse_causes_include_direct_and_transitive_antecedents(self) -> None:
        projection = project_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
        )

        self.assertTrue(projection["found"])
        self.assertValidProjection(projection)
        by_source = {path["source"]: path for path in projection["paths"]}
        self.assertEqual(1, by_source["hypothesis.network.packet_loss"]["distance"])
        self.assertEqual(1, by_source["observation.network.tcp_integrity_errors"]["distance"])
        self.assertEqual(2, by_source["hypothesis.network.packet_corruption"]["distance"])

    def test_reverse_causes_respect_max_depth(self) -> None:
        projection = project_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            max_depth=1,
        )

        self.assertValidProjection(projection)
        sources = {path["source"] for path in projection["paths"]}
        self.assertEqual(
            {
                "hypothesis.network.packet_loss",
                "observation.network.tcp_integrity_errors",
            },
            sources,
        )

    def test_missing_path_has_empty_projection(self) -> None:
        projection = project_path(
            "observation.network.transport_latency",
            "hypothesis.network.packet_loss",
            self.edges,
            self.concepts,
        )

        self.assertFalse(projection["found"])
        self.assertEqual([], projection["paths"])
        self.assertValidProjection(projection)


if __name__ == "__main__":
    unittest.main()

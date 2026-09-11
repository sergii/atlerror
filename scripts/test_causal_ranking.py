#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges
from causal_ranking import rank_causes

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "causal-ranking.schema.json"


class CausalRankingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.edges = load_edges(ROOT)
        cls.concepts = load_concepts(ROOT)
        with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.schema = json.load(handle)
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assertValidRanking(self, ranking: dict) -> None:
        errors = sorted(self.validator.iter_errors(ranking), key=lambda error: list(error.path))
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_retransmission_ranking_prefers_direct_packet_loss_without_extra_context(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
        )

        self.assertTrue(ranking["found"])
        self.assertValidRanking(ranking)
        sources = [candidate["source"]["id"] for candidate in ranking["candidates"]]
        self.assertEqual(
            [
                "hypothesis.network.packet_loss",
                "hypothesis.network.packet_corruption",
            ],
            sources,
        )
        packet_loss = ranking["candidates"][0]
        self.assertEqual(
            ["observation.network.tcp_retransmissions"],
            packet_loss["factors"]["prediction_matches"]["strong"],
        )

    def test_integrity_error_observation_promotes_packet_corruption(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            observed={"observation.network.tcp_integrity_errors"},
        )

        self.assertValidRanking(ranking)
        first = ranking["candidates"][0]
        self.assertEqual("hypothesis.network.packet_corruption", first["source"]["id"])
        self.assertEqual(
            ["observation.network.tcp_integrity_errors"],
            first["factors"]["matched_path_observations"],
        )

    def test_absent_integrity_error_penalizes_corruption_path(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            absent={"observation.network.tcp_integrity_errors"},
        )

        self.assertValidRanking(ranking)
        self.assertEqual(
            "hypothesis.network.packet_loss",
            ranking["candidates"][0]["source"]["id"],
        )
        corruption = next(
            candidate
            for candidate in ranking["candidates"]
            if candidate["source"]["id"] == "hypothesis.network.packet_corruption"
        )
        self.assertEqual(
            ["observation.network.tcp_integrity_errors"],
            corruption["factors"]["conflicting_observations"],
        )

    def test_direct_packet_loss_observation_strengthens_packet_loss_prediction(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            observed={"observation.network.packet_loss"},
        )

        self.assertValidRanking(ranking)
        packet_loss = ranking["candidates"][0]
        self.assertEqual("hypothesis.network.packet_loss", packet_loss["source"]["id"])
        self.assertEqual(
            [
                "observation.network.packet_loss",
                "observation.network.tcp_retransmissions",
            ],
            packet_loss["factors"]["prediction_matches"]["strong"],
        )

    def test_transport_latency_context_can_rank_longer_corruption_chain_first(self) -> None:
        ranking = rank_causes(
            "observation.network.transport_latency",
            self.edges,
            self.concepts,
            observed={
                "observation.network.tcp_integrity_errors",
                "observation.network.tcp_retransmissions",
            },
        )

        self.assertValidRanking(ranking)
        first = ranking["candidates"][0]
        self.assertEqual("hypothesis.network.packet_corruption", first["source"]["id"])
        self.assertEqual(
            [
                "observation.network.tcp_integrity_errors",
                "observation.network.tcp_retransmissions",
            ],
            first["factors"]["matched_path_observations"],
        )
        self.assertEqual(3, first["path"]["distance"])

    def test_max_depth_limits_candidate_set(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            max_depth=1,
        )

        self.assertValidRanking(ranking)
        self.assertEqual(
            ["hypothesis.network.packet_loss"],
            [candidate["source"]["id"] for candidate in ranking["candidates"]],
        )

    def test_ranking_is_ordinal_and_exposes_no_probability_score(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
        )

        self.assertValidRanking(ranking)
        self.assertEqual("deterministic_ordinal", ranking["ranking_method"]["type"])
        for candidate in ranking["candidates"]:
            self.assertNotIn("score", candidate)
            self.assertNotIn("probability", candidate)


if __name__ == "__main__":
    unittest.main()

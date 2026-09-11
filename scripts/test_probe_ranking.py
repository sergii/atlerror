#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges
from causal_ranking import rank_causes
from probe_ranking import load_ranking, rank_probes

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "probe-ranking.schema.json"


class ProbeRankingTest(unittest.TestCase):
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

    def retransmission_ranking(
        self,
        *,
        observed: set[str] | None = None,
        absent: set[str] | None = None,
    ) -> dict:
        return rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            observed=observed,
            absent=absent,
        )

    def test_integrity_error_probe_is_best_initial_discriminator(self) -> None:
        ranking = self.retransmission_ranking()
        projection = rank_probes(ranking, self.concepts)

        self.assertValidProjection(projection)
        self.assertTrue(projection["found"])
        self.assertIsNone(projection["not_found_reason"])
        self.assertEqual(
            [
                "hypothesis.network.packet_loss",
                "hypothesis.network.packet_corruption",
            ],
            projection["query"]["candidate_hypotheses"],
        )

        first = projection["probes"][0]
        self.assertEqual(1, first["rank"])
        self.assertEqual(
            "probe.network.inspect_tcp_integrity_errors",
            first["probe"]["id"],
        )
        self.assertEqual("read_only", first["risk"])
        self.assertEqual(
            ["observation.network.tcp_integrity_errors"],
            first["factors"]["discriminating_observations"],
        )
        self.assertEqual(
            ["hypothesis.network.packet_corruption"],
            first["factors"]["top_candidate_two_sided_alternatives"],
        )
        self.assertEqual(3, first["factors"]["top_candidate_contrast_components"])

        analysis = first["outcome_analysis"][0]
        effects = {effect["hypothesis"]: effect for effect in analysis["candidate_effects"]}
        packet_loss = effects["hypothesis.network.packet_loss"]
        corruption = effects["hypothesis.network.packet_corruption"]
        self.assertFalse(packet_loss["on_causal_path"])
        self.assertIsNone(packet_loss["prediction_strength"])
        self.assertTrue(corruption["on_causal_path"])
        self.assertEqual("strong", corruption["prediction_strength"])
        self.assertFalse(corruption["absent_is_falsifier_conflict"])
        self.assertTrue(corruption["explicitly_tested_by_probe"])

    def test_resolved_integrity_observation_is_not_recommended_again(self) -> None:
        ranking = self.retransmission_ranking(
            observed={"observation.network.tcp_integrity_errors"}
        )
        projection = rank_probes(ranking, self.concepts)

        self.assertValidProjection(projection)
        self.assertTrue(projection["found"])
        probe_ids = [candidate["probe"]["id"] for candidate in projection["probes"]]
        self.assertNotIn("probe.network.inspect_tcp_integrity_errors", probe_ids)
        self.assertEqual("probe.network.measure_packet_delivery", probe_ids[0])
        for candidate in projection["probes"]:
            self.assertNotIn(
                "observation.network.tcp_integrity_errors",
                candidate["unresolved_observations"],
            )

    def test_probe_ranking_exposes_no_probability_or_opaque_score(self) -> None:
        projection = rank_probes(self.retransmission_ranking(), self.concepts)

        self.assertValidProjection(projection)
        self.assertEqual("deterministic_ordinal", projection["ranking_method"]["type"])
        for candidate in projection["probes"]:
            self.assertNotIn("score", candidate)
            self.assertNotIn("probability", candidate)
            self.assertIn("factors", candidate)
            self.assertGreaterEqual(len(candidate["reasons"]), 2)

    def test_target_observation_is_already_resolved_for_probe_selection(self) -> None:
        projection = rank_probes(self.retransmission_ranking(), self.concepts)

        self.assertValidProjection(projection)
        for candidate in projection["probes"]:
            self.assertNotIn(
                "observation.network.tcp_retransmissions",
                candidate["unresolved_observations"],
            )

    def test_single_causal_candidate_needs_no_discriminating_probe(self) -> None:
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            max_depth=1,
        )
        projection = rank_probes(ranking, self.concepts)

        self.assertValidProjection(projection)
        self.assertFalse(projection["found"])
        self.assertEqual("fewer_than_two_candidates", projection["not_found_reason"])
        self.assertEqual([], projection["probes"])

    def test_no_discriminating_probe_is_explicit_when_all_candidate_signals_are_resolved(self) -> None:
        ranking = self.retransmission_ranking(
            observed={
                "observation.network.tcp_integrity_errors",
                "observation.network.packet_loss",
                "observation.network.packet_corruption",
            }
        )
        projection = rank_probes(ranking, self.concepts)

        self.assertValidProjection(projection)
        self.assertFalse(projection["found"])
        self.assertEqual("no_discriminating_probe", projection["not_found_reason"])
        self.assertEqual([], projection["probes"])

    def test_projection_is_deterministic(self) -> None:
        ranking = self.retransmission_ranking()
        first = rank_probes(ranking, self.concepts)
        second = rank_probes(copy.deepcopy(ranking), dict(reversed(list(self.concepts.items()))))
        self.assertEqual(first, second)

    def test_cli_loader_validates_causal_ranking_json(self) -> None:
        ranking = self.retransmission_ranking()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ranking.json"
            path.write_text(json.dumps(ranking), encoding="utf-8")
            loaded = load_ranking(path)
        self.assertEqual(ranking, loaded)


if __name__ == "__main__":
    unittest.main()

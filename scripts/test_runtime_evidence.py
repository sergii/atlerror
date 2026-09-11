#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges
from causal_ranking import rank_causes
from runtime_evidence import load_runtime_evidence, resolve_runtime_evidence, validate_runtime_references

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
RANKING_SCHEMA_PATH = ROOT / "schema" / "causal-ranking.schema.json"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)


class RuntimeEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.edges = load_edges(ROOT)
        cls.concepts = load_concepts(ROOT)
        with RANKING_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.ranking_schema = json.load(handle)
        Draft202012Validator.check_schema(cls.ranking_schema)
        cls.ranking_validator = Draft202012Validator(cls.ranking_schema)

    def load_example(self) -> dict:
        return load_runtime_evidence(EXAMPLE_PATH)

    def assert_valid_ranking(self, ranking: dict) -> None:
        errors = sorted(
            self.ranking_validator.iter_errors(ranking),
            key=lambda error: list(error.path),
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_resolves_active_stale_and_absent_state(self) -> None:
        document = self.load_example()
        observed, absent, context = resolve_runtime_evidence(
            document,
            self.concepts,
            as_of=AS_OF,
            source_path=str(EXAMPLE_PATH.relative_to(ROOT)),
        )

        self.assertEqual(
            {
                "observation.network.tcp_integrity_errors",
                "observation.network.tcp_retransmissions",
            },
            observed,
        )
        self.assertEqual(set(), absent)
        self.assertEqual(
            ["evidence.network.packet_loss.stale"],
            context["stale_instance_ids"],
        )
        self.assertEqual([], context["future_instance_ids"])
        self.assertEqual(2, len(context["active_instances"]))
        self.assertEqual("incident.network.retransmission_spike", context["incident_id"])

    def test_runtime_evidence_promotes_corruption_candidate(self) -> None:
        document = self.load_example()
        observed, absent, context = resolve_runtime_evidence(
            document,
            self.concepts,
            as_of=AS_OF,
            source_path=str(EXAMPLE_PATH.relative_to(ROOT)),
        )
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            observed=observed,
            absent=absent,
            evidence_context=context,
        )

        self.assert_valid_ranking(ranking)
        self.assertEqual(
            "hypothesis.network.packet_corruption",
            ranking["candidates"][0]["source"]["id"],
        )
        self.assertEqual(
            "incident.network.retransmission_spike",
            ranking["evidence_context"]["incident_id"],
        )
        self.assertIn(
            "observation.network.tcp_integrity_errors",
            ranking["candidates"][0]["factors"]["matched_path_observations"],
        )

    def test_conflicting_active_states_are_rejected(self) -> None:
        document = self.load_example()
        conflicting = copy.deepcopy(document["instances"][1])
        conflicting["id"] = "evidence.network.tcp_integrity_errors.conflicting"
        conflicting["state"] = "absent"
        document["instances"].append(conflicting)

        with self.assertRaisesRegex(ValueError, "contradictory states"):
            resolve_runtime_evidence(
                document,
                self.concepts,
                as_of=AS_OF,
            )

    def test_expiry_must_follow_observation_time(self) -> None:
        document = self.load_example()
        document["instances"][0]["expires_at"] = document["instances"][0]["observed_at"]

        with self.assertRaisesRegex(ValueError, "expires_at must be later"):
            validate_runtime_references(document, self.concepts)

    def test_future_evidence_is_not_used_yet(self) -> None:
        document = self.load_example()
        future = copy.deepcopy(document["instances"][0])
        future["id"] = "evidence.network.packet_corruption.future"
        future["observation"] = "observation.network.packet_corruption"
        future["observed_at"] = "2026-09-11T15:10:00Z"
        future["expires_at"] = "2026-09-11T15:20:00Z"
        document["instances"].append(future)

        observed, _, context = resolve_runtime_evidence(
            document,
            self.concepts,
            as_of=AS_OF,
        )

        self.assertNotIn("observation.network.packet_corruption", observed)
        self.assertEqual(
            ["evidence.network.packet_corruption.future"],
            context["future_instance_ids"],
        )


if __name__ == "__main__":
    unittest.main()

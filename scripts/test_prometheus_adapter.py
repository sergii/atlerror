#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts, load_edges
from causal_ranking import rank_causes
from prometheus_adapter import (
    build_runtime_evidence,
    load_adapter,
    parse_prometheus_response,
    validate_adapter_references,
)
from runtime_evidence import resolve_runtime_evidence

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "examples" / "adapters" / "prometheus" / "network-tcp.yaml"
RESPONSE_DIR = ROOT / "examples" / "telemetry" / "prometheus"
RUNTIME_SCHEMA_PATH = ROOT / "schema" / "runtime-evidence.schema.json"
SAMPLE_TIME = datetime.fromtimestamp(1789141500, tz=timezone.utc)


class PrometheusAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.adapter = load_adapter(ADAPTER_PATH)
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with RUNTIME_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.runtime_schema = json.load(handle)
        Draft202012Validator.check_schema(cls.runtime_schema)
        cls.runtime_validator = Draft202012Validator(cls.runtime_schema)

    def load_responses(self) -> dict[str, dict]:
        responses: dict[str, dict] = {}
        for mapping in self.adapter["mappings"]:
            path = RESPONSE_DIR / f"{mapping['id']}.json"
            with path.open("r", encoding="utf-8") as handle:
                responses[mapping["id"]] = json.load(handle)
        return responses

    def build_document(self, responses: dict[str, dict] | None = None) -> dict:
        return build_runtime_evidence(
            self.adapter,
            self.load_responses() if responses is None else responses,
            self.concepts,
            incident_id="incident.prometheus.network_tcp",
            source_uri="http://prometheus:9090",
        )

    def assert_valid_runtime_document(self, document: dict) -> None:
        errors = sorted(
            self.runtime_validator.iter_errors(document),
            key=lambda error: list(error.path),
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_adapter_references_are_valid(self) -> None:
        validate_adapter_references(self.adapter, self.concepts)

    def test_prometheus_vectors_become_runtime_evidence(self) -> None:
        document = self.build_document()
        self.assert_valid_runtime_document(document)
        self.assertEqual(2, len(document["instances"]))

        by_observation = {instance["observation"]: instance for instance in document["instances"]}
        retransmissions = by_observation["observation.network.tcp_retransmissions"]
        integrity_errors = by_observation["observation.network.tcp_integrity_errors"]

        self.assertEqual("observed", retransmissions["state"])
        self.assertEqual("observed", integrity_errors["state"])
        self.assertEqual(
            ["boundary.application.external_dependency"],
            retransmissions["scope"]["boundaries"],
        )
        self.assertEqual(
            ["system_entity.application_service"],
            retransmissions["scope"]["entities"],
        )
        self.assertEqual("api", retransmissions["scope"]["attributes"]["service"])
        self.assertEqual("app-1:9100", retransmissions["scope"]["attributes"]["instance"])
        self.assertEqual("12.5", retransmissions["source"]["attributes"]["label.__name__"] if False else "12.5")
        self.assertEqual(12.5, retransmissions["measurement"]["value"])
        self.assertEqual(5.0, retransmissions["measurement"]["baseline"])
        self.assertEqual(7.5, retransmissions["measurement"]["delta"])
        self.assertEqual("above_baseline", retransmissions["measurement"]["comparison"])

    def test_below_threshold_maps_to_absent(self) -> None:
        responses = self.load_responses()
        responses["tcp_retransmissions_rate"]["data"]["result"][0]["value"][1] = "1.0"
        document = self.build_document(responses)
        retransmissions = next(
            instance
            for instance in document["instances"]
            if instance["observation"] == "observation.network.tcp_retransmissions"
        )
        self.assertEqual("absent", retransmissions["state"])
        self.assertEqual("below_baseline", retransmissions["measurement"]["comparison"])

    def test_dynamic_scope_label_must_reference_known_boundary(self) -> None:
        responses = self.load_responses()
        responses["tcp_retransmissions_rate"]["data"]["result"][0]["metric"][
            "atlerror_boundary"
        ] = "boundary.unknown.path"
        with self.assertRaisesRegex(ValueError, "unknown boundary"):
            self.build_document(responses)

    def test_instance_ids_are_stable_across_label_order(self) -> None:
        responses_a = self.load_responses()
        responses_b = copy.deepcopy(responses_a)
        metric = responses_b["tcp_retransmissions_rate"]["data"]["result"][0]["metric"]
        responses_b["tcp_retransmissions_rate"]["data"]["result"][0]["metric"] = dict(
            reversed(list(metric.items()))
        )
        document_a = self.build_document(responses_a)
        document_b = self.build_document(responses_b)
        ids_a = sorted(instance["id"] for instance in document_a["instances"])
        ids_b = sorted(instance["id"] for instance in document_b["instances"])
        self.assertEqual(ids_a, ids_b)

    def test_generated_evidence_drives_scope_aware_causal_ranking(self) -> None:
        document = self.build_document()
        observed, absent, context = resolve_runtime_evidence(
            document,
            self.concepts,
            as_of=SAMPLE_TIME + timedelta(seconds=60),
            scope_query={"boundaries": ["boundary.application.external_dependency"]},
        )
        ranking = rank_causes(
            "observation.network.tcp_retransmissions",
            self.edges,
            self.concepts,
            observed=observed,
            absent=absent,
            evidence_context=context,
        )
        self.assertEqual(
            "hypothesis.network.packet_corruption",
            ranking["candidates"][0]["source"]["id"],
        )
        self.assertIn(
            "observation.network.tcp_integrity_errors",
            ranking["candidates"][0]["factors"]["matched_path_observations"],
        )

    def test_non_finite_prometheus_values_are_rejected(self) -> None:
        response = self.load_responses()["tcp_retransmissions_rate"]
        response["data"]["result"][0]["value"][1] = "NaN"
        with self.assertRaisesRegex(ValueError, "must be finite"):
            parse_prometheus_response(response, "tcp_retransmissions_rate")


if __name__ == "__main__":
    unittest.main()

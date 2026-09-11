#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from causal_projection import load_concepts
from opentelemetry_trace_adapter import (
    build_runtime_evidence,
    load_adapter,
    load_payload,
)
from runtime_evidence import resolve_runtime_evidence

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "examples" / "adapters" / "opentelemetry" / "external-dependency.yaml"
PAYLOAD_PATH = ROOT / "examples" / "telemetry" / "opentelemetry" / "external-dependency-trace.json"
RUNTIME_SCHEMA_PATH = ROOT / "schema" / "runtime-evidence.schema.json"
AS_OF = datetime(2026, 9, 11, 16, 31, tzinfo=timezone.utc)


class OpenTelemetryTraceAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        with RUNTIME_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            cls.runtime_schema = json.load(handle)
        Draft202012Validator.check_schema(cls.runtime_schema)
        cls.runtime_validator = Draft202012Validator(cls.runtime_schema)

    def build(self) -> dict:
        return build_runtime_evidence(
            load_adapter(ADAPTER_PATH),
            load_payload(PAYLOAD_PATH),
            self.concepts,
            incident_id="incident.checkout.stripe_timeout",
            source_uri="file://external-dependency-trace.json",
        )

    def test_translates_otlp_span_into_runtime_evidence(self) -> None:
        evidence = self.build()
        errors = sorted(
            self.runtime_validator.iter_errors(evidence),
            key=lambda error: list(error.path),
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))
        self.assertEqual(2, len(evidence["instances"]))

        by_observation = {instance["observation"]: instance for instance in evidence["instances"]}
        latency = by_observation["observation.dependency.latency"]
        timeout = by_observation["observation.network.connection_timeout"]

        self.assertEqual("observed", latency["state"])
        self.assertEqual(250.0, latency["measurement"]["value"])
        self.assertEqual(100.0, latency["measurement"]["baseline"])
        self.assertEqual("trace", latency["source"]["type"])
        self.assertEqual("stripe", latency["scope"]["attributes"]["dependency"])
        self.assertEqual(
            ["boundary.application.external_dependency"],
            latency["scope"]["boundaries"],
        )
        self.assertEqual("observed", timeout["state"])
        self.assertEqual("present", timeout["measurement"]["comparison"])

    def test_scope_resolution_uses_trace_topology(self) -> None:
        evidence = self.build()
        observed, absent, context = resolve_runtime_evidence(
            evidence,
            self.concepts,
            as_of=AS_OF,
            scope_query={
                "boundaries": ["boundary.application.external_dependency"],
                "attributes": {"dependency": "stripe"},
            },
        )
        self.assertEqual(
            {
                "observation.dependency.latency",
                "observation.network.connection_timeout",
            },
            observed,
        )
        self.assertEqual(set(), absent)
        self.assertEqual(2, len(context["active_instances"]))
        self.assertEqual([], context["scope_filtered_instance_ids"])

    def test_other_dependency_scope_excludes_trace_evidence(self) -> None:
        evidence = self.build()
        observed, absent, context = resolve_runtime_evidence(
            evidence,
            self.concepts,
            as_of=AS_OF,
            scope_query={
                "boundaries": ["boundary.application.external_dependency"],
                "attributes": {"dependency": "github"},
            },
        )
        self.assertEqual(set(), observed)
        self.assertEqual(set(), absent)
        self.assertEqual(2, len(context["scope_filtered_instance_ids"]))

    def test_instance_ids_are_stable_for_same_trace_span(self) -> None:
        first = self.build()
        second = self.build()
        self.assertEqual(
            [instance["id"] for instance in first["instances"]],
            [instance["id"] for instance in second["instances"]],
        )

    def test_unknown_semantic_boundary_from_span_fails(self) -> None:
        payload = load_payload(PAYLOAD_PATH)
        mutated = copy.deepcopy(payload)
        attributes = mutated["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        for attribute in attributes:
            if attribute["key"] == "atlerror.boundary":
                attribute["value"]["stringValue"] = "boundary.unknown.path"
        with self.assertRaisesRegex(ValueError, "unknown boundary"):
            build_runtime_evidence(
                load_adapter(ADAPTER_PATH),
                mutated,
                self.concepts,
                incident_id="incident.invalid",
            )

    def test_payload_with_no_matching_span_fails(self) -> None:
        payload = load_payload(PAYLOAD_PATH)
        mutated = copy.deepcopy(payload)
        span = mutated["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        span["kind"] = "SPAN_KIND_SERVER"
        with self.assertRaisesRegex(ValueError, "matched no adapter mappings"):
            build_runtime_evidence(
                load_adapter(ADAPTER_PATH),
                mutated,
                self.concepts,
                incident_id="incident.no_match",
            )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from causal_projection import ROOT, concept_view, load_concepts

SCHEMA_PATH = ROOT / "schema" / "probe-ranking.schema.json"
CAUSAL_RANKING_SCHEMA_PATH = ROOT / "schema" / "causal-ranking.schema.json"

STRENGTH_ORDER = {None: -1, "weak": 0, "moderate": 1, "strong": 2}
RISK_ORDER = {"read_only": 0, "low": 1, "state_changing": 2, "high": 3}
RANKING_PRIORITY = [
    "more_top_candidate_two_sided_alternatives",
    "more_top_candidate_contrast_components",
    "more_top_candidate_discriminated_alternatives",
    "more_two_sided_candidate_pairs",
    "more_contrast_components",
    "more_discriminated_candidate_pairs",
    "lower_probe_risk",
    "more_hypotheses_explicitly_tested",
    "probe_id",
]


def _pair(left: str, right: str) -> list[str]:
    return [left, right]


def _pair_key(left: str, right: str, order: dict[str, int]) -> tuple[str, str]:
    if order[left] <= order[right]:
        return left, right
    return right, left


def _prediction(hypothesis: dict[str, Any], observation: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for item in hypothesis.get("predictions", []):
        if not isinstance(item, dict) or item.get("observation") != observation:
            continue
        strength = item.get("strength", "weak")
        if strength not in {"weak", "moderate", "strong"}:
            strength = "weak"
        candidate = {
            "strength": strength,
            "expected": item.get("expected") if isinstance(item.get("expected"), str) else None,
        }
        if best is None or STRENGTH_ORDER[candidate["strength"]] > STRENGTH_ORDER[best["strength"]]:
            best = candidate
    return best


def _falsifier(hypothesis: dict[str, Any], observation: str) -> dict[str, Any] | None:
    for item in hypothesis.get("falsifiers", []):
        if not isinstance(item, dict) or item.get("observation") != observation:
            continue
        return {
            "condition": item.get("condition") if isinstance(item.get("condition"), str) else None,
            "effect": item.get("effect") if isinstance(item.get("effect"), str) else None,
        }
    return None


def _path_observation_ids(candidate: dict[str, Any]) -> set[str]:
    path = candidate.get("path", {})
    nodes = path.get("nodes", []) if isinstance(path, dict) else []
    if not isinstance(nodes, list) or len(nodes) < 3:
        return set()
    output: set[str] = set()
    for node in nodes[1:-1]:
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            output.add(node["id"])
    return output


def _candidate_effect(
    candidate: dict[str, Any],
    hypothesis: dict[str, Any],
    probe_id: str,
    observation: str,
) -> dict[str, Any]:
    prediction = _prediction(hypothesis, observation)
    falsifier = _falsifier(hypothesis, observation)
    tested_by = hypothesis.get("tested_by", [])
    return {
        "hypothesis": candidate["source"]["id"],
        "current_rank": candidate["rank"],
        "on_causal_path": observation in _path_observation_ids(candidate),
        "prediction_strength": prediction["strength"] if prediction is not None else None,
        "prediction_expected": prediction["expected"] if prediction is not None else None,
        "absent_is_falsifier_conflict": falsifier is not None,
        "falsifier_condition": falsifier["condition"] if falsifier is not None else None,
        "explicitly_tested_by_probe": probe_id in tested_by if isinstance(tested_by, list) else False,
    }


def _observed_signature(effect: dict[str, Any]) -> tuple[int, int]:
    return (
        1 if effect["on_causal_path"] else 0,
        STRENGTH_ORDER[effect["prediction_strength"]],
    )


def _absent_signature(effect: dict[str, Any]) -> tuple[int, int]:
    return (
        1 if effect["on_causal_path"] else 0,
        1 if effect["absent_is_falsifier_conflict"] else 0,
    )


def _contrast_components(left: dict[str, Any], right: dict[str, Any]) -> tuple[int, int]:
    observed = 0
    observed += int(left["on_causal_path"] != right["on_causal_path"])
    observed += int(
        STRENGTH_ORDER[left["prediction_strength"]]
        != STRENGTH_ORDER[right["prediction_strength"]]
    )

    absent = 0
    absent += int(left["on_causal_path"] != right["on_causal_path"])
    absent += int(
        left["absent_is_falsifier_conflict"]
        != right["absent_is_falsifier_conflict"]
    )
    return observed, absent


def _analyze_observation(
    observation: str,
    probe_id: str,
    candidates: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    effects: list[dict[str, Any]] = []
    for candidate in candidates:
        hypothesis_id = candidate["source"]["id"]
        effects.append(
            _candidate_effect(
                candidate,
                concepts[hypothesis_id],
                probe_id,
                observation,
            )
        )

    order = {candidate["source"]["id"]: index for index, candidate in enumerate(candidates)}
    observed_pairs: set[tuple[str, str]] = set()
    absent_pairs: set[tuple[str, str]] = set()
    two_sided_pairs: set[tuple[str, str]] = set()
    pair_components: dict[tuple[str, str], int] = {}

    for left_index, left in enumerate(effects):
        for right in effects[left_index + 1 :]:
            key = _pair_key(left["hypothesis"], right["hypothesis"], order)
            observed_components, absent_components = _contrast_components(left, right)
            if _observed_signature(left) != _observed_signature(right):
                observed_pairs.add(key)
            if _absent_signature(left) != _absent_signature(right):
                absent_pairs.add(key)
            if key in observed_pairs and key in absent_pairs:
                two_sided_pairs.add(key)
            pair_components[key] = observed_components + absent_components

    all_pairs = observed_pairs | absent_pairs
    return {
        "observation": observation,
        "candidate_effects": effects,
        "observed_distinguishes_pairs": [_pair(*pair) for pair in sorted(observed_pairs, key=lambda p: (order[p[0]], order[p[1]]))],
        "absent_distinguishes_pairs": [_pair(*pair) for pair in sorted(absent_pairs, key=lambda p: (order[p[0]], order[p[1]]))],
        "two_sided_distinguishes_pairs": [_pair(*pair) for pair in sorted(two_sided_pairs, key=lambda p: (order[p[0]], order[p[1]]))],
        "contrast_components": sum(pair_components[pair] for pair in all_pairs),
    }


def _tested_hypotheses(
    probe_id: str,
    candidates: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
) -> list[str]:
    output: list[str] = []
    for candidate in candidates:
        hypothesis_id = candidate["source"]["id"]
        tested_by = concepts[hypothesis_id].get("tested_by", [])
        if isinstance(tested_by, list) and probe_id in tested_by:
            output.append(hypothesis_id)
    return output


def _probe_reasons(candidate: dict[str, Any], top_candidate: str) -> list[str]:
    factors = candidate["factors"]
    reasons: list[str] = []
    top_two_sided = factors["top_candidate_two_sided_alternatives"]
    top_any = factors["top_candidate_discriminated_alternatives"]
    if top_two_sided:
        reasons.append(
            "Either observed or absent results change ranking factors differently for the current top candidate versus: "
            + ", ".join(top_two_sided)
            + "."
        )
    elif top_any:
        reasons.append(
            "At least one outcome changes ranking factors differently for the current top candidate versus: "
            + ", ".join(top_any)
            + "."
        )

    reasons.append(
        "Diagnostic contrast uses only existing causal-path matches, prediction strengths, and explicit falsifiers."
    )
    reasons.append(
        f"Probe risk is {candidate['risk']}; capability availability is not inferred by this projection."
    )
    if candidate["hypotheses_tested"]:
        reasons.append(
            "Catalog hypotheses that explicitly list this probe: "
            + ", ".join(candidate["hypotheses_tested"])
            + "."
        )
    if top_candidate not in candidate["hypotheses_tested"]:
        reasons.append(
            "The probe can still discriminate the top candidate through an alternative candidate's prediction, causal path, or falsifier."
        )
    return reasons


def _probe_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    factors = candidate["factors"]
    return (
        -len(factors["top_candidate_two_sided_alternatives"]),
        -factors["top_candidate_contrast_components"],
        -len(factors["top_candidate_discriminated_alternatives"]),
        -len(factors["two_sided_candidate_pairs"]),
        -factors["contrast_components"],
        -len(factors["discriminated_candidate_pairs"]),
        RISK_ORDER[candidate["risk"]],
        -len(candidate["hypotheses_tested"]),
        candidate["probe"]["id"],
    )


def validate_causal_ranking(ranking: dict[str, Any], *, schema_path: Path = CAUSAL_RANKING_SCHEMA_PATH) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(ranking), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            "causal ranking schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def validate_probe_ranking(projection: dict[str, Any], *, schema_path: Path = SCHEMA_PATH) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(projection), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            "probe ranking schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def validate_references(
    ranking: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> None:
    seen: set[str] = set()
    for candidate in ranking.get("candidates", []):
        hypothesis_id = candidate.get("source", {}).get("id")
        hypothesis = concepts.get(hypothesis_id)
        if not isinstance(hypothesis, dict) or hypothesis.get("kind") != "hypothesis":
            raise ValueError(f"ranking candidate is not a known hypothesis: {hypothesis_id}")
        if hypothesis_id in seen:
            raise ValueError(f"ranking contains duplicate hypothesis candidate: {hypothesis_id}")
        seen.add(hypothesis_id)
        for probe_id in hypothesis.get("tested_by", []):
            probe = concepts.get(probe_id)
            if not isinstance(probe, dict) or probe.get("kind") != "probe":
                raise ValueError(f"hypothesis {hypothesis_id} references unknown probe: {probe_id}")
            for observation in probe.get("produces", []):
                concept = concepts.get(observation)
                if not isinstance(concept, dict) or concept.get("kind") != "observation":
                    raise ValueError(f"probe {probe_id} produces unknown observation: {observation}")


def rank_probes(
    ranking: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_references(ranking, concepts)

    query = ranking["query"]
    candidates = sorted(ranking.get("candidates", []), key=lambda candidate: candidate["rank"])
    candidate_ids = [candidate["source"]["id"] for candidate in candidates]
    resolved = set(query.get("observed", [])) | set(query.get("absent", [])) | {query["target"]}

    projection: dict[str, Any] = {
        "schema_version": "0.1",
        "kind": "probe_ranking",
        "query": {
            "target": query["target"],
            "observed": sorted(query.get("observed", [])),
            "absent": sorted(query.get("absent", [])),
            "candidate_hypotheses": candidate_ids,
        },
        "found": False,
        "not_found_reason": None,
        "ranking_method": {
            "type": "deterministic_ordinal",
            "purpose": "discriminate_current_causal_candidates",
            "priority": RANKING_PRIORITY,
        },
        "probes": [],
    }

    if len(candidates) < 2:
        projection["not_found_reason"] = "fewer_than_two_candidates"
        validate_probe_ranking(projection)
        return projection

    top_candidate = candidate_ids[0]
    order = {hypothesis_id: index for index, hypothesis_id in enumerate(candidate_ids)}
    probe_ids: set[str] = set()
    for hypothesis_id in candidate_ids:
        tested_by = concepts[hypothesis_id].get("tested_by", [])
        if isinstance(tested_by, list):
            probe_ids.update(probe_id for probe_id in tested_by if isinstance(probe_id, str))

    probe_candidates: list[dict[str, Any]] = []
    for probe_id in sorted(probe_ids):
        probe = concepts[probe_id]
        unresolved = sorted(
            observation
            for observation in probe.get("produces", [])
            if isinstance(observation, str) and observation not in resolved
        )
        if not unresolved:
            continue

        analyses = [
            _analyze_observation(observation, probe_id, candidates, concepts)
            for observation in unresolved
        ]
        analyses = [
            analysis
            for analysis in analyses
            if analysis["observed_distinguishes_pairs"] or analysis["absent_distinguishes_pairs"]
        ]
        if not analyses:
            continue

        discriminated_pairs: set[tuple[str, str]] = set()
        two_sided_pairs: set[tuple[str, str]] = set()
        top_discriminated: set[str] = set()
        top_two_sided: set[str] = set()
        total_components = 0
        top_components = 0

        for analysis in analyses:
            observed_pairs = {tuple(pair) for pair in analysis["observed_distinguishes_pairs"]}
            absent_pairs = {tuple(pair) for pair in analysis["absent_distinguishes_pairs"]}
            current_pairs = observed_pairs | absent_pairs
            current_two_sided = observed_pairs & absent_pairs
            discriminated_pairs.update(current_pairs)
            two_sided_pairs.update(current_two_sided)
            total_components += analysis["contrast_components"]

            effect_by_id = {
                effect["hypothesis"]: effect for effect in analysis["candidate_effects"]
            }
            for alternative in candidate_ids[1:]:
                pair_key = _pair_key(top_candidate, alternative, order)
                if pair_key in current_pairs:
                    top_discriminated.add(alternative)
                if pair_key in current_two_sided:
                    top_two_sided.add(alternative)
                observed_components, absent_components = _contrast_components(
                    effect_by_id[top_candidate], effect_by_id[alternative]
                )
                top_components += observed_components + absent_components

        hypotheses_tested = _tested_hypotheses(probe_id, candidates, concepts)
        risk = probe.get("risk", "high")
        if risk not in RISK_ORDER:
            risk = "high"
        candidate = {
            "rank": 0,
            "probe": concept_view(probe_id, concepts),
            "risk": risk,
            "requires": sorted(
                item for item in probe.get("requires", []) if isinstance(item, str)
            ),
            "preferred_tools": sorted(
                item for item in probe.get("preferred_tools", []) if isinstance(item, str)
            ),
            "unresolved_observations": unresolved,
            "hypotheses_tested": hypotheses_tested,
            "outcome_analysis": analyses,
            "factors": {
                "top_candidate": top_candidate,
                "top_candidate_two_sided_alternatives": sorted(top_two_sided, key=order.get),
                "top_candidate_discriminated_alternatives": sorted(top_discriminated, key=order.get),
                "top_candidate_contrast_components": top_components,
                "two_sided_candidate_pairs": [
                    _pair(*pair)
                    for pair in sorted(two_sided_pairs, key=lambda pair: (order[pair[0]], order[pair[1]]))
                ],
                "discriminated_candidate_pairs": [
                    _pair(*pair)
                    for pair in sorted(discriminated_pairs, key=lambda pair: (order[pair[0]], order[pair[1]]))
                ],
                "contrast_components": total_components,
                "discriminating_observations": [analysis["observation"] for analysis in analyses],
            },
            "reasons": [],
        }
        candidate["reasons"] = _probe_reasons(candidate, top_candidate)
        probe_candidates.append(candidate)

    probe_candidates.sort(key=_probe_sort_key)
    for rank, candidate in enumerate(probe_candidates, start=1):
        candidate["rank"] = rank

    projection["found"] = bool(probe_candidates)
    projection["not_found_reason"] = None if probe_candidates else "no_discriminating_probe"
    projection["probes"] = probe_candidates
    validate_probe_ranking(projection)
    return projection


def load_ranking(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ranking file must contain valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("ranking file must contain a JSON object")
    validate_causal_ranking(document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rank existing diagnostic probes by how transparently their unresolved observations "
            "can discriminate between current causal candidates."
        )
    )
    parser.add_argument("ranking", type=Path, help="Causal ranking JSON produced by causal_ranking.py")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        ranking = load_ranking(args.ranking)
        concepts = load_concepts(root)
        projection = rank_probes(ranking, concepts)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    indent = 2 if args.pretty else None
    print(json.dumps(projection, indent=indent, sort_keys=True))
    return 0 if projection["found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

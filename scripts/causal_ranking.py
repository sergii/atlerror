#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from causal_projection import (
    ROOT,
    concept_view,
    load_concepts,
    load_edges,
    path_view,
    reverse_shortest_paths,
)

STRENGTH_ORDER = {"weak": 0, "moderate": 1, "strong": 2}
PROVENANCE_ORDER = {"none": 0, "claim": 1, "experiment": 2}
RANKING_PRIORITY = [
    "fewer_conflicts",
    "more_causal_path_matches",
    "more_strong_prediction_matches",
    "more_moderate_prediction_matches",
    "more_weak_prediction_matches",
    "stronger_weakest_causal_edge",
    "stronger_weakest_provenance",
    "shorter_causal_path",
    "fewer_contributes_to_edges",
    "source_id",
]


def edge_provenance(edge: dict[str, Any]) -> str:
    evidence = edge.get("evidence")
    if not isinstance(evidence, dict):
        return "none"
    experiments = evidence.get("experiments")
    if isinstance(experiments, list) and experiments:
        return "experiment"
    claims = evidence.get("claims")
    if isinstance(claims, list) and claims:
        return "claim"
    return "none"


def prediction_matches(
    hypothesis: dict[str, Any],
    observed: set[str],
) -> dict[str, list[str]]:
    matches: dict[str, set[str]] = {"strong": set(), "moderate": set(), "weak": set()}
    for prediction in hypothesis.get("predictions", []):
        if not isinstance(prediction, dict):
            continue
        observation = prediction.get("observation")
        if observation not in observed:
            continue
        strength = prediction.get("strength", "weak")
        if strength not in matches:
            strength = "weak"
        matches[strength].add(observation)
    return {strength: sorted(values) for strength, values in matches.items()}


def falsifier_conflicts(
    hypothesis: dict[str, Any],
    absent: set[str],
) -> list[str]:
    conflicts: set[str] = set()
    for falsifier in hypothesis.get("falsifiers", []):
        if not isinstance(falsifier, dict):
            continue
        observation = falsifier.get("observation")
        if observation in absent:
            conflicts.add(observation)
    return sorted(conflicts)


def candidate_factors(
    nodes: list[str],
    path_edges: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
    observed: set[str],
    absent: set[str],
) -> dict[str, Any]:
    source = concepts[nodes[0]]
    intermediate = set(nodes[1:-1])
    matched_path = sorted(intermediate & observed)
    path_conflicts = sorted(intermediate & absent)
    hypothesis_falsifier_conflicts = falsifier_conflicts(source, absent)
    conflicts = sorted(set(path_conflicts) | set(hypothesis_falsifier_conflicts))

    effective_observed = set(observed)
    effective_observed.add(nodes[-1])
    matches = prediction_matches(source, effective_observed)

    strengths = [edge["strength"] for edge in path_edges]
    weakest_strength = min(strengths, key=lambda value: STRENGTH_ORDER[value])
    provenances = [edge_provenance(edge) for edge in path_edges]
    weakest_provenance = min(provenances, key=lambda value: PROVENANCE_ORDER[value])

    return {
        "matched_path_observations": matched_path,
        "path_conflicts": path_conflicts,
        "falsifier_conflicts": hypothesis_falsifier_conflicts,
        "conflicting_observations": conflicts,
        "prediction_matches": matches,
        "weakest_edge_strength": weakest_strength,
        "weakest_provenance": weakest_provenance,
        "experiment_backed_edges": sum(
            1 for provenance in provenances if provenance == "experiment"
        ),
        "claim_backed_edges": sum(
            1
            for edge in path_edges
            if isinstance(edge.get("evidence"), dict)
            and isinstance(edge["evidence"].get("claims"), list)
            and bool(edge["evidence"]["claims"])
        ),
        "contributes_to_edges": sum(
            1 for edge in path_edges if edge["relation"] == "contributes_to"
        ),
    }


def candidate_reasons(path: dict[str, Any], factors: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    conflicts = factors["conflicting_observations"]
    if conflicts:
        reasons.append("Conflicting observations: " + ", ".join(conflicts) + ".")
    else:
        reasons.append("No supplied observations conflict with this causal path or an explicit falsifier.")

    matched_path = factors["matched_path_observations"]
    if matched_path:
        reasons.append("Observed causal path nodes: " + ", ".join(matched_path) + ".")

    matches = factors["prediction_matches"]
    if any(matches[strength] for strength in ("strong", "moderate", "weak")):
        reasons.append(
            "Matched hypothesis predictions: "
            f"strong={len(matches['strong'])}, "
            f"moderate={len(matches['moderate'])}, "
            f"weak={len(matches['weak'])}."
        )

    reasons.append(
        "Path basis: "
        f"weakest edge={factors['weakest_edge_strength']}, "
        f"weakest provenance={factors['weakest_provenance']}, "
        f"distance={path['distance']}."
    )
    return reasons


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    factors = candidate["factors"]
    matches = factors["prediction_matches"]
    return (
        len(factors["conflicting_observations"]),
        -len(factors["matched_path_observations"]),
        -len(matches["strong"]),
        -len(matches["moderate"]),
        -len(matches["weak"]),
        -STRENGTH_ORDER[factors["weakest_edge_strength"]],
        -PROVENANCE_ORDER[factors["weakest_provenance"]],
        candidate["path"]["distance"],
        factors["contributes_to_edges"],
        candidate["source"]["id"],
    )


def rank_causes(
    target: str,
    edges: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
    observed: set[str] | None = None,
    absent: set[str] | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    observed = set() if observed is None else set(observed)
    absent = set() if absent is None else set(absent)

    candidates: list[dict[str, Any]] = []
    for nodes, path_edges in reverse_shortest_paths(target, edges, max_depth=max_depth):
        source = concepts.get(nodes[0], {})
        if source.get("kind") != "hypothesis":
            continue
        projected_path = path_view(nodes, path_edges, concepts)
        factors = candidate_factors(nodes, path_edges, concepts, observed, absent)
        candidates.append(
            {
                "rank": 0,
                "source": concept_view(nodes[0], concepts),
                "path": projected_path,
                "factors": factors,
                "reasons": candidate_reasons(projected_path, factors),
            }
        )

    candidates.sort(key=candidate_sort_key)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    return {
        "schema_version": "0.1",
        "kind": "causal_ranking",
        "query": {
            "target": target,
            "observed": sorted(observed),
            "absent": sorted(absent),
            "max_depth": max_depth,
        },
        "found": bool(candidates),
        "ranking_method": {
            "type": "deterministic_ordinal",
            "target_is_observed": True,
            "priority": RANKING_PRIORITY,
        },
        "candidates": candidates,
    }


def validate_query(
    parser: argparse.ArgumentParser,
    target: str,
    observed: set[str],
    absent: set[str],
    concepts: dict[str, dict[str, Any]],
) -> None:
    target_concept = concepts.get(target)
    if target_concept is None:
        parser.error(f"unknown target concept: {target}")
    if target_concept.get("kind") != "observation":
        parser.error("target must be an observation concept")

    for label, values in (("--observed", observed), ("--absent", absent)):
        for concept_id in sorted(values):
            concept = concepts.get(concept_id)
            if concept is None:
                parser.error(f"{label} references unknown concept: {concept_id}")
            if concept.get("kind") != "observation":
                parser.error(f"{label} requires observation concepts: {concept_id}")

    overlap = sorted(observed & absent)
    if overlap:
        parser.error("the same observation cannot be both observed and absent: " + ", ".join(overlap))
    if target in absent:
        parser.error("the target observation cannot be marked absent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank upstream causal hypotheses using transparent ordinal evidence factors."
    )
    parser.add_argument("target", help="Observed target concept to explain")
    parser.add_argument(
        "--observed",
        action="append",
        default=[],
        metavar="OBSERVATION_ID",
        help="Additional observation currently present; may be repeated",
    )
    parser.add_argument(
        "--absent",
        action="append",
        default=[],
        metavar="OBSERVATION_ID",
        help="Observation known to be absent or normal; may be repeated",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum number of upstream causal edges to traverse",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be at least 1")

    edges = load_edges(root)
    concepts = load_concepts(root)
    observed = set(args.observed)
    absent = set(args.absent)
    validate_query(parser, args.target, observed, absent, concepts)

    projection = rank_causes(
        args.target,
        edges,
        concepts,
        observed=observed,
        absent=absent,
        max_depth=args.max_depth,
    )
    indent = 2 if args.pretty else None
    print(json.dumps(projection, indent=indent, sort_keys=True))
    return 0 if projection["found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

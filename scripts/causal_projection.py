#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CAUSAL_ROOT = ROOT / "causal"
KNOWLEDGE_ROOT = ROOT / "knowledge"


def load_edges(root: Path = ROOT) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    causal_root = root / "causal"
    for path in sorted(causal_root.rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            edge = yaml.safe_load(handle)
        edge["source_path"] = str(path.relative_to(root))
        edges.append(edge)
    return edges


def load_concepts(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    concepts: dict[str, dict[str, Any]] = {}
    knowledge_root = root / "knowledge"
    for path in sorted(knowledge_root.rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        if not isinstance(document, dict):
            continue
        concept_id = document.get("id")
        kind = document.get("kind")
        if not isinstance(concept_id, str) or not isinstance(kind, str):
            continue
        concepts[concept_id] = document
    return concepts


def _sorted_outgoing(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)
    for values in outgoing.values():
        values.sort(key=lambda edge: (edge["target"], edge["relation"], edge["id"]))
    return outgoing


def _sorted_incoming(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    incoming: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], []).append(edge)
    for values in incoming.values():
        values.sort(key=lambda edge: (edge["source"], edge["relation"], edge["id"]))
    return incoming


def shortest_path(
    source: str,
    target: str,
    edges: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]] | None:
    outgoing = _sorted_outgoing(edges)
    queue: deque[str] = deque([source])
    previous: dict[str, tuple[str, dict[str, Any]] | None] = {source: None}

    while queue:
        node = queue.popleft()
        if node == target:
            break
        for edge in outgoing.get(node, []):
            next_node = edge["target"]
            if next_node in previous:
                continue
            previous[next_node] = (node, edge)
            queue.append(next_node)

    if target not in previous:
        return None

    nodes = [target]
    path_edges: list[dict[str, Any]] = []
    current = target
    while current != source:
        step = previous[current]
        if step is None:
            raise RuntimeError("causal path reconstruction failed")
        prior, edge = step
        path_edges.append(edge)
        nodes.append(prior)
        current = prior

    nodes.reverse()
    path_edges.reverse()
    return nodes, path_edges


def reverse_shortest_paths(
    target: str,
    edges: list[dict[str, Any]],
    max_depth: int | None = None,
) -> list[tuple[list[str], list[dict[str, Any]]]]:
    incoming = _sorted_incoming(edges)
    queue: deque[tuple[str, int]] = deque([(target, 0)])
    next_step: dict[str, tuple[str, dict[str, Any]] | None] = {target: None}
    distance: dict[str, int] = {target: 0}

    while queue:
        node, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for edge in incoming.get(node, []):
            prior = edge["source"]
            if prior in next_step:
                continue
            next_step[prior] = (node, edge)
            distance[prior] = depth + 1
            queue.append((prior, depth + 1))

    results: list[tuple[list[str], list[dict[str, Any]]]] = []
    antecedents = sorted(
        (node for node in next_step if node != target),
        key=lambda node: (distance[node], node),
    )
    for source in antecedents:
        nodes = [source]
        path_edges: list[dict[str, Any]] = []
        current = source
        while current != target:
            step = next_step[current]
            if step is None:
                raise RuntimeError("reverse causal path reconstruction failed")
            next_node, edge = step
            path_edges.append(edge)
            nodes.append(next_node)
            current = next_node
        results.append((nodes, path_edges))

    return results


def concept_view(concept_id: str, concepts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw = concepts.get(concept_id, {})
    output: dict[str, Any] = {"id": concept_id}
    for key in ("kind", "title", "summary"):
        value = raw.get(key)
        if isinstance(value, str):
            output[key] = value
    return output


def edge_view(edge: dict[str, Any]) -> dict[str, Any]:
    output = {
        "id": edge["id"],
        "source": edge["source"],
        "target": edge["target"],
        "relation": edge["relation"],
        "strength": edge["strength"],
        "explanation": edge["explanation"],
        "source_path": edge["source_path"],
    }
    for key in ("conditions", "evidence", "limitations"):
        if key in edge:
            output[key] = edge[key]
    return output


def path_view(
    nodes: list[str],
    path_edges: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": nodes[0],
        "target": nodes[-1],
        "distance": len(path_edges),
        "nodes": [concept_view(node, concepts) for node in nodes],
        "edges": [edge_view(edge) for edge in path_edges],
    }


def project_path(
    source: str,
    target: str,
    edges: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = shortest_path(source, target, edges)
    paths = [] if result is None else [path_view(*result, concepts)]
    return {
        "schema_version": "0.1",
        "kind": "causal_projection",
        "query": {
            "mode": "path",
            "source": source,
            "target": target,
        },
        "found": bool(paths),
        "paths": paths,
    }


def project_causes(
    target: str,
    edges: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
    max_depth: int | None = None,
) -> dict[str, Any]:
    results = reverse_shortest_paths(target, edges, max_depth=max_depth)
    paths = [path_view(nodes, path_edges, concepts) for nodes, path_edges in results]
    return {
        "schema_version": "0.1",
        "kind": "causal_projection",
        "query": {
            "mode": "causes",
            "target": target,
            "max_depth": max_depth,
        },
        "found": bool(paths),
        "paths": paths,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project Atlerror causal graph data as transport-independent JSON."
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    path_parser = subparsers.add_parser("path", help="Find the shortest directed causal path")
    path_parser.add_argument("source", help="Source concept ID")
    path_parser.add_argument("target", help="Target concept ID")

    causes_parser = subparsers.add_parser(
        "causes",
        help="Find upstream causal antecedents and one shortest path from each antecedent",
    )
    causes_parser.add_argument("target", help="Target concept ID")
    causes_parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum number of upstream edges to traverse",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "max_depth", None) is not None and args.max_depth < 1:
        parser.error("--max-depth must be at least 1")

    edges = load_edges()
    concepts = load_concepts()
    if args.command == "path":
        projection = project_path(args.source, args.target, edges, concepts)
    else:
        projection = project_causes(args.target, edges, concepts, max_depth=args.max_depth)

    indent = 2 if args.pretty else None
    print(json.dumps(projection, indent=indent, sort_keys=True))
    return 0 if projection["found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

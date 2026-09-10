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


def load_edges() -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for path in sorted(CAUSAL_ROOT.rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            edge = yaml.safe_load(handle)
        edge["path"] = str(path.relative_to(ROOT))
        edges.append(edge)
    return edges


def find_path(source: str, target: str, edges: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]] | None:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Find a directed path through Atlerror causal edges.")
    parser.add_argument("source", help="Source concept ID")
    parser.add_argument("target", help="Target concept ID")
    args = parser.parse_args()

    edges = load_edges()
    result = find_path(args.source, args.target, edges)
    if result is None:
        print(json.dumps({"found": False, "source": args.source, "target": args.target}, sort_keys=True))
        return 1

    nodes, path_edges = result
    output_edges = [
        {
            "id": edge["id"],
            "source": edge["source"],
            "target": edge["target"],
            "relation": edge["relation"],
            "strength": edge["strength"],
            "path": edge["path"],
        }
        for edge in path_edges
    ]
    print(
        json.dumps(
            {
                "found": True,
                "source": args.source,
                "target": args.target,
                "nodes": nodes,
                "edges": output_edges,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

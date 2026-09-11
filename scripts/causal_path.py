#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json

from causal_projection import edge_view, load_edges, shortest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Find a directed path through Atlerror causal edges.")
    parser.add_argument("source", help="Source concept ID")
    parser.add_argument("target", help="Target concept ID")
    args = parser.parse_args()

    edges = load_edges()
    result = shortest_path(args.source, args.target, edges)
    if result is None:
        print(json.dumps({"found": False, "source": args.source, "target": args.target}, sort_keys=True))
        return 1

    nodes, path_edges = result
    output_edges = []
    for edge in path_edges:
        projected = edge_view(edge)
        output_edges.append(
            {
                "id": projected["id"],
                "source": projected["source"],
                "target": projected["target"],
                "relation": projected["relation"],
                "strength": projected["strength"],
                "path": projected["source_path"],
            }
        )

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

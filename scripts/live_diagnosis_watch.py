#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from causal_projection import ROOT, load_concepts, load_edges
from live_diagnosis import LiveDiagnosisEngine

DEFAULT_RECEIVER_URL = "http://127.0.0.1:4318"
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_TIMEOUT_SECONDS = 5.0


def evidence_fingerprint(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def fetch_evidence(receiver_url: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    endpoint = urljoin(receiver_url.rstrip("/") + "/", "evidence")
    request = Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise ValueError(f"evidence endpoint returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"cannot reach evidence endpoint: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise ValueError("evidence endpoint must return a JSON object")
    if payload.get("kind") != "runtime_evidence":
        raise ValueError("evidence endpoint did not return a runtime_evidence document")
    return payload


class DiagnosisWatcher:
    def __init__(
        self,
        *,
        receiver_url: str,
        engine: LiveDiagnosisEngine,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.receiver_url = receiver_url
        self.engine = engine
        self.timeout_seconds = timeout_seconds
        self._fingerprint: str | None = None
        self._evidence_revision = 0
        self._polls_total = 0
        self._evidence_changes_total = 0
        self._no_evidence_polls_total = 0

    def tick(self) -> dict[str, Any]:
        document = fetch_evidence(
            self.receiver_url,
            timeout_seconds=self.timeout_seconds,
        )
        self._polls_total += 1
        if document is None:
            self._no_evidence_polls_total += 1
            return self.status(state="waiting_for_evidence")

        fingerprint = evidence_fingerprint(document)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._evidence_revision += 1
            self._evidence_changes_total += 1
            self.engine.refresh(
                document,
                evidence_revision=self._evidence_revision,
            )
            state = "evidence_changed"
        else:
            before = self.engine.status()["diagnosis_runs_total"]
            self.engine.current(
                document,
                evidence_revision=self._evidence_revision,
            )
            after = self.engine.status()["diagnosis_runs_total"]
            state = "freshness_recomputed" if after > before else "unchanged"
        return self.status(state=state)

    def status(self, *, state: str = "ok") -> dict[str, Any]:
        return {
            "status": "ok",
            "state": state,
            "receiver_url": self.receiver_url,
            "polls_total": self._polls_total,
            "evidence_revision": self._evidence_revision,
            "evidence_changes_total": self._evidence_changes_total,
            "no_evidence_polls_total": self._no_evidence_polls_total,
            **self.engine.status(),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Watch an Atlerror OTLP receiver evidence endpoint and automatically refresh "
            "transparent causal diagnosis snapshots."
        )
    )
    parser.add_argument(
        "--receiver-url",
        default=DEFAULT_RECEIVER_URL,
        help=f"Atlerror receiver base URL, default {DEFAULT_RECEIVER_URL}",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="JSON diagnosis snapshot atomically updated when diagnosis changes",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout for the evidence endpoint",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Optional maximum upstream causal depth for every automatic ranking",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, update the snapshot if evidence exists, then exit",
    )
    parser.add_argument("--verbose", action="store_true", help="Print watcher status updates")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be at least 1")

    try:
        engine = LiveDiagnosisEngine(
            concepts=load_concepts(root),
            edges=load_edges(root),
            snapshot_path=args.snapshot,
            max_depth=args.max_depth,
        )
        watcher = DiagnosisWatcher(
            receiver_url=args.receiver_url,
            engine=engine,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    while True:
        try:
            status = watcher.tick()
        except ValueError as exc:
            print(f"live diagnosis poll failed: {exc}", file=sys.stderr)
            if args.once:
                return 1
            time.sleep(args.poll_interval)
            continue

        if args.verbose:
            print(json.dumps(status, sort_keys=True), file=sys.stderr)
        if args.once:
            return 0 if status["evidence_revision"] > 0 else 1
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())

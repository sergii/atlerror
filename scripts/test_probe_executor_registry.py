#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from causal_projection import ROOT, load_concepts
from probe_execution import (
    begin_probe_session,
    build_probe_execution_capabilities,
    finish_probe_session,
)
from probe_executor_registry import (
    CPU_UTILIZATION_EXECUTOR_ID,
    CPU_UTILIZATION_OBSERVATION_ID,
    CPU_UTILIZATION_PROBE_ID,
    TCP_INTEGRITY_PROBE_ID,
    default_executor_registry,
    parse_proc_stat_cpu,
)


def proc_stat_text(*, user: int, system: int, idle: int, iowait: int = 0) -> str:
    return f"cpu  {user} 0 {system} {idle} {iowait} 0 0 0 0 0\n"


class ProbeExecutorRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)

    def test_registry_projects_two_registered_read_only_executors(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            snmp = directory / "snmp"
            stat = directory / "stat"
            snmp.write_text("Tcp: InErrs\nTcp: 0\n", encoding="utf-8")
            stat.write_text(proc_stat_text(user=10, system=5, idle=85), encoding="utf-8")
            registry = default_executor_registry(
                {
                    TCP_INTEGRITY_PROBE_ID: snmp,
                    CPU_UTILIZATION_PROBE_ID: stat,
                }
            )
            projection = build_probe_execution_capabilities(self.concepts, registry=registry)
            self.assertEqual("probe_execution_capabilities", projection["kind"])
            self.assertEqual(
                [CPU_UTILIZATION_PROBE_ID, TCP_INTEGRITY_PROBE_ID],
                [entry["probe"]["id"] for entry in projection["executors"]],
            )
            self.assertTrue(all(entry["available"] for entry in projection["executors"]))
            cpu = next(
                entry
                for entry in projection["executors"]
                if entry["probe"]["id"] == CPU_UTILIZATION_PROBE_ID
            )
            self.assertEqual(CPU_UTILIZATION_EXECUTOR_ID, cpu["executor"]["id"])
            self.assertEqual(80.0, cpu["policy"]["observed_threshold_pct"])

    def test_parse_proc_stat_cpu_uses_idle_plus_iowait(self) -> None:
        parsed = parse_proc_stat_cpu(
            proc_stat_text(user=30, system=10, idle=50, iowait=10)
        )
        self.assertEqual({"idle": 60, "total": 100}, parsed)
        with self.assertRaisesRegex(ValueError, "aggregate cpu row"):
            parse_proc_stat_cpu("intr 1 2 3\n")

    def test_cpu_executor_emits_observed_measurement_above_policy_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            source = Path(directory_name) / "stat"
            source.write_text(proc_stat_text(user=100, system=0, idle=900), encoding="utf-8")
            registry = default_executor_registry({CPU_UTILIZATION_PROBE_ID: source})
            session = begin_probe_session(
                incident_id="incident.cpu.test",
                probe_id=CPU_UTILIZATION_PROBE_ID,
                concepts=self.concepts,
                registry=registry,
                started_at=datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc),
            )
            self.assertEqual("registered", session["executor"]["source_mode"])
            self.assertEqual("proc.stat.cpu", session["baseline"]["metric"])
            self.assertEqual({"idle": 900, "total": 1000}, session["baseline"]["values"])

            source.write_text(proc_stat_text(user=190, system=0, idle=910), encoding="utf-8")
            evidence = finish_probe_session(
                session,
                self.concepts,
                registry=registry,
                finished_at=datetime(2026, 9, 12, 0, 0, 10, tzinfo=timezone.utc),
            )
            instance = evidence["instances"][0]
            self.assertEqual(CPU_UTILIZATION_OBSERVATION_ID, instance["observation"])
            self.assertEqual("observed", instance["state"])
            self.assertEqual(90.0, instance["measurement"]["value"])
            self.assertEqual(80.0, instance["measurement"]["baseline"])
            self.assertEqual(10.0, instance["measurement"]["delta"])
            self.assertEqual("percent", instance["measurement"]["unit"])

    def test_cpu_executor_emits_absence_below_policy_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            source = Path(directory_name) / "stat"
            source.write_text(proc_stat_text(user=100, system=0, idle=900), encoding="utf-8")
            registry = default_executor_registry({CPU_UTILIZATION_PROBE_ID: source})
            session = begin_probe_session(
                incident_id="incident.cpu.test",
                probe_id=CPU_UTILIZATION_PROBE_ID,
                concepts=self.concepts,
                registry=registry,
                started_at=datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc),
            )
            source.write_text(proc_stat_text(user=120, system=0, idle=980), encoding="utf-8")
            evidence = finish_probe_session(
                session,
                self.concepts,
                registry=registry,
                finished_at=datetime(2026, 9, 12, 0, 0, 10, tzinfo=timezone.utc),
            )
            instance = evidence["instances"][0]
            self.assertEqual("absent", instance["state"])
            self.assertEqual(20.0, instance["measurement"]["value"])
            self.assertEqual("below_baseline", instance["measurement"]["comparison"])

    def test_cpu_counter_reset_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            source = Path(directory_name) / "stat"
            source.write_text(proc_stat_text(user=100, system=20, idle=900), encoding="utf-8")
            registry = default_executor_registry({CPU_UTILIZATION_PROBE_ID: source})
            session = begin_probe_session(
                incident_id="incident.cpu.test",
                probe_id=CPU_UTILIZATION_PROBE_ID,
                concepts=self.concepts,
                registry=registry,
                started_at=datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc),
            )
            source.write_text(proc_stat_text(user=10, system=2, idle=90), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "counters decreased"):
                finish_probe_session(
                    session,
                    self.concepts,
                    registry=registry,
                    finished_at=datetime(2026, 9, 12, 0, 0, 10, tzinfo=timezone.utc),
                )


if __name__ == "__main__":
    unittest.main()

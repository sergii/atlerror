#!/usr/bin/env python3

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "vocabulary" / "diagnostic-catalog.yaml"

SYMPTOM_CODE = re.compile(r"^S[1-9][0-9]*$")
FAMILY_CODE = re.compile(r"^[A-Z][1-9][0-9]*$")
MECHANISM_CODE = re.compile(r"^[A-Z][1-9][0-9]*\.[1-9][0-9]*$")
LAB_CODE = re.compile(r"^[A-Z][1-9][0-9]*\.[1-9][0-9]*-L[1-9][0-9]*$")


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def ids_under(directory: str) -> set[str]:
    ids: set[str] = set()
    for path in sorted((ROOT / directory).rglob("*.yaml")):
        document = load_yaml(path) or {}
        object_id = document.get("id")
        if object_id:
            ids.add(object_id)
    return ids


def fail(errors: list[str]) -> None:
    for error in errors:
        print(f"ERROR: {error}")
    raise SystemExit(1)


def validate() -> None:
    document = load_yaml(CATALOG) or {}
    errors: list[str] = []

    concept_ids = ids_under("knowledge")
    claim_ids = ids_under("claims")
    experiment_ids = ids_under("experiments")

    domains = document.get("domains", [])
    domain_prefixes: set[str] = set()
    for domain in domains:
        prefix = domain.get("prefix")
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z]", prefix):
            errors.append(f"invalid domain prefix: {prefix!r}")
            continue
        if prefix in domain_prefixes:
            errors.append(f"duplicate domain prefix: {prefix}")
        domain_prefixes.add(prefix)

    family_codes: set[str] = set()
    for family in document.get("families", []):
        code = family.get("code")
        domain = family.get("domain")
        if not isinstance(code, str) or not FAMILY_CODE.fullmatch(code):
            errors.append(f"invalid family code: {code!r}")
            continue
        if code in family_codes:
            errors.append(f"duplicate family code: {code}")
        family_codes.add(code)
        if domain not in domain_prefixes:
            errors.append(f"{code}: unknown domain: {domain}")
        elif not code.startswith(domain):
            errors.append(f"{code}: family prefix does not match domain {domain}")

    entries = document.get("entries", [])
    entries_by_code: dict[str, dict[str, Any]] = {}
    lab_codes: set[str] = set()

    for entry in entries:
        code = entry.get("code")
        kind = entry.get("kind")
        if not isinstance(code, str):
            errors.append(f"entry missing code: {entry!r}")
            continue
        if code in entries_by_code:
            errors.append(f"duplicate catalog code: {code}")
            continue
        entries_by_code[code] = entry

        if kind == "symptom":
            if not SYMPTOM_CODE.fullmatch(code):
                errors.append(f"{code}: symptom code must match S<number>")
            canonical_id = entry.get("canonical_id")
            if canonical_id not in concept_ids:
                errors.append(f"{code}: unresolved canonical symptom id: {canonical_id}")
            continue

        if kind != "mechanism":
            errors.append(f"{code}: unknown entry kind: {kind}")
            continue

        if not MECHANISM_CODE.fullmatch(code):
            errors.append(f"{code}: mechanism code must match <prefix><family>.<topic>")
        family_code = code.split(".", 1)[0]
        if family_code not in family_codes:
            errors.append(f"{code}: family not declared: {family_code}")
        elif code[0] not in domain_prefixes:
            errors.append(f"{code}: domain prefix not declared")

        coverage = entry.get("coverage")
        if coverage not in {"planned", "semantic", "empirical"}:
            errors.append(f"{code}: invalid coverage: {coverage}")

        canonical_id = entry.get("canonical_id")
        if coverage != "planned" and canonical_id not in concept_ids:
            errors.append(f"{code}: unresolved canonical mechanism id: {canonical_id}")
        elif canonical_id is not None and canonical_id not in concept_ids:
            errors.append(f"{code}: canonical_id does not resolve: {canonical_id}")

        for claim_id in entry.get("claims", []):
            if claim_id not in claim_ids:
                errors.append(f"{code}: unresolved claim: {claim_id}")

        experiments = entry.get("experiments", [])
        if coverage == "empirical" and not experiments:
            errors.append(f"{code}: empirical coverage requires at least one experiment")
        for experiment in experiments:
            lab_code = experiment.get("code")
            experiment_id = experiment.get("id")
            if not isinstance(lab_code, str) or not LAB_CODE.fullmatch(lab_code):
                errors.append(f"{code}: invalid lab code: {lab_code!r}")
            elif not lab_code.startswith(f"{code}-L"):
                errors.append(f"{code}: lab code must inherit mechanism code: {lab_code}")
            elif lab_code in lab_codes:
                errors.append(f"duplicate lab code: {lab_code}")
            else:
                lab_codes.add(lab_code)
            if experiment_id not in experiment_ids:
                errors.append(f"{code}: unresolved experiment: {experiment_id}")

        for lab_code in entry.get("planned_labs", []):
            if not isinstance(lab_code, str) or not LAB_CODE.fullmatch(lab_code):
                errors.append(f"{code}: invalid planned lab code: {lab_code!r}")
            elif not lab_code.startswith(f"{code}-L"):
                errors.append(f"{code}: planned lab must inherit mechanism code: {lab_code}")
            elif lab_code in lab_codes:
                errors.append(f"duplicate lab code: {lab_code}")
            else:
                lab_codes.add(lab_code)

    for code, entry in entries_by_code.items():
        for target in entry.get("routes_to", []):
            if target not in entries_by_code:
                errors.append(f"{code}: unresolved routes_to code: {target}")
            elif entries_by_code[target].get("kind") != "mechanism":
                errors.append(f"{code}: routes_to must target a mechanism: {target}")
        for symptom in entry.get("symptoms", []):
            if symptom not in entries_by_code:
                errors.append(f"{code}: unresolved symptom code: {symptom}")
            elif entries_by_code[symptom].get("kind") != "symptom":
                errors.append(f"{code}: symptoms must reference symptom entries: {symptom}")

    for planned in document.get("planned_examples", []):
        code = planned.get("code")
        if not isinstance(code, str) or not MECHANISM_CODE.fullmatch(code):
            errors.append(f"invalid planned example code: {code!r}")
            continue
        if code in entries_by_code:
            errors.append(f"planned example duplicates catalog entry: {code}")
        family_code = code.split(".", 1)[0]
        if family_code not in family_codes:
            errors.append(f"planned example uses undeclared family: {code}")

    if errors:
        fail(errors)

    symptoms = sum(1 for entry in entries if entry.get("kind") == "symptom")
    mechanisms = sum(1 for entry in entries if entry.get("kind") == "mechanism")
    empirical = sum(1 for entry in entries if entry.get("coverage") == "empirical")
    semantic = sum(1 for entry in entries if entry.get("coverage") == "semantic")
    planned = sum(1 for entry in entries if entry.get("coverage") == "planned")
    print(
        f"Validated diagnostic catalog: {symptoms} symptoms, {mechanisms} mechanisms, "
        f"{len(lab_codes)} lab codes ({empirical} empirical, {semantic} semantic, {planned} planned)."
    )


if __name__ == "__main__":
    validate()

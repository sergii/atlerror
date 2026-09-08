#!/usr/bin/env python3

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "vocabulary" / "diagnostic-catalog.yaml"
PROFILES = ROOT / "vocabulary" / "diagnostic-profiles.yaml"
HISTORY = ROOT / "vocabulary" / "diagnostic-code-history.yaml"

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


def validate_profiles(entries_by_code: dict[str, dict[str, Any]], errors: list[str]) -> tuple[int, int]:
    document = load_yaml(PROFILES) or {}
    facet_dimensions = document.get("facet_dimensions", {})
    coverage_dimensions = document.get("coverage_dimensions", {})
    profiles = document.get("profiles", [])
    profiles_by_code: dict[str, dict[str, Any]] = {}

    mechanism_codes = {code for code, entry in entries_by_code.items() if entry.get("kind") == "mechanism"}

    for profile in profiles:
        code = profile.get("code")
        if code not in mechanism_codes:
            errors.append(f"profile references unknown/non-mechanism code: {code}")
            continue
        if code in profiles_by_code:
            errors.append(f"duplicate diagnostic profile: {code}")
            continue
        profiles_by_code[code] = profile

        facets = profile.get("facets", {})
        for dimension, values in facets.items():
            allowed = set(facet_dimensions.get(dimension, []))
            if not allowed:
                errors.append(f"{code}: unknown facet dimension: {dimension}")
                continue
            if not isinstance(values, list) or not values:
                errors.append(f"{code}: facet {dimension} must be a non-empty list")
                continue
            for value in values:
                if value not in allowed:
                    errors.append(f"{code}: invalid {dimension} facet: {value}")

        coverage = profile.get("coverage", {})
        missing_coverage = set(coverage_dimensions) - set(coverage)
        extra_coverage = set(coverage) - set(coverage_dimensions)
        if missing_coverage:
            errors.append(f"{code}: missing coverage dimensions: {sorted(missing_coverage)}")
        if extra_coverage:
            errors.append(f"{code}: unknown coverage dimensions: {sorted(extra_coverage)}")
        for dimension, value in coverage.items():
            if value not in set(coverage_dimensions.get(dimension, [])):
                errors.append(f"{code}: invalid coverage value {dimension}={value}")

        empirical = profile.get("empirical", {})
        synthetic_labs = empirical.get("synthetic_labs")
        if not isinstance(synthetic_labs, int) or synthetic_labs < 0:
            errors.append(f"{code}: empirical.synthetic_labs must be a non-negative integer")
            continue
        catalog_lab_count = len(entries_by_code[code].get("experiments", []))
        if synthetic_labs != catalog_lab_count:
            errors.append(f"{code}: profile synthetic_labs={synthetic_labs} but catalog has {catalog_lab_count} experiments")
        if entries_by_code[code].get("coverage") == "empirical" and synthetic_labs < 1:
            errors.append(f"{code}: empirical catalog coverage requires at least one synthetic lab in profile")
        if empirical.get("production_evidence") not in {"none", "observed", "validated"}:
            errors.append(f"{code}: invalid production_evidence: {empirical.get('production_evidence')}")

    missing_profiles = mechanism_codes - set(profiles_by_code)
    if missing_profiles:
        errors.append(f"mechanisms missing diagnostic profiles: {sorted(missing_profiles)}")

    synthetic_labs = sum((profile.get("empirical", {}).get("synthetic_labs") or 0) for profile in profiles_by_code.values())
    return len(profiles_by_code), synthetic_labs


def validate_history(entries_by_code: dict[str, dict[str, Any]], planned_codes: set[str], errors: list[str]) -> tuple[int, int]:
    document = load_yaml(HISTORY) or {}
    policy = document.get("policy", {})
    if policy.get("published_codes_immutable") is not True:
        errors.append("code history must declare published_codes_immutable: true")
    if policy.get("reuse_for_different_meaning") != "forbidden":
        errors.append("code history must forbid reuse_for_different_meaning")

    resolvable = set(entries_by_code) | planned_codes
    alias_codes: set[str] = set()
    for alias in document.get("aliases", []):
        code, target = alias.get("code"), alias.get("target")
        if not isinstance(code, str) or not (SYMPTOM_CODE.fullmatch(code) or MECHANISM_CODE.fullmatch(code)):
            errors.append(f"invalid alias code: {code!r}")
            continue
        if code in alias_codes:
            errors.append(f"duplicate alias code: {code}")
        alias_codes.add(code)
        if target not in resolvable:
            errors.append(f"{code}: alias target is not resolvable: {target}")
        if code == target:
            errors.append(f"{code}: alias cannot target itself")

    deprecated_codes: set[str] = set()
    for item in document.get("deprecations", []):
        code, target = item.get("code"), item.get("superseded_by")
        if not isinstance(code, str) or not (SYMPTOM_CODE.fullmatch(code) or MECHANISM_CODE.fullmatch(code)):
            errors.append(f"invalid deprecated code: {code!r}")
            continue
        if code in deprecated_codes:
            errors.append(f"duplicate deprecated code: {code}")
        deprecated_codes.add(code)
        if target is not None and target not in resolvable:
            errors.append(f"{code}: superseded_by is not resolvable: {target}")
        if target == code:
            errors.append(f"{code}: deprecation cannot supersede itself")

    overlap = alias_codes & deprecated_codes
    if overlap:
        errors.append(f"codes cannot be both alias and deprecation: {sorted(overlap)}")
    return len(alias_codes), len(deprecated_codes)


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

    planned_codes: set[str] = set()
    for planned in document.get("planned_examples", []):
        code = planned.get("code")
        if not isinstance(code, str) or not MECHANISM_CODE.fullmatch(code):
            errors.append(f"invalid planned example code: {code!r}")
            continue
        if code in entries_by_code:
            errors.append(f"planned example duplicates catalog entry: {code}")
        planned_codes.add(code)
        family_code = code.split(".", 1)[0]
        if family_code not in family_codes:
            errors.append(f"planned example uses undeclared family: {code}")

    profile_count, profile_lab_count = validate_profiles(entries_by_code, errors)
    alias_count, deprecated_count = validate_history(entries_by_code, planned_codes, errors)

    if errors:
        fail(errors)

    symptoms = sum(1 for entry in entries if entry.get("kind") == "symptom")
    mechanisms = sum(1 for entry in entries if entry.get("kind") == "mechanism")
    empirical = sum(1 for entry in entries if entry.get("coverage") == "empirical")
    semantic = sum(1 for entry in entries if entry.get("coverage") == "semantic")
    planned = sum(1 for entry in entries if entry.get("coverage") == "planned")
    print(
        f"Validated diagnostic catalog: {symptoms} symptoms, {mechanisms} mechanisms, "
        f"{len(lab_codes)} lab codes ({empirical} empirical, {semantic} semantic, {planned} planned); "
        f"{profile_count} profiles / {profile_lab_count} synthetic labs; "
        f"{alias_count} aliases / {deprecated_count} deprecations."
    )


if __name__ == "__main__":
    validate()

# Atlerror Lab

Atlerror Lab contains small reproducible experiments that test diagnostic mechanisms encoded by the semantic knowledge base.

The goal is not to prove universal truths from one synthetic environment. Experiments produce empirical evidence that supports, contradicts, or leaves a diagnostic claim inconclusive under a recorded environment.

## Contract

An empirical check has three layers:

1. `claims/**/*.yaml` - a stable, machine-addressable statement derived from a hypothesis.
2. `experiments/**/*.yaml` - a manifest that references the claim and declares how to run a fixture.
3. `lab/**` - fixture code that creates a controlled physical intervention and emits `EmpiricalEvidence` JSON.

Run an experiment with:

```bash
python scripts/run_lab.py experiments/memory/retention-ruby.yaml
```

The generic runner:

1. validates the experiment manifest;
2. builds the declared Docker image;
3. runs the fixture with declared resource constraints;
4. parses evidence JSON from the fixture;
5. validates the evidence schema;
6. verifies experiment and claim references;
7. checks the expected result;
8. writes the result under `lab-results/`.

## Principles

- Prefer the smallest environment that can reproduce the mechanism.
- Use real operating-system/runtime behavior rather than mocked measurements when practical.
- Record environment and intervention explicitly.
- Treat results as evidence, not proof.
- Keep experiments deterministic enough for CI when possible.
- Add semantic concepts only when a real measurement or diagnostic distinction requires them.

## Evidence semantics

Results are deliberately limited to:

- `supports`
- `contradicts`
- `inconclusive`

A successful synthetic experiment does not establish production frequency, impact, or exclusivity of a cause.

## Current experiments

- Ruby retained objects -> live heap and RSS growth
- Ruby busy loop -> near-one-core CPU utilization with user-space dominance

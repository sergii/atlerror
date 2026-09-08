# Atlerror Lab

Atlerror Lab contains small reproducible experiments that test diagnostic mechanisms encoded by the semantic knowledge base.

The goal is not to prove universal truths from one synthetic environment. Experiments should produce empirical evidence that supports, contradicts, or leaves a diagnostic claim inconclusive under a recorded environment.

## Principles

- Prefer the smallest environment that can reproduce the mechanism.
- Use real operating-system/runtime behavior rather than mocked measurements when practical.
- Record environment and intervention explicitly.
- Distinguish managed-runtime state from operating-system process metrics.
- Treat results as evidence, not proof.
- Keep experiments deterministic enough for CI when possible.

## Current experiment

`memory-retention/` deliberately retains Ruby objects inside a Docker container and records process RSS plus Ruby heap live slots before, during, and after retention.

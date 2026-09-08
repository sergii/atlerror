# Atlerror Semantic Specification

This document defines the minimal semantic contract used by Atlerror knowledge files.

## Dual-use invariant

> Every concept should be useful to a human and addressable by a machine. Every diagnostic relationship should be explainable to a human and executable or testable by an agent where possible.

## Layers

Atlerror separates four concerns:

1. **Vocabulary** - the language: concept kinds, relations, action classes, and semantic constraints.
2. **Knowledge** - concrete facts expressed using that language.
3. **Rules** - deterministic inference over observations and hypotheses.
4. **Projections** - generated views for people and software: docs, website pages, CLI, MCP, HTTP APIs, agent skills, or other adapters.

The canonical semantic source is machine-readable YAML validated by JSON Schema. Markdown is a human projection and explanatory layer, not the source of truth for executable relations.

## Stable IDs

IDs are namespaced by semantic role rather than by product name.

Examples:

```text
symptom.cpu.high
hypothesis.traffic.increase
hypothesis.cpu.busy_loop
observation.http.request_rate
probe.http.inspect_request_rate
capability.cpu.profile
tool.ebpf
```

Product or repository names MUST NOT be embedded into semantic IDs.

## Minimal concept kinds

The current executable slice uses only a subset of the planned model:

- `symptom`
- `hypothesis`
- `observation`
- `probe`

The broader target model is documented in `RFC/0001-semantic-foundation.md`.

## Diagnostic semantics

### Symptom

A deviation from expected system behavior. A symptom does not assert a cause.

### Observation

A concrete measured or reported fact about a system. Observations should carry provenance when runtime instances are modeled.

### Hypothesis

A candidate explanation for one or more symptoms or observations. A hypothesis is not a fact and should define testable predictions.

### Prediction

An expected observation if a hypothesis is true. Predictions enable falsification and confidence updates.

### Probe

A diagnostic action whose primary purpose is to gather evidence. Probes should be side-effect free where possible and declare required capabilities.

## Relations

The minimal vocabulary supports these semantic relations:

- `may_indicate`: symptom -> hypothesis
- `predicts`: hypothesis -> expected observation or condition
- `tested_by`: hypothesis -> probe
- `produces`: probe -> observation
- `requires`: probe -> capability
- `supports`: observation -> hypothesis
- `contradicts`: observation -> hypothesis
- `related_to`: concept -> concept

`may_indicate` MUST NOT be interpreted as causality.

`supports` and `contradicts` SHOULD be treated as updates to belief or confidence, not universal proof, unless a rule explicitly declares a deterministic exclusion.

## Rules

Rules are separate from concepts. A rule describes how an observed condition changes diagnostic state.

Example:

```yaml
id: rule.traffic_increase.request_rate_normal
hypothesis: hypothesis.traffic.increase
when:
  observation: observation.http.request_rate
  operator: not_above_baseline
effect:
  confidence: decrease
```

The initial rule model intentionally avoids a global numeric probability system. Early rules use qualitative effects such as `increase`, `decrease`, and `reject`.

## Human-facing fields

Concepts may include explanatory fields such as:

- `title`
- `summary`
- `explanation`
- `why_it_matters`
- `common_misconceptions`
- `prerequisites`
- `examples`
- `search_terms`

These fields can power documentation, learning paths, SEO pages, and short educational content without changing the executable semantic relations.

## Machine-facing fields

Machine-facing fields include:

- stable `id`
- `kind`
- explicit relations
- predictions
- probes
- capabilities
- deterministic rules
- risk and approval metadata for actions as the model expands

## Integration boundary

The semantic core is transport independent.

Expected consumers include:

- CLI
- embedded library
- local daemon
- Unix domain socket
- MCP server
- HTTP API
- gRPC API if justified
- Run Witness-like local runtime instrumentation
- RunDiff/Plywo-style regression analysis
- incident and observability integrations

MCP is an adapter, not the ontology itself.

## Versioning

The project is experimental. Changes to IDs or relation semantics should be treated as breaking semantic changes even before a formal versioning policy is introduced.

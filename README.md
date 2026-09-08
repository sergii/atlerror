# Atlerror

Atlerror is an open, machine-readable semantic layer for software troubleshooting.

It is designed for two consumers at the same time:

1. **Humans** - to learn how software systems fail, how to reason about incidents, and how system design concepts relate to real failure modes.
2. **Machines and agents** - to classify observations, evaluate hypotheses, select diagnostic probes, and explain why a diagnostic action should happen next.

The project starts from a small executable vertical slice around **high CPU utilization** and grows toward a broader debugging ontology.

## Core idea

Atlerror models a diagnostic loop explicitly:

```text
symptom
  -> observations
  -> candidate hypotheses
  -> predictions
  -> probes / experiments
  -> findings
  -> hypothesis updates
  -> cause / contributing factors
  -> mitigation / fix
  -> verification / prevention
```

The long-term goal is to make this loop useful both as documentation and as a deterministic reasoning substrate for developer tools and AI agents.

## Design principles

- **One semantic source, multiple projections.** Human documentation, agent context, CLI output, APIs, MCP resources, and websites should derive from the same canonical knowledge.
- **Knowledge first. Tools second. AI third.** Atlerror should not depend on Datadog, OpenTelemetry, Kubernetes, Ruby, or an LLM. Those are adapters and consumers.
- **Stable machine-addressable IDs.** Product naming may change; semantic IDs should not.
- **Falsification matters.** A useful hypothesis defines what evidence would support it and what evidence would make it less likely.
- **Transport agnostic.** CLI, embedded libraries, MCP, HTTP, gRPC, or Unix domain sockets are integration choices above the semantic core.
- **Human-readable and machine-usable.** Every concept should teach something to a person and be addressable by software.

## Repository structure

```text
RFC/             design decisions and semantic roadmap
schema/          structural validation for machine-readable knowledge
vocabulary/      canonical kinds and relations
knowledge/       concrete troubleshooting knowledge
rules/           deterministic inference rules
examples/        complete diagnostic flows
```

## First vertical slice

The initial slice models `symptom.cpu.high` and a small set of hypotheses:

- `hypothesis.traffic.increase`
- `hypothesis.cpu.busy_loop`
- `hypothesis.gc.pressure`
- `hypothesis.kernel.work`

This slice is intentionally small. The broader semantic model is tracked in [RFC 0001](RFC/0001-semantic-foundation.md).

## Status

Experimental. The schema and vocabulary are expected to evolve while keeping the intent and stable IDs explicit.

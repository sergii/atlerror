# RFC 0001: Semantic Foundation

- Status: Draft
- Scope: Long-term semantic model
- Initial implementation: CPU High vertical slice

## Summary

Atlerror aims to become a canonical semantic layer for software troubleshooting that serves both human learning and machine/agent reasoning.

This RFC records the broader semantic model discussed at project inception. The initial implementation intentionally covers only a minimal subset. New concepts should be added incrementally through concrete vertical slices rather than by attempting to model the whole universe upfront.

## Product boundary

Atlerror is not primarily an observability backend, incident-management SaaS, or LLM wrapper.

It is a semantic and reasoning layer above telemetry and diagnostic tools.

Observability systems answer questions such as:

- What happened?
- What signals are available?
- Where did a request spend time?

Audit systems answer questions such as:

- Who or what actor changed state?
- What was authorized?
- What was the provenance of an action?

Atlerror aims to answer:

- What could explain these observations?
- What would each hypothesis predict?
- Which probe best distinguishes the remaining hypotheses?
- What evidence would falsify a hypothesis?
- What action is appropriate and safe next?

## Core semantic model

The following concepts form the planned semantic vocabulary.

### SystemEntity

A system component or runtime entity that can participate in a failure or observation.

Examples: service, process, container, host, database, table, queue, browser, endpoint, dependency, agent, tool.

### Boundary

An interface between system entities where expected input/output behavior can be compared.

Examples: browser -> CDN, load balancer -> application, application -> database, producer -> queue, queue -> consumer.

Boundary bisection is a core debugging strategy.

### Symptom

An observed deviation from expected behavior.

Examples: high CPU, elevated latency, duplicate event, incorrect result, missing side effect.

### Observation

A concrete fact measured or reported about the system.

Examples: CPU user time = 92%, request rate unchanged, trace span duration = 3.2 s.

### Signal

The source class from which observations are derived.

Planned signal types include:

- Metric
- Log
- Trace
- Span
- Event
- Profile
- Dump
- QueryPlan
- NetworkCapture
- UserReport
- Screenshot
- AuditRecord

### Evidence

An observation interpreted in a diagnostic context. Evidence can support, contradict, or constrain hypotheses.

### Hypothesis

A candidate explanation for symptoms or observations.

A useful hypothesis should be testable and should define predictions that can be checked.

### Prediction

An expected observation if a hypothesis is true.

Predictions are first-class because they make falsification possible.

### DiagnosticExperiment

A structured test designed to discriminate among hypotheses.

### Probe

A concrete diagnostic operation that gathers evidence.

Examples: inspect request rate, capture CPU profile, inspect blocked DB sessions, replay a request.

### Finding

The interpreted result of a probe or diagnostic experiment.

### FailureClass

A reusable category of failure behavior.

Candidate classes include:

- exception
- latency degradation
- CPU/resource saturation
- memory growth or leak
- intermittent failure
- wrong result
- missing operation/event
- duplicate/idempotency failure
- data inconsistency
- transaction failure
- restart-sensitive state
- node/environment-specific failure
- tenant/data-specific failure
- deploy regression
- external dependency failure
- network failure
- deadlock/contention
- race condition
- native/runtime crash
- security/auth failure

### Trigger

An event that initiates or exposes a failure condition.

### ContributingFactor

A condition that contributes to an incident without necessarily being sufficient or singularly causal.

### Cause

A causal factor that explains part of the failure mechanism.

Atlerror MUST NOT assume every incident has exactly one root cause.

### NecessaryCondition

A condition required for a failure mechanism to occur.

### LatentCondition

A pre-existing condition that makes a failure possible or more likely.

### ProximateCause

A causal factor close in time or mechanism to the observed failure.

### FailedDefense

A guardrail, validation, limit, fallback, invariant, review process, or other defense that should have prevented or contained the problem but did not.

### Amplifier

A factor that increases incident impact or propagation.

### MitigatingFactor

A factor that reduces impact or limits propagation.

### Mitigation

An action intended to stop or reduce user/system impact before the underlying mechanism is permanently fixed.

### Fix

A change intended to address the failure mechanism or causal factor.

### Verification

Evidence that a mitigation or fix achieved its intended effect and did not introduce unacceptable regressions.

### Prevention

A change intended to reduce recurrence probability or improve future detection and diagnosis.

### Guardrail

A preventive or limiting mechanism introduced before or after an incident.

### Tool

A concrete diagnostic or observability tool.

Examples: eBPF tooling, perf, rbspy, Datadog, Sentry, OpenTelemetry collectors, database clients.

### Capability

An abstract operation a tool or environment can provide.

Examples:

- metrics.query
- logs.query
- traces.query
- cpu.profile
- heap.capture
- database.inspect_locks
- network.capture

Knowledge should depend on capabilities where possible, not directly on vendor-specific tools.

### Action

A machine-addressable operation that may be executed by a human or agent.

Planned action classes include:

- QueryLogs
- InspectTrace
- CompareVersions
- Bisect
- ProfileCPU
- InspectHeap
- CheckLocks
- ReplayRequest
- RunQuery
- InspectQueue
- RestartComponent
- DisableFeatureFlag

### RiskLevel

A classification of the operational risk of an action.

Initial direction:

- read-only diagnostic
- low-risk diagnostic
- state-changing mitigation
- high-risk production mutation

### ApprovalPolicy

Defines whether an actor may execute an action autonomously or requires approval.

### Actor

An entity responsible for an observation, decision, or action.

Planned actors include:

- Human
- Service
- Agent
- SubAgent
- Tool
- Workflow
- Model

### Provenance

Metadata describing where evidence, observations, decisions, or actions came from.

Potential fields include source, actor, timestamp, system entity, request ID, trace ID, job ID, incident ID, tool invocation, and audit record.

### Timeline / Event

A time-ordered event model used to reconstruct incidents and correlate observations, deploys, configuration changes, traffic changes, dependencies, jobs, and actor actions.

## Planned relationships

The vocabulary is expected to evolve, but the following relationships are important candidates:

- Symptom `affects` SystemEntity
- Symptom `may_indicate` Hypothesis
- Observation `observed_at` SystemEntity
- Observation `derived_from` Signal
- Observation `supports` Hypothesis
- Observation `contradicts` Hypothesis
- Hypothesis `predicts` Prediction
- Hypothesis `tested_by` DiagnosticExperiment
- DiagnosticExperiment `uses` Probe
- DiagnosticExperiment `produces` Observation
- Probe `requires` Capability
- Tool `provides` Capability
- Finding `narrows_to` FailureClass
- Trigger `contributes_to` Failure
- ContributingFactor `contributes_to` Failure
- FailedDefense `allowed` Failure
- Mitigation `changes` SystemState
- Fix `addresses` CausalFactor
- Verification `verifies` Fix or Mitigation
- Prevention `introduces` Guardrail
- Action `has_risk_level` RiskLevel
- Action `governed_by` ApprovalPolicy
- Observation `has_provenance` Provenance
- Action `performed_by` Actor

## Falsification as a first-class requirement

Atlerror should not only encode evidence that supports a hypothesis.

For each important hypothesis, the model should make it possible to ask:

> What observation would make this hypothesis less likely or impossible?

This should drive diagnostic probe selection and reduce confirmation bias.

## Human learning requirements

The semantic model must support educational projections, including:

- short and detailed explanations
- prerequisites
- common misconceptions
- examples
- related concepts
- system-design context
- language/framework-specific notes
- search terms and common questions
- diagrams and learning paths
- generated website/SEO pages
- educational video outlines

These are projections over the same semantic knowledge, not a separate knowledge base.

## Agent requirements

The semantic model must support deterministic or semi-deterministic agent workflows:

```text
observations
  -> classify
  -> candidate hypotheses
  -> predictions
  -> choose discriminating probe
  -> execute through available capability/tool
  -> ingest new observation
  -> update hypothesis state
  -> repeat
```

LLMs may assist with hypothesis generation, interpretation of unknown evidence, mapping incidents to known patterns, and proposing missing experiments. Known diagnostic reasoning should not require an LLM to rediscover it from scratch.

## Integration architecture

The intended layering is:

```text
semantic knowledge
  -> core query/reasoning API
      -> embedded library
      -> CLI
      -> local daemon
          -> Unix domain socket
          -> MCP adapter
          -> HTTP API
          -> gRPC adapter if justified
```

Consumers may include local developer tools, Run Witness-style runtime instrumentation, RunDiff/Plywo-style regression analysis, incident systems, observability systems, CI, and autonomous agents.

## Action safety

Diagnostic knowledge should distinguish observation from mutation.

A future action model should make it possible to express:

- required capability
- expected evidence produced
- side effects
- risk level
- environment restrictions
- default approval policy
- reversibility

Read-only probes should be easy to automate. Production-changing actions should be policy controlled.

## Auditability and agentic systems

As systems gain non-human actors, troubleshooting and auditability increasingly overlap.

Atlerror should be able to reason over chains such as:

```text
human intent
  -> agent
  -> sub-agent
  -> tool call
  -> generated operation
  -> state mutation
```

The project should therefore preserve actor and provenance concepts from the beginning even if runtime audit-log integration arrives later.

## Implementation strategy

Do not implement this RFC top-down.

Instead:

1. choose a concrete failure slice;
2. model only the concepts needed to express it accurately;
3. validate the model against human and machine use cases;
4. add vocabulary and rules incrementally;
5. compile lessons from real incidents back into the semantic model.

The first slice is `symptom.cpu.high`.

## Open questions

- qualitative vs numeric confidence model
- rule language and inference semantics
- YAML layout vs graph-native storage
- schema versioning
- RDF/OWL export
- SHACL or equivalent semantic validation
- provenance runtime schema
- causal-edge vocabulary
- deterministic probe ranking
- cost-aware probe selection
- risk/approval policy language
- embedded core implementation language
- agent-facing MCP resource/tool design
- website and generated educational projections

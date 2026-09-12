# RFC 0020: Next-probe execution annotation

Status: Accepted

## Summary

Atlerror already separates two questions:

1. Which semantic probe best discriminates the current causal candidates?
2. Can this host actually execute that probe through a registered safe executor?

This RFC joins those answers at the live diagnosis boundary without allowing execution availability to influence causal or probe ranking.

Each diagnosable live target now contains:

```yaml
probe_ranking:
  # Existing deterministic semantic recommendation.

probe_execution:
  schema_version: "0.1"
  kind: probe_execution_annotation
  affects_ranking: false
  platform: linux
  probe_id: probe.network.inspect_tcp_integrity_errors
  registered: true
  executable_here: true
  unavailable_reason: null
  executor:
    id: executor.linux.proc_net_snmp.tcp_inerrs
    platform: linux
  capability: capability.network.inspect_tcp_integrity_errors
  observation: observation.network.tcp_integrity_errors
  source: /proc/net/snmp
  policy:
    classification: delta_positive
```

The annotation always describes the first entry of the already-computed `probe_ranking`. It never selects or reorders probes.

## Motivation

Before this change, an agent had to read two independent resources and join them itself:

```text
current diagnosis
  -> semantic next probe

probe execution capabilities
  -> registered executors on this host
```

That separation remains available and useful, but the live diagnosis can now answer the common planning question directly:

```text
next probe: probe.network.inspect_tcp_integrity_errors
recommended: yes
executable_here: yes
executor: executor.linux.proc_net_snmp.tcp_inerrs
```

or:

```text
next probe: probe.network.inspect_tcp_integrity_errors
recommended: yes
executable_here: no
reason: source does not exist: /proc/net/snmp
```

## Ranking boundary

Execution annotations are computed only after causal ranking and probe ranking are complete.

The order is:

```text
runtime evidence
  -> causal ranking
  -> probe ranking
  -> host execution capability projection
  -> execution annotation
```

The `probe_execution` object therefore contains:

```yaml
affects_ranking: false
```

as a machine-readable invariant.

Changing any of the following must not change probe ranks:

- current operating system;
- registered executor availability;
- whether a registered source exists;
- executor policy;
- an executor becoming temporarily unavailable.

Tests compare probe rankings across available and unavailable executor projections to preserve this invariant.

## Annotation states

### Recommended and executable

```yaml
probe_id: probe.network.inspect_tcp_integrity_errors
registered: true
executable_here: true
unavailable_reason: null
```

The annotation also includes the registered executor, capability, produced observation, source and explicit policy.

### Recommended but registered executor unavailable

```yaml
registered: true
executable_here: false
unavailable_reason: source does not exist: /proc/net/snmp
```

The semantic recommendation remains unchanged.

### Recommended but not registered

```yaml
registered: false
executable_here: false
unavailable_reason: no_registered_executor
executor: null
source: null
```

A probe can therefore remain the best semantic next step even when Atlerror cannot execute it locally.

### No semantic next probe

When `probe_ranking.found` is false:

```yaml
probe_id: null
registered: null
executable_here: null
unavailable_reason: no_recommended_probe
```

`null` is intentional here. Registration and host executability are not applicable when there is no recommended probe to annotate.

## Capability source

The annotation is derived from the same validated `probe_execution_capabilities` projection used by:

```bash
python scripts/probe_execution.py capabilities --pretty
```

and by the read-only MCP resource:

```text
atlerror://probe-execution/capabilities
```

No second capability model is introduced.

The live diagnosis computes the host capability projection once per snapshot build and reuses it for all diagnosis entries.

## Snapshot boundary

`probe_execution` is stored in the persisted `diagnosis_snapshot` beside `probe_ranking`.

Therefore existing read-only transports expose it automatically:

```text
GET /diagnosis
atlerror://diagnosis/current
```

Neither HTTP nor MCP recomputes executor availability or ranking.

## Runtime nature

Unlike causal knowledge and semantic probe ranking, execution availability is host-local runtime context.

A persisted snapshot can therefore differ across hosts even when semantic evidence and probe ranking are identical. Consumers must treat `probe_execution` as an annotation about the host that produced that snapshot, not as universal knowledge about the probe.

The semantic `probe_ranking` remains deterministic for the same evidence and knowledge catalog.

## Safety

This change does not execute a probe.

Capability inspection can check:

- platform compatibility;
- existence of the registered source;
- source file type;
- read permission.

It does not:

- capture a baseline;
- read diagnostic counters as probe evidence;
- start a probe session;
- run a command or subprocess;
- generate traffic;
- mutate the target system;
- perform remediation;
- enable MCP active tools.

The opt-in boundary for active MCP tools remains unchanged.

## Non-goals

This RFC does not add:

- capability-aware probe ranking;
- fallback re-ranking when the best probe is unavailable;
- automatic probe execution;
- remote executor discovery;
- dynamic plugins;
- a generic shell runner;
- remediation.

A future planner may use execution annotations when deciding what action to request next, but it must preserve the distinction between semantic recommendation quality and local action availability.

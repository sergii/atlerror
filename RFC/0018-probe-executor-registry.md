# RFC 0018: Probe executor registry and capability discovery

- Status: Accepted
- Date: 2026-09-12

## Summary

Atlerror active diagnostics now use an explicit built-in executor registry instead of a single hard-coded probe branch.

The registry maps a canonical read-only probe to one concrete local executor, validates that the probe's declared capability and produced observation still match the executor contract, exposes deterministic capability discovery, and owns the executor-specific baseline and result policy.

The first two registered executors are:

```text
probe.network.inspect_tcp_integrity_errors
  -> executor.linux.proc_net_snmp.tcp_inerrs

probe.cpu.inspect_utilization
  -> executor.linux.proc_stat.cpu_utilization
```

No generic command runner is introduced.

## Motivation

RFC 0016 deliberately started with one fixed Linux TCP integrity executor. RFC 0017 then exposed that safe execution path through opt-in MCP tools.

That proved the feedback loop, but the implementation still encoded one probe directly in `probe_execution.py`. The next step must allow more read-only executors without turning active diagnostics into arbitrary process execution.

The registry therefore separates three concerns:

```text
canonical probe semantics
        |
        v
executor registration and local availability
        |
        v
probe session lifecycle and runtime evidence
```

The semantic layer remains vendor-independent. The executor layer is implementation policy.

## Registry contract

Each executor registration declares:

```text
executor id
probe id
required capability id
produced observation id
platform
registered local source
baseline metric
classification policy
capture function
result evaluator
```

A probe can execute only when:

1. its ID resolves to canonical `kind: probe` knowledge;
2. canonical risk is exactly `read_only`;
3. a built-in executor is registered for the probe;
4. the canonical probe still declares the executor's capability;
5. the canonical probe still produces the executor's observation.

A mismatch fails closed before execution.

## Capability discovery

The registry projects a read-only machine contract:

```bash
python scripts/probe_execution.py capabilities --pretty
```

The projection uses `schema/probe-execution-capabilities.schema.json` and reports, for every registered executor:

- canonical probe ID, title, and risk;
- executor ID and platform;
- required capability;
- produced observation;
- registered source;
- current local availability;
- explicit executor policy.

Availability is observational only. It checks platform and whether the registered source currently exists and is readable. It does not execute the probe.

The output is deterministic and sorted by probe ID.

## Generalized probe session

`schema/probe-execution-session.schema.json` no longer assumes `Tcp.InErrs` as the only possible baseline.

A session baseline is now:

```json
{
  "metric": "...",
  "values": {
    "...": 123
  }
}
```

The session also pins:

```text
executor id
platform
source
source mode
executor policy
```

`source_mode=registered` means the source must still match the registry when the session is loaded. `source_mode=explicit_override` exists only for direct local replay and tests. MCP execution does not accept a source path from the caller.

The TCP compatibility wrapper temporarily retains the previous `counter` and `value` baseline fields so existing local callers can migrate without changing result semantics.

## TCP integrity executor

The existing executor remains:

```text
executor.linux.proc_net_snmp.tcp_inerrs
```

It reads:

```text
/proc/net/snmp -> Tcp.InErrs
```

Classification remains:

```text
delta > 0  -> observation.network.tcp_integrity_errors = observed
delta == 0 -> observation.network.tcp_integrity_errors = absent
delta < 0  -> fail closed
```

The emitted evidence preserves the raw current value, baseline, delta, source, executor ID, capability, session ID, and semantic scope.

## CPU utilization executor

The second built-in executor is:

```text
executor.linux.proc_stat.cpu_utilization
```

for:

```text
probe.cpu.inspect_utilization
```

It reads the aggregate Linux CPU counters from:

```text
/proc/stat
```

A two-phase session captures cumulative `idle` and `total` CPU counters before and after the externally controlled workload. The evaluator computes:

```text
busy_delta = total_delta - idle_delta
utilization = busy_delta / total_delta
```

Linux `iowait` is included in the idle side of this executor policy. Guest counters are not added again because Linux already accounts guest time inside user and nice counters.

Counter decreases or a non-advancing total fail closed.

### Classification policy

The first built-in CPU executor declares:

```text
observed_threshold_pct = 80.0
```

Therefore:

```text
utilization >= 80% -> observation.cpu.utilization = observed
utilization < 80%  -> observation.cpu.utilization = absent
```

This threshold is explicitly executor policy. It is not a universal Atlerror ontology threshold and must not be interpreted as one.

The measurement stores the actual utilization percentage, the executor threshold as the comparison baseline, and the signed delta from that threshold.

The executor measures aggregate host CPU, so consumers must preserve and interpret scope accordingly. A later executor can implement process- or cgroup-specific CPU semantics without changing the canonical observation ID.

## Runtime evidence remains the feedback boundary

Executors still emit the existing `runtime_evidence` contract with `source.type=probe`.

No executor updates causal rankings directly. The result returns to the same pipeline:

```text
probe execution
  -> runtime evidence
  -> evidence composition
  -> freshness + scope resolution
  -> causal ranking
  -> next-probe ranking
```

This keeps active diagnostics subordinate to the semantic reasoning layer rather than creating a second diagnosis engine.

## MCP behavior

The existing opt-in MCP tools continue to accept a diagnosis target, not an executor or command.

The selected canonical recommendation is passed into the registry-backed probe runtime. The legacy TCP source default is treated only as a TCP compatibility detail; non-TCP registered probes use their own registered source.

The MCP caller still cannot choose:

- an executor ID;
- a local source path;
- a command string;
- a subprocess;
- a non-read-only probe;
- remediation behavior.

## Security boundary

The registry is static Python code shipped with Atlerror.

This RFC does not add dynamic plugin loading, executable configuration, shell interpolation, arbitrary subprocess execution, packet generation, service mutation, or remediation.

Only canonical `risk: read_only` probes can be registered by this runtime path.

Direct CLI `--source-path` exists for local fixtures and replay. Such sessions are explicitly marked `source_mode=explicit_override`; the MCP path never exposes this parameter.

## Determinism

Executor selection is exact by canonical probe ID.

Capability projection ordering is deterministic.

Session IDs are derived from incident, probe, executor, start time, scope, and source. Runtime evidence IDs remain deterministic for a session and produced observation.

CPU utilization uses a pinned session policy so a session cannot silently change interpretation between begin and finish.

## Non-goals

This RFC does not add:

- dynamic third-party executors;
- remote executor discovery;
- non-read-only probe execution;
- automatic workload generation;
- remediation;
- probability or opaque scoring;
- process or cgroup CPU targeting;
- executor scheduling or concurrency control.

## Future work

The next slices can add more fixed read-only executors, expose the capability projection through MCP as a read-only resource, add process/cgroup-aware host collectors, and introduce explicit executor concurrency policy before any broader active-diagnostics surface.

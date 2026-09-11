# RFC 0016: Safe read-only probe execution

Status: Accepted

## Summary

Atlerror can now execute a deliberately narrow class of diagnostic probes instead of only recommending them. The first executor supports:

```text
probe.network.inspect_tcp_integrity_errors
```

on Linux by reading the `Tcp.InErrs` counter from `/proc/net/snmp` before and after a controlled workload window.

The result is emitted as the existing `runtime_evidence` contract and can immediately feed the normal composition, scope, freshness, causal-ranking, and next-probe pipeline.

This RFC does not introduce arbitrary command execution, shell access, remediation, state-changing probes, or an MCP tool.

## Motivation

The read-only demo completed the following loop up to recommendation:

```text
telemetry
  -> runtime evidence
  -> causal diagnosis
  -> recommended next probe
```

The next useful boundary is to prove that a recommended probe can be executed safely and that its result can return to the same evidence model:

```text
telemetry
  -> diagnosis
  -> recommended probe
  -> controlled read-only execution
  -> runtime evidence
  -> revised diagnosis
```

The execution layer must not become a generic remote shell. The semantic probe catalog remains the authority for what a probe means, what it produces, what capability it requires, and how risky it is.

## Safety model

The first execution layer is intentionally restrictive.

A probe is executable only when all of the following are true:

1. the requested ID resolves to a canonical `kind: probe` concept;
2. the canonical probe declares `risk: read_only`;
3. the probe is explicitly registered to a built-in executor;
4. the declared required capability matches the executor capability;
5. the declared produced observation matches the executor output;
6. the executor performs a fixed implementation rather than accepting a user-supplied command.

The executor does not call a shell, create arbitrary subprocesses, mutate network state, send packets, run workloads, or perform remediation.

A `low`, `state_changing`, or `high` risk probe is rejected before executor lookup.

## First executor

The first registered executor is:

```text
executor.linux.proc_net_snmp.tcp_inerrs
```

It implements:

```text
probe.network.inspect_tcp_integrity_errors
```

and provides:

```text
capability.network.inspect_tcp_integrity_errors
```

The executor reads:

```text
/proc/net/snmp
Tcp.InErrs
```

The probe uses a two-phase session.

### Begin

`begin` captures the current `Tcp.InErrs` value and persists a validated `probe_execution_session` document.

```text
counter before controlled workload
  -> baseline session
```

### Workload window

Atlerror does not execute the workload. The operator or an external agent performs the intended controlled workload separately.

This keeps workload generation outside the first execution trust boundary.

### Finish

`finish` reads `Tcp.InErrs` again and compares it with the baseline.

```text
current > baseline -> observation.network.tcp_integrity_errors observed
current = baseline -> observation.network.tcp_integrity_errors absent
current < baseline -> fail closed because the counter may have reset
```

## Session contract

The session schema is:

```text
schema/probe-execution-session.schema.json
```

A session records:

- incident ID;
- canonical probe ID;
- canonical risk;
- required capability;
- produced observation;
- executor ID and platform;
- immutable counter source;
- optional semantic scope;
- start timestamp;
- baseline counter value.

The persisted session is validated again before finishing. A changed or forged semantic mapping is rejected.

## Runtime evidence output

Finishing the session emits an ordinary `runtime_evidence` document.

The evidence instance uses:

```text
source.type = probe
source.name = probe.network.inspect_tcp_integrity_errors
observation = observation.network.tcp_integrity_errors
confidence = moderate
```

The measurement preserves:

```text
value     = final Tcp.InErrs
baseline  = initial Tcp.InErrs
delta     = final - initial
unit      = segments
comparison = changed | equal
```

The session scope is copied unchanged onto the evidence instance. This allows the result to join the same semantic partition as the diagnosis that recommended the probe.

## Why confidence is moderate

Linux `Tcp.InErrs` is useful but is not a perfect checksum-only signal.

It can include TCP input errors broader than checksum failures, and checksum offload can affect what is visible to host counters. Atlerror therefore preserves the measurement and caveat instead of treating the counter as definitive proof.

This is also why execution does not bypass causal ranking. It produces evidence, not a final diagnosis.

## Feedback-loop behavior

The checkout-to-Stripe demo initially sees:

```text
observation.network.tcp_retransmissions
  -> hypothesis.network.packet_loss
  -> alternative hypothesis.network.packet_corruption
  -> next probe probe.network.inspect_tcp_integrity_errors
```

When the probe observes an increase in `Tcp.InErrs`, the emitted evidence enters the same incident and scope. Existing causal ranking then moves:

```text
hypothesis.network.packet_corruption
```

ahead of packet loss for the retransmission target.

The completed probe is no longer recommended because its produced observation is already resolved.

No special feedback-loop ranking rule is added.

## CLI

Begin a session:

```bash
python scripts/probe_execution.py begin \
  --incident-id incident.demo.checkout.stripe \
  --probe probe.network.inspect_tcp_integrity_errors \
  --session /tmp/atlerror-demo/tcp-integrity-probe-session.json \
  --scope-boundary boundary.application.external_dependency \
  --scope-attribute service=checkout-api \
  --scope-attribute dependency=stripe
```

Run the controlled workload outside Atlerror, then finish:

```bash
python scripts/probe_execution.py finish \
  --session /tmp/atlerror-demo/tcp-integrity-probe-session.json \
  --output /tmp/atlerror-demo/tcp-integrity-probe-evidence.json \
  --pretty
```

The output can be composed with existing incident evidence through `runtime_evidence_composition.py`.

## Explicit non-goals

This slice does not add:

- arbitrary shell commands;
- arbitrary subprocess execution;
- user-defined executable probe code;
- packet generation;
- workload execution;
- state-changing probes;
- remediation;
- privilege escalation;
- remote execution;
- MCP tools;
- automatic execution merely because a probe was recommended.

## Future work

The next slices can build on this trust boundary without weakening it:

1. expose explicitly registered read-only executors through an opt-in MCP tool;
2. bind execution requests to the current diagnosis recommendation and semantic scope;
3. append returned probe evidence to a live incident evidence store automatically;
4. recompute diagnosis after successful probe completion;
5. add more built-in read-only executors with per-executor capability and platform checks;
6. design separate approval and policy boundaries before considering any non-read-only action.

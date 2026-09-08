# RFC 0003 - Diagnostic Catalog Codes

Status: Draft

## Purpose

Atlerror uses descriptive canonical semantic IDs such as:

```text
hypothesis.database.deadlock
experiment.database.deadlock.python_postgres
```

Those IDs are authoritative machine identities, but they are intentionally verbose. This RFC defines a shorter navigation namespace for humans and agents:

```text
S4        Transaction failed
D2.2      Database deadlock
D2.2-L1   PostgreSQL two-transaction deadlock lab
```

The short code is a catalog address. It does not replace the canonical ID and it does not itself assert ontology relationships.

## Identity layers

Atlerror deliberately separates four identities:

```text
canonical semantic identity   hypothesis.database.deadlock
human catalog identity        D2.2
experiment design identity    D2.2-L1 / experiment.database.deadlock.python_postgres
experiment run identity       one execution with timestamp, environment, git SHA, and evidence
```

`D2.2-L1` names a reusable laboratory design, not a single CI run. The same lab can execute many times and produce many evidence records.

## Catalog tree versus ontology graph

The catalog is a primary navigation tree. The ontology is a graph.

A mechanism has one short primary catalog location so humans can navigate it quickly, but it may have many semantic relationships and many cross-cutting facets.

For example, `R1.4 Kernel/system CPU work` is primarily catalogued under CPU/resource mechanisms, but may also concern operating-system and filesystem behavior. Its catalog placement MUST NOT be interpreted as an exclusive `is-a` relationship.

Therefore:

1. Short codes are navigation aliases.
2. Canonical semantic IDs are identity.
3. Semantic relations use canonical IDs, never short codes.
4. Cross-cutting truth belongs in ontology relations and facets, not by duplicating or deeply nesting codes.

## Code hierarchy

### Symptoms

Symptoms use `S<number>`:

```text
S1  High CPU
S2  High memory
S3  High latency
S4  Transaction failed
S5  Data invariant violated
```

Symptoms are entry points into the graph rather than fault domains. One symptom can route to mechanisms from several domains.

### Fault / mechanism domains

```text
R  Resource and runtime
D  Database and persistence
N  Network and transport
Q  Queue and messaging
E  External dependencies
A  Application and logic
I  Infrastructure and operating system
F  Filesystem and storage
X  Security and identity
```

The first number selects a family. The second selects a concrete topic:

```text
D2     Locking, transactions, and isolation
D2.1   Lock contention / row lock wait
D2.2   Deadlock
D2.3   Serialization failure
D2.4   Isolation anomaly / write skew

D3     Connections and pooling
D3.1   Application database connection pool exhaustion
```

Mechanism codes intentionally stop at two numeric levels. Do not create catalog addresses such as `D2.3.4.1.2`; detailed structure belongs in canonical semantic IDs and graph relations.

### Labs

A reproducible laboratory design inherits the mechanism code and adds `-L<number>`:

```text
D2.2-L1
D2.2-L2
D3.1-L1
```

Multiple labs may validate the same mechanism in different runtimes, databases, operating systems, or scenarios without changing the mechanism code.

## Stability and evolution

Published short codes are immutable references.

Rules:

1. Canonical semantic IDs remain authoritative.
2. Short codes MUST NOT be targets of semantic relations such as `may_indicate`, `predicts`, or `tested_by`.
3. A published code MUST NOT be silently reused for another meaning.
4. Reclassification does not justify historical renumbering.
5. When a topic moves, keep the old code resolvable through explicit alias/deprecation metadata.
6. Planned topics may reserve a code before their canonical semantic object exists.
7. Numeric order is catalog order, not a claim that the family is exhaustive.

Machine-readable code history and future aliases live in:

```text
vocabulary/diagnostic-code-history.yaml
```

## Facets

Because real faults cross tree boundaries, Atlerror keeps facets separately from the primary catalog location.

Example conceptual profile:

```yaml
code: R1.4
facets:
  domains: [resource_runtime, infrastructure_os, filesystem_storage]
  concerns: [performance]
  resources: [cpu, filesystem]
  layers: [runtime, operating_system]
```

Facets are intentionally many-to-many. They support search, filtering, learning paths, and agent retrieval without making the short-code hierarchy unstable.

Machine-readable profiles live in:

```text
vocabulary/diagnostic-profiles.yaml
```

## Coverage vector

The single labels `planned`, `semantic`, and `empirical` remain useful as quick catalog summaries, but they are too coarse for knowledge-quality tracking.

Each mechanism can therefore expose a multidimensional coverage profile:

```yaml
coverage:
  semantic: complete
  predictions: complete
  falsification: complete
  probes: complete
  rules: partial
  human_explanation: complete
  agent_action: none

empirical:
  synthetic_labs: 1
  runtimes: [python]
  databases: [postgresql]
  production_evidence: none
```

This is a coverage report, not a confidence or truth score. In particular, one successful synthetic lab does not prove universal behavior or production prevalence.

## Wait-state location for latency diagnosis

Several mechanisms can all surface as `S3 High latency` while the excess time lives in different places. The catalog code should preserve the mechanism, while observations and probes identify the actual wait location.

```text
D1.1  synchronous database query delay
      application crosses DB boundary -> database operation itself is slow

D2.1  row lock contention
      application crosses DB boundary -> database session waits on another transaction

D3.1  application database connection pool exhaustion
      application waits for pool checkout -> DB boundary has not yet been crossed

Q1.1  generic queueing delay
      describes the wait shape, not necessarily the concrete resource causing it
```

This distinction is useful for deterministic probe selection. A fast SQL statement does not falsify D3.1 if the request already spent most of its time waiting before it obtained a connection.

## Current empirical map

```text
R1.1-L1  Traffic-driven CPU load
R1.2-L1  Busy loop
R1.3-L1  Garbage collection pressure
R1.4-L1  Kernel/system CPU work
R2.1-L1  Retained live objects

D1.1-L1  Synchronous database query delay
D2.1-L1  PostgreSQL row lock contention
D2.2-L1  PostgreSQL deadlock
D2.3-L1  PostgreSQL serialization failure
D2.4-L1  PostgreSQL Repeatable Read write skew vs Serializable protection
D3.1-L1  Application connection pool exhaustion with fast PostgreSQL query

E1.1-L1  Slow external dependency
```

D2.2 and D2.3 intentionally distinguish PostgreSQL SQLSTATE `40P01` deadlocks from `40001` serialization failures. D2.4 is separate because an isolation anomaly can produce an incorrect committed state while every transaction reports success. D3.1 is separate from both database query latency and PostgreSQL-wide connection exhaustion because the wait can occur entirely inside the application before the database call begins.

## Immediate roadmap

After D3.1, the next connection-family discriminator should separate application-pool exhaustion from database-side connection admission limits:

```text
D3.2     Database connection limit / admission exhaustion
D3.2-L1  PostgreSQL max_connections or reserved-slot admission lab
```

A later network family can then distinguish connection establishment and transport failures from both D3 mechanisms.

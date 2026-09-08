# RFC 0003 - Diagnostic Catalog Codes

Status: Draft

## Purpose

Atlerror canonical semantic IDs are stable machine identities such as:

```text
hypothesis.database.deadlock
experiment.database.deadlock.python_postgres
```

They are intentionally descriptive, but they are not ideal for quickly navigating the size and depth of the diagnostic corpus.

This RFC introduces a second, shorter namespace for human and agent navigation.

Examples:

```text
S4        Transaction failed
D2.2      Database deadlock
D2.2-L1   PostgreSQL two-transaction deadlock lab
```

The short code is an alias for a catalog location. It does not replace the canonical ID.

## Why two identifiers

Canonical IDs answer:

> What semantic object is this exactly?

Catalog codes answer:

> Where is this topic in the diagnostic map, and how much of it have we covered?

The same mechanism can accumulate multiple claims, runtimes, and experiments without changing its short topic code.

## Code hierarchy

### Symptoms

Symptoms use `S<number>`:

```text
S1  High CPU
S2  High memory
S3  High latency
S4  Transaction failed
```

Symptoms are intentionally separated from fault domains because one symptom can be explained by mechanisms from multiple domains.

For example, `S3 High latency` can route to database, external dependency, CPU saturation, queueing, or network topics.

### Fault / mechanism domains

The first letter describes the diagnostic domain:

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

The first number selects a family inside the domain. The second selects a concrete mechanism/topic.

Example:

```text
D2     Locking, transactions, and isolation
D2.1   Lock contention / row lock wait
D2.2   Deadlock
D2.3   Serialization failure
D2.4   Isolation anomaly / write skew
```

### Labs

A reproducible empirical lab inherits the mechanism code and adds `-L<number>`:

```text
D2.2-L1
```

This means the first empirical lab validating `D2.2 Deadlock`.

If later we reproduce the same mechanism in another database or runtime, we can add:

```text
D2.2-L2
D2.2-L3
```

without inventing a new fault code.

## Stability rules

1. Canonical semantic IDs remain authoritative.
2. Short codes MUST NOT be used as the target of semantic relations such as `may_indicate`, `predicts`, or `tested_by`.
3. Published short codes should remain stable.
4. A code should not be silently reused for a different meaning.
5. If taxonomy changes, prefer alias/deprecation metadata over renumbering history.
6. Planned topics may reserve a code before their canonical semantic object exists.

## Coverage states

Each mechanism receives one of three initial coverage states:

```text
planned    catalog slot exists, semantic implementation not yet complete
semantic   canonical ontology exists, but no empirical lab yet
empirical  at least one reproducible lab supports a claim for the mechanism
```

This is deliberately not a truth score. `empirical` means we have reproducible synthetic evidence for at least one scoped claim, not that every manifestation of the mechanism has been proven.

## Current map

The machine-readable source is:

```text
vocabulary/diagnostic-catalog.yaml
```

Current empirical lab codes:

```text
R1.1-L1  Traffic-driven CPU load
R1.2-L1  Busy loop
R1.3-L1  Garbage collection pressure
R1.4-L1  Kernel/system CPU work
R2.1-L1  Retained live objects
D1.1-L1  Synchronous database query delay
D2.1-L1  PostgreSQL row lock contention
D2.2-L1  PostgreSQL deadlock
E1.1-L1  Slow external dependency
```

That gives the repository nine empirical labs mapped into the catalog.

## Immediate roadmap

The next reserved database transaction topics are:

```text
D2.3     Serialization failure
D2.3-L1  First serialization-failure lab

D2.4     Isolation anomaly / write skew
D2.4-L1  First isolation-anomaly lab
```

The intended order is `D2.3` first, because it extends the current `S4 Transaction failed` branch directly after `D2.2 Deadlock` and lets Atlerror distinguish SQLSTATE `40P01` deadlock failures from SQLSTATE `40001` serialization failures.

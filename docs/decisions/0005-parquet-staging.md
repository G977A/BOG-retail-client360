# 0005 — Stage generated data to Parquet, then load Oracle

**Date:** <2026-07-29>
**Status:** accepted

## Context

The generator produces tens of millions of transaction rows. They have to reach two
consumers: Oracle (for the SQL analytics layer) and PySpark (for the feature
pipeline). The generator could write directly into Oracle, or write files that both
consumers read.

Holding the full transaction set in one pandas DataFrame is not viable at target
scale — memory grows linearly with customer count and the run dies on a laptop long
before the dataset is interesting.

## Decision

Generate in **chunks of customers** and write one **Parquet** file per chunk to
`data/parquet/`. Oracle is loaded from those files in a separate ingest step;
PySpark reads the same files directly.

## Alternatives considered

- **Insert straight into Oracle from the generator.** Couples generation to a running
  database, makes every re-run a slow round trip, and leaves no artefact PySpark can
  read without going back through the database.
- **One CSV.** No column types, no compression, no predicate pushdown, and a single
  file that has to be rewritten in full on every run. Parquet is columnar, typed and
  roughly an order of magnitude smaller.
- **Generate everything in memory, write once.** Simplest code, but peak memory
  scales with the dataset and caps the project at a size too small to justify Spark.

## Consequences

- Peak memory stays flat regardless of customer count; scale becomes a config change
  rather than a rewrite.
- One artefact serves both consumers, and re-running a downstream stage does not
  require regenerating data.
- Parquet files are gitignored — they are reproducible from the seed, and the
  repository stays small.
- Written with pandas/pyarrow, which needs no Hadoop `winutils` on Windows; only
  Spark's own file I/O does.
- Adds an explicit ingest step between generation and Oracle. Acceptable: it mirrors
  how a real pipeline separates extraction from loading.
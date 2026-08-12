# 0008 — Exclude `digital_engagement_flag` from the feature set (leakage)

**Date:** <!-- 2026-08-12 -->
**Status:** accepted

## Context

Ground truth is sealed in a separate directory and never joined during modelling
(decision record 0002). That protects against the obvious leak — reading the answer
key — but not against a subtler one: an observable column that happens to *encode*
the answer.

`dim_customer.digital_engagement_flag` is such a column. The generator computes it as
`digital_share > 0.5`, where `digital_share` is a per-persona constant. Every student
receives the same value, every pensioner receives the same value. The flag is a
deterministic function of the hidden persona, sitting in an observable dimension
table with nothing to mark it as ground truth.

It was found by scoring every candidate feature with a one-way ANOVA F-statistic
against the sealed persona labels. All genuine features produced large but finite
values; this one returned infinity — zero variance within each persona group.
Clustering handed that column would have recovered the personas exactly and proved
nothing about the method.

## Decision

Exclude `digital_engagement_flag` from the feature matrix.

Keep `digital_value_share` — the same concept computed from actual transactions
(share of debit value through mobile app, internet bank and e-commerce). It carries
real per-customer variation because it is measured rather than assigned.

Leave the column in `dim_customer`. It is legitimate for BI and reporting; the
restriction is on model input.

## Alternatives considered

- **Regenerate with per-customer noise on the flag.** The most complete fix, and what
  a second pass would do. Rejected for now because the honest version of the feature
  already exists, and regenerating invalidates the loaded warehouse for no analytical
  gain.
- **Move the column into the sealed ground-truth tables.** Wrong classification: a
  real bank does hold a digital-engagement flag on the customer record. The problem is
  this synthetic one is degenerate, not that the concept is secret.
- **Keep it and report the strong clustering result.** Rejected. The result would be
  an artefact of the generator, and any reviewer who checked would find it
  immediately.

## Consequences

- Clustering scores will be lower and meaningful, rather than high and hollow.
- The feature-scoring step becomes a permanent part of the pipeline: every candidate
  feature is checked against the sealed labels **for suspicion, not for selection** —
  the labels are used to detect leakage, never to choose features by predictive power,
  which would itself be a leak.
- General rule adopted: a feature that separates the target perfectly is treated as a
  bug until proven otherwise. In production the same signature — implausibly strong
  single predictor — usually means the field is populated after the outcome it is
  meant to predict.

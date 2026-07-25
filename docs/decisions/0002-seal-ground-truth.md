# 0002 — Seal planted ground truth away from the modelling pipeline

**Date:** <!2026-07-25>
**Status:** accepted

## Context

The data generator plants two hidden facts for every synthetic customer: their true
**persona** (which the segmentation should rediscover) and their true campaign
**uplift** (which the uplift model should estimate). These exist so the project can
*prove* its models work — something impossible with real data, where the true persona
and the causal effect of a campaign are never observable.

That proof only holds if the modelling code cannot see the answer while it works. If
the true persona sits in `dim_customer`, or the true uplift is reachable by a join,
then sooner or later — deliberately or not — a feature, a filter, or a sort will leak
the answer into the model. The validation would then be measuring the leak, not the
model, and every "it works" claim becomes worthless.

## Decision

Keep all planted ground truth in a **separate schema** (`gt_` tables:
`gt_customer_persona`, `gt_customer_uplift`), physically apart from the star schema.

The segmentation and uplift pipelines are **forbidden from joining to `gt_` tables**
while building features, clusters, or models. The `gt_` tables are opened **only** in
the final validation step, to score results that were already produced without them.

## Alternatives considered

- **Ground truth as columns on `dim_customer`.** Simplest to store, but one careless
  `SELECT *` pulls the answer into the feature set. Rejected — too easy to leak.
- **No ground truth; trust the models.** What a real project has to do. Rejected here
  because the entire point is to *demonstrate* correctness, not assert it.

## Consequences

- A little extra plumbing: a second schema, and discipline to keep the pipelines
  blind to it until scoring.
- In exchange, the validation is honest and defensible: cluster-recovery scores
  (e.g. Adjusted Rand Index) and uplift-accuracy comparisons mean what they claim,
  and the demonstration that naive campaign reads are inflated rests on a real,
  untainted reference.
- Good interview answer to "how do you know your segmentation is any good?" — because
  the truth was held out and the model recovered it blind.

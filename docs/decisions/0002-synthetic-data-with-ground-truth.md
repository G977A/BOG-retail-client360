# ADR-0002 — Generate synthetic data with planted ground truth

**Status:** accepted

## Context

Real retail banking data is not obtainable, and public bank datasets (PKDD'99
Berka, UCI Bank Marketing) each cover only part of what this project needs.
Berka has rich transactions but no merchant categories and no campaigns; UCI
Bank Marketing has campaign outcomes but no transaction behaviour.

More importantly: on any real dataset, a segmentation cannot be *validated*.
You produce clusters, you tell a story about them, and no one — including you —
knows whether the story is true.

## Decision

Write a generator that assigns every synthetic customer a hidden **persona**
(driving their spending pattern) and a hidden **true uplift** for each campaign.
Store both in a separate Oracle schema, `RBA_TRUTH`, that the modelling code has
no credentials for.

## Consequences

**Gains**
- Clustering can be scored objectively (ARI/NMI) instead of narrated.
- The bias in naive campaign measurement can be *demonstrated*, not asserted —
  the single most persuasive thing in the project.
- Data volume is a parameter, so the PySpark work is genuinely at scale.

**Costs**
- The generator is real work, and a naive one produces clusters that are
  trivially separable — which proves nothing. It needs overlapping personas,
  noise, and realistic seasonality to be a fair test.
- "You made up the data" is a legitimate critique. The answer is that the
  *method* is what's being demonstrated, and the method is only checkable
  because the data is synthetic.

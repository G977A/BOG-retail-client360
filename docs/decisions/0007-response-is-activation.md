# 0007 — Define campaign response as activation, not card opening

**Date:** <2026-07-29>
**Status:** accepted

## Context

The campaign offers a credit card. "Success" could mean the customer opened a card,
or that they opened it *and* used it. The two differ: around 10% of opened cards in
the generated data are never activated.

The choice is not cosmetic. Whatever the outcome metric is becomes what the targeting
model optimises for, and what the ROI claim rests on.

## Decision

**Response = opened AND activated within the measurement window** (contact date + 60
days), where activated means at least one transaction on the card.

`card_opened` is still recorded, but as an operational metric only. It is deliberately
excluded from the causal outcome: the uplift model is trained on `responded`, and the
revenue proxy is attached only to activated cards.

## Alternatives considered

- **Response = card opened.** Easier to observe and gives a larger, better-looking
  number. Rejected: a dormant card generates no interchange, no revolving interest and
  no fees, so it earns the bank nothing while still costing acquisition. Optimising for
  openings would systematically favour customers who accept offers and never use them.
- **Response = opened, with activation modelled as a separate second-stage outcome.**
  More faithful to how a bank tracks the funnel, but it splits the causal question in
  two and complicates the uplift arithmetic for no analytical gain at this scale.

## Consequences

- The headline take-up rate is lower than an "openings" metric would give. That is the
  point — it is the number that corresponds to revenue.
- `p0` and `tau` govern the composite outcome directly, so non-activation is already
  priced into the propensity and the causal arithmetic stays clean.
- Activation rate among opens (~90%) is reported separately as an operational health
  metric, and is a natural Power BI tile.
- If the business defined success differently, only the outcome column changes; the
  experimental design and uplift method are unaffected.
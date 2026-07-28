# 0004 — Model persona and uplift archetype as separate, correlated traits

**Date:** <2026-07-28>
**Status:** accepted

## Context

Each synthetic customer carries two planted traits: a **persona** (how they bank) and
an **uplift archetype** (how they respond to the campaign — persuadable, sure-thing,
lost-cause, sleeping-dog, giving a true baseline `p0` and true uplift `τ`).

These could be modelled as one trait or two, and the choice determines whether the
segmentation and the uplift model each have real work to do.

## Decision

Model them as **two distinct latent variables that are statistically dependent**,
linked by a per-persona mixing distribution over the four archetypes (the table in
`docs/02-generator-design.md`). Persona shifts the probabilities over archetype
without determining it.

A small behaviour-driven perturbation (`BEHAVIOUR_UPLIFT_COUPLING`) additionally
nudges `τ` by the customer's own engagement, so uplift is partly learnable from
observable features rather than a pure function of the archetype label.

## Alternatives considered

- **Collapse archetype into persona** (one trait; each persona has a fixed response).
  Rejected: it asserts that everyone in a behavioural segment responds identically,
  so segment membership would fully determine targeting and an uplift model would add
  nothing. It also contradicts reality — two customers with near-identical spending
  can respond to the same offer in opposite directions.
- **Draw archetype fully independently of persona** (identical mixing for every
  persona). Rejected: segments would then carry no information about who to target,
  making the clustering decorative rather than actionable.

## Consequences

- Both models earn their place: clustering yields an actionable strategy (prioritise
  persuadable-heavy segments, suppress sleeping-dog-heavy ones), while the uplift
  model still adds value *within* each segment.
- The mixing matrix is an explicit, stated assumption about who resists credit cards.
  It is a judgement, not a measurement, and is called out as such.
- Slightly more generator complexity: archetype is drawn per persona, and `τ` is
  perturbed by behaviour rather than read straight from the archetype range.
- Terminology note: the two traits are *distinct axes*, **not statistically
  independent**. If they were independent, every row of the mixing matrix would be
  identical. An earlier draft of the design doc used "independent" loosely; corrected.

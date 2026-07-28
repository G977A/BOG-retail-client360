# 0003 — Ground personas in Bank of Georgia's real retail segments

**Date:** <2026-07-28>
**Status:** accepted

## Context

The generator needs a set of customer personas for the segmentation to rediscover.
The first draft used generic archetypes (young spender, family, affluent, cash-heavy,
dormant). They were plausible but invented — they described *a* retail bank rather
than *this* one, and nothing about them could be checked against reality.

## Decision

Anchor the persona roster to Bank of Georgia's published retail structure and to
distinctive features of the Georgian market:

| Persona | Anchor |
| --- | --- |
| Student / Young Digital | BoG Student Card segment |
| Mass-Retail Family | Mass Retail / Plus+ loyalty base |
| SOLO Affluent | SOLO premium banking tier |
| Cash Traditionalist | low-digital mass segment |
| Pensioner | pension-account holders |
| Remittance Household | migrant / remittance recipients |

Six personas, deliberately overlapping at the edges (Pensioner and Cash
Traditionalist share age, channel and category profiles).

## Alternatives considered

- **Keep the generic five.** Faster, but unverifiable and reads as templated. Rejected.
- **Derive segments from published market statistics only.** No dataset exists at the
  granularity needed, and it would still require invented behavioural parameters.
- **Fewer, cleanly separated personas.** Would produce a flattering clustering score
  that says more about the generator than the method. Rejected — the overlap is the
  realistic case and gives an honest result to discuss.

## Consequences

- Segment names are defensible in conversation and specific to this bank rather than
  generic — the Pensioner and Remittance personas in particular reflect the local
  market.
- The GEL parameters attached to each persona remain estimates and are the weakest
  assumption in the project; they are isolated in `config.py` for that reason and
  flagged for review.
- If BoG's internal segmentation differs from the public product structure, the
  personas are still structurally reasonable and the method is unaffected — only the
  labels would change.
- Overlapping personas mean clustering will not score near-perfect recovery. That is
  intended, and the shortfall is a finding to explain rather than a defect to hide.

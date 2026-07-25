# ADR-0003 — Customer dimension is SCD Type 1 (for now)

**Status:** accepted, revisit at step 5

## Context

`DIM_CUSTOMER` holds attributes that genuinely change: income band, city,
employment status, digital registration. Type 2 tracking would preserve history.

## Decision

Type 1 (overwrite) for the first iteration.

## Consequences

- Simpler generator, simpler loads, simpler Power BI model.
- **What breaks:** any "what did this customer look like *at the time of the
  campaign*" question is unanswerable. Feature snapshots partly compensate,
  since `FCT_CUSTOMER_FEATURE` is grained by month.
- If the campaign analysis needs point-in-time attributes, this becomes Type 2
  and this ADR gets superseded. Knowing that in advance is the point of writing
  it down.

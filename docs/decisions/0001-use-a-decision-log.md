# 0001 — Keep a decision log

**Date:** <!-- YYYY-MM-DD -->
**Status:** accepted

## Context

Analytics projects are judged on results, but analysts are hired on reasoning.
The output of this project — a dashboard and a number — hides every judgement
call that produced it: why this many clusters, why this control group size, why
this feature set. Those judgements are the actual work.

## Decision

Record one short entry per non-obvious choice, numbered sequentially, in
`docs/decisions/`. An entry is worth writing when a competent person could have
chosen differently.

## Format

Context (what forced a choice) → Decision (what I chose) → Alternatives
considered → Consequences (what this costs me later). Keep each under a page.

## Consequences

Small ongoing cost while working. In exchange, the repository history answers
"why did you do it that way?" without relying on memory, and the log itself
becomes evidence of how I think.

---

<!-- Copy this file as a template. Entries worth writing in this project:
     - choice of star schema grain
     - number of clusters and how k was selected
     - feature scaling strategy
     - control group size and randomisation unit
     - uplift modelling approach
     - what "response" means, exactly
-->

# 0006 — Income bounds spending; cash withdrawals absorb the remainder

**Date:** <!-- YYYY-MM-DD -->
**Status:** accepted

## Context

Transaction volume (`txns_per_month`), ticket size (`avg_txn_amount_gel`) and income
(`monthly_salary_gel`) are configured independently per persona. Nothing forced them
to agree, and they did not: the first generated dataset produced monthly outflow of
1.18x to 1.73x monthly inflow for every persona, so every current-account balance
drained to the overdraft floor within a few months. Balance features were therefore
constant at the floor and carried no information.

Two separate causes:

1. **Cash withdrawals were sized as a fraction of income *per withdrawal*.** A
   customer making four withdrawals a month withdrew several times their income.
2. **Lognormal mean inflation.** Amounts were drawn with the target as the *median*;
   a lognormal's mean sits `exp(sigma^2/2)` above its median, so every ticket ran
   ~23% above the configured average.

## Decision

Make income the binding constraint, and let cash absorb whatever card spend leaves:

- Each persona gets a **`SPEND_RATIO`** — the share of income spent, remainder
  accumulating as balance.
- The budget is computed against **expected** income (`inflow x salary_regularity`),
  not headline income, because regularity is the probability the credit lands in a
  given month.
- **Card purchases** are drawn from the configured count and ticket size.
- **Cash withdrawals take the remainder** of the budget, split across the number of
  withdrawals implied by the persona's ATM share.
- Lognormal draws are divided by `exp(sigma^2/2)` before taking logs, so
  `avg_txn_amount_gel` means the actual mean ticket.
- Where configured card spend alone exceeds the budget, purchases are scaled down and
  the scale factor is **reported** by `spend_calibration()` rather than silently
  applied.

## Alternatives considered

- **Re-tune the config numbers until they happen to balance.** Fragile: any later
  edit silently reintroduces the problem.
- **Constrain spending against the running balance during generation.** The most
  realistic option, but it couples transaction generation to the balance path and
  makes generation sequential, losing the vectorisation the layer depends on.
- **Let balances drift and ignore it.** Rejected — balance level, volatility and
  trend are among the strongest clustering features, and a constant floor destroys
  all three.

## Consequences

- Balances now track each persona's configured base with realistic volatility, so
  balance-derived features carry real signal.
- The cash mechanic is more realistic than what it replaced: a cash-preferring
  customer shows low card spend and high ATM withdrawals because their spending
  happens off the card rails, which is exactly how such a customer looks in a real
  warehouse.
- `spend_calibration()` surfaces config inconsistency instead of hiding it. A scale
  well below 1.0 is a prompt to revisit that persona's numbers, not a silent fix.
- `avg_txn_amount_gel` is now an upper bound on mean card ticket rather than a
  guarantee: for personas whose configured spend exceeds their income, the realised
  average is lower. This is stated in the calibration output.
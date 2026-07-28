"""
Layer 3 of the generator — product holdings and the month-end snapshot.

Produces:

  dim_product           one row per banking product
  dim_date              one row per calendar day in the window
  fact_account_monthly  one row per customer x held product x month-end

Balances for the current account are DERIVED from the transactions written by
layer 2 (inflows minus outflows, with a sweep to savings), not drawn
independently. That matters: it means balance features and transaction
features agree with each other, the way they would in a real warehouse where
both come from the same underlying ledger.

Also decides which customers already hold a credit card. Those customers are
excluded from the campaign population in layer 4 — you do not cross-sell a
product the customer already has.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ------------------------------------------------------------- holding rates
# P(customer holds this product) by persona. Current Account and Debit Card are
# universal (everyone in this synthetic base is an existing banking customer).
PRODUCT_HOLDING_PROB = {
    "Savings Account": {
        "student_young_digital": 0.25, "mass_retail_family": 0.45,
        "solo_affluent": 0.75, "cash_traditionalist": 0.30,
        "pensioner": 0.35, "remittance_household": 0.30,
    },
    "Term Deposit": {
        "student_young_digital": 0.03, "mass_retail_family": 0.15,
        "solo_affluent": 0.45, "cash_traditionalist": 0.12,
        "pensioner": 0.30, "remittance_household": 0.10,
    },
    "Consumer Loan": {
        "student_young_digital": 0.08, "mass_retail_family": 0.30,
        "solo_affluent": 0.20, "cash_traditionalist": 0.25,
        "pensioner": 0.10, "remittance_household": 0.22,
    },
    # PRE-EXISTING credit card holders. These customers are campaign-ineligible.
    "Credit Card": {
        "student_young_digital": 0.05, "mass_retail_family": 0.12,
        "solo_affluent": 0.35, "cash_traditionalist": 0.06,
        "pensioner": 0.03, "remittance_household": 0.07,
    },
}

UNIVERSAL_PRODUCTS = ["Current Account", "Debit Card"]

# Current-account balance dynamics.
SWEEP_RATE = 0.40          # share of surplus above buffer moved to savings
BUFFER_MULTIPLIER = 1.0    # target buffer = this x the customer's base balance
OVERDRAFT_FLOOR = -500.0   # current accounts may dip slightly negative

# Other product balance shapes (multiples of the customer's base balance).
SAVINGS_START_MULT = 0.8
SAVINGS_MONTHLY_RATE = 0.004
TERM_DEPOSIT_MULT = 2.5
LOAN_PRINCIPAL_MULT = 3.0
LOAN_TERM_MONTHS = 36
CREDIT_CARD_UTILISATION = 0.35   # of a limit set from monthly inflow
CREDIT_CARD_LIMIT_MULT = 1.5     # limit = this x monthly inflow

P_PRODUCT_PREDATES_WINDOW = 0.85  # else it opens during the 12-month window


# --------------------------------------------------------------- dimensions
def build_products() -> pd.DataFrame:
    rows = []
    for i, p in enumerate(config.PRODUCTS, start=1):
        rows.append({
            "product_sk": i,
            "product_name": p["name"],
            "product_group": p["group"],
            "is_campaign_target": p["is_target"],
        })
    return pd.DataFrame(rows)


def build_date_dim(start: str, n_months: int = config.N_MONTHS) -> pd.DataFrame:
    """Calendar dimension covering the generation window plus the campaign
    measurement tail. Every time-based measure in SQL and Power BI hangs off
    this, so it is generated once and loaded as a real table rather than
    derived ad hoc."""
    start_ts = pd.Timestamp(start)
    end_ts = (start_ts + pd.DateOffset(months=n_months + 4)) - pd.Timedelta(days=1)
    d = pd.date_range(start_ts, end_ts, freq="D")
    df = pd.DataFrame({"full_date": d})
    df["date_sk"] = df.full_date.dt.year * 10000 + df.full_date.dt.month * 100 + df.full_date.dt.day
    df["year"] = df.full_date.dt.year
    df["quarter"] = df.full_date.dt.quarter
    df["month"] = df.full_date.dt.month
    df["month_name"] = df.full_date.dt.month_name()
    df["day_of_month"] = df.full_date.dt.day
    df["day_of_week"] = df.full_date.dt.dayofweek + 1
    df["day_name"] = df.full_date.dt.day_name()
    df["is_weekend"] = df.full_date.dt.dayofweek >= 5
    df["is_month_end"] = df.full_date.dt.is_month_end
    df["year_month"] = df.full_date.dt.strftime("%Y-%m")
    return df[["date_sk", "full_date", "year", "quarter", "month", "month_name",
               "day_of_month", "day_of_week", "day_name", "is_weekend",
               "is_month_end", "year_month"]]


# ------------------------------------------------------- holdings assignment
def assign_holdings(master: pd.DataFrame, rng) -> pd.DataFrame:
    """One row per customer x held product, with the month it opened.

    open_month_idx < 0 means the product predates the generation window.
    """
    rows = []
    cust = master["customer_sk"].to_numpy()
    persona = master["persona"].to_numpy()
    n = len(master)

    def open_offsets(size):
        pre = rng.random(size) < P_PRODUCT_PREDATES_WINDOW
        off = rng.integers(1, config.N_MONTHS, size=size)   # opens mid-window
        off[pre] = -1
        return off

    for prod in UNIVERSAL_PRODUCTS:
        rows.append(pd.DataFrame({
            "customer_sk": cust, "product_name": prod,
            "open_month_idx": np.full(n, -1),               # always pre-existing
        }))

    for prod, probs in PRODUCT_HOLDING_PROB.items():
        p = np.array([probs[x] for x in persona])
        held = rng.random(n) < p
        if held.any():
            rows.append(pd.DataFrame({
                "customer_sk": cust[held], "product_name": prod,
                "open_month_idx": open_offsets(int(held.sum())),
            }))

    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------- monthly flow aggregate
def aggregate_monthly_flows(parquet_dir: str | Path) -> pd.DataFrame:
    """Read the layer-2 Parquet chunks and aggregate to customer x month.

    Aggregated chunk by chunk so peak memory stays flat: chunks are split by
    customer, so each chunk's aggregate is already final — no cross-chunk
    combination needed.
    """
    parts = sorted(Path(parquet_dir).glob("txns_part_*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no transaction parquet files in {parquet_dir}")

    out = []
    for p in parts:
        df = pd.read_parquet(p, columns=["customer_sk", "txn_date", "direction", "amount_gel"])
        df["year_month"] = df.txn_date.dt.to_period("M")
        g = (df.assign(
                inflow=np.where(df.direction == "credit", df.amount_gel, 0.0),
                outflow=np.where(df.direction == "debit", df.amount_gel, 0.0))
             .groupby(["customer_sk", "year_month"], observed=True)[["inflow", "outflow"]]
             .sum().reset_index())
        out.append(g)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------- the monthly snapshot
def build_account_monthly(master: pd.DataFrame, holdings: pd.DataFrame,
                          flows: pd.DataFrame, products: pd.DataFrame,
                          start_month: str = "2025-01-01",
                          n_months: int = config.N_MONTHS) -> pd.DataFrame:
    """Month-end snapshot per customer x held product."""
    start = pd.Timestamp(start_month)
    months = [start + pd.DateOffset(months=m) for m in range(n_months)]
    month_ends = [m + pd.offsets.MonthEnd(0) for m in months]
    periods = [pd.Period(m, freq="M") for m in months]

    cust = master["customer_sk"].to_numpy()
    pos = {c: i for i, c in enumerate(cust)}
    n = len(cust)
    base_balance = master["balance_base_gel"].to_numpy()
    inflow_m = master["monthly_inflow_gel"].to_numpy()

    # ---- pivot flows into (customer x month) matrices -----------------------
    inflow_mat = np.zeros((n, n_months))
    outflow_mat = np.zeros((n, n_months))
    pidx = {p: i for i, p in enumerate(periods)}
    f = flows[flows.year_month.isin(periods)]
    ri = f.customer_sk.map(pos).to_numpy()
    ci = f.year_month.map(pidx).to_numpy()
    ok = ~pd.isna(ri)
    inflow_mat[ri[ok].astype(int), ci[ok].astype(int)] = f.inflow.to_numpy()[ok]
    outflow_mat[ri[ok].astype(int), ci[ok].astype(int)] = f.outflow.to_numpy()[ok]

    # ---- current account: sequential over months, vectorised over customers -
    holds_savings = set(holdings.loc[holdings.product_name == "Savings Account", "customer_sk"])
    has_sav = np.array([c in holds_savings for c in cust])

    buffer = base_balance * BUFFER_MULTIPLIER
    ca_bal = base_balance.copy()
    sav_bal = np.where(has_sav, base_balance * SAVINGS_START_MULT, 0.0)

    ca_path = np.zeros((n, n_months))
    sav_path = np.zeros((n, n_months))
    for m in range(n_months):
        ca_bal = ca_bal + inflow_mat[:, m] - outflow_mat[:, m]
        surplus = np.maximum(ca_bal - buffer, 0.0)
        sweep = np.where(has_sav, surplus * SWEEP_RATE, 0.0)
        ca_bal = np.maximum(ca_bal - sweep, OVERDRAFT_FLOOR)
        sav_bal = sav_bal * (1 + SAVINGS_MONTHLY_RATE) + sweep
        ca_path[:, m] = ca_bal
        sav_path[:, m] = sav_bal

    # ---- assemble rows per held product ------------------------------------
    prod_sk = dict(zip(products.product_name, products.product_sk))
    rng_local = np.random.default_rng(config.RANDOM_SEED + 7)   # for static balances
    frames = []

    for prod, grp in holdings.groupby("product_name"):
        c = grp.customer_sk.to_numpy()
        r = np.array([pos[x] for x in c])
        open_idx = grp.open_month_idx.to_numpy()
        k = len(c)

        if prod == "Term Deposit":
            static = base_balance[r] * TERM_DEPOSIT_MULT * rng_local.normal(1.0, 0.15, k)
        elif prod == "Consumer Loan":
            principal = base_balance[r] * LOAN_PRINCIPAL_MULT * rng_local.normal(1.0, 0.2, k)
            elapsed = rng_local.integers(0, LOAN_TERM_MONTHS, k)
        elif prod == "Credit Card":
            limit = inflow_m[r] * CREDIT_CARD_LIMIT_MULT
            util = rng_local.normal(CREDIT_CARD_UTILISATION, 0.15, k).clip(0, 0.95)

        for m in range(n_months):
            live = (open_idx < 0) | (open_idx <= m)
            if not live.any():
                continue
            sel = np.flatnonzero(live)

            if prod == "Current Account":
                bal = ca_path[r[sel], m]
            elif prod == "Savings Account":
                bal = sav_path[r[sel], m]
            elif prod == "Term Deposit":
                bal = static[sel]
            elif prod == "Consumer Loan":
                remaining = np.clip(1 - (elapsed[sel] + m) / LOAN_TERM_MONTHS, 0, 1)
                bal = -principal[sel] * remaining          # liability: negative
            elif prod == "Credit Card":
                bal = -limit[sel] * util[sel] * rng_local.normal(1.0, 0.2, len(sel)).clip(0.2, 1.6)
            else:                                          # Debit Card — no balance
                bal = np.zeros(len(sel))

            frames.append(pd.DataFrame({
                "customer_sk": c[sel],
                "product_sk": prod_sk[prod],
                "month_end_date": month_ends[m],
                "balance_gel": np.round(bal, 2),
                "opened_this_month": open_idx[sel] == m,
            }))

    snap = pd.concat(frames, ignore_index=True)
    snap["date_sk"] = (snap.month_end_date.dt.year * 10000
                       + snap.month_end_date.dt.month * 100
                       + snap.month_end_date.dt.day)
    snap["is_held"] = True
    return snap.sort_values(["customer_sk", "month_end_date", "product_sk"]).reset_index(drop=True)


def campaign_eligible(master: pd.DataFrame, holdings: pd.DataFrame) -> pd.DataFrame:
    """Customers WITHOUT a pre-existing credit card. The campaign population."""
    have = set(holdings.loc[holdings.product_name == "Credit Card", "customer_sk"])
    return master[~master.customer_sk.isin(have)].copy()


# ------------------------------------------------------------------ demo
if __name__ == "__main__":
    try:
        from .customers import build_customers
    except ImportError:
        from .customer import build_customers
    from .sampling import make_rng
    from .transactions import generate_transactions

    config.validate_config()
    rng = make_rng(config.RANDOM_SEED)

    master = build_customers(3000, rng)
    info = generate_transactions(master, rng, "/tmp/acc_demo", chunk_size=1500)
    print(f"transactions: {info['rows']:,} rows in {info['files']} files\n")

    products = build_products()
    holdings = assign_holdings(master, rng)
    flows = aggregate_monthly_flows("/tmp/acc_demo")
    snap = build_account_monthly(master, holdings, flows, products)

    print(f"fact_account_monthly: {len(snap):,} rows "
          f"({len(snap)/len(master)/config.N_MONTHS:.2f} products per customer-month)\n")

    print("product penetration by persona:")
    h = holdings.merge(master[["customer_sk", "persona"]], on="customer_sk")
    pen = pd.crosstab(h.persona, h.product_name).div(master.persona.value_counts(), axis=0)
    print(pen[["Savings Account", "Term Deposit", "Consumer Loan", "Credit Card"]].round(3).to_string())

    elig = campaign_eligible(master, holdings)
    print(f"\ncampaign-eligible (no existing credit card): {len(elig):,} of {len(master):,} "
          f"({len(elig)/len(master):.1%})")

    ca = snap[snap.product_sk == 1].merge(master[["customer_sk", "persona"]], on="customer_sk")
    print("\ncurrent-account month-end balance by persona (GEL):")
    print(ca.groupby("persona")["balance_gel"].agg(["mean", "median", "std"]).round(0).to_string())

    print("\nbalance path, one affluent customer (sweep keeps it near buffer):")
    sk = master[master.persona == "solo_affluent"].customer_sk.iloc[0]
    one = snap[(snap.customer_sk == sk)].pivot_table(
        index="month_end_date", columns="product_sk", values="balance_gel")
    print(one.round(0).head(12).to_string())
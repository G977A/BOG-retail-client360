"""
Layer 2 of the generator — transactions.

Takes the customer master (layer 1) and expands each customer into ~12 months
of individual transactions. This is the large table (fact_transaction) and the
sole source of every behavioural feature used downstream: the clustering never
sees a customer's latent parameters, only what can be derived from these rows.

Three event types are produced:

  purchase         — the bulk. Has an MCC category, a merchant and a channel.
  cash_withdrawal  — ATM cash. No merchant, no MCC; round amounts.
  inflow           — monthly salary / pension / remittance credit.

Generation is chunked by customer batch and written to Parquet so it scales
past what fits in memory. Parquet is written with pyarrow (pandas), which does
NOT require winutils on Windows — only Spark reading/writing does.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ---------------------------------------------------------------- shape knobs
# Local to this layer; promote to config.py if they start needing real tuning.

# Relative transaction volume by calendar month. December peaks (holidays),
# summer lifts (travel/leisure), February dips.
MONTH_SEASONALITY = {
    1: 0.88, 2: 0.90, 3: 0.98, 4: 1.00, 5: 1.02, 6: 1.05,
    7: 1.10, 8: 1.12, 9: 1.00, 10: 0.98, 11: 1.00, 12: 1.25,
}

# Typical ticket size per category, as a multiple of the customer's own average.
# A coffee is not a plane ticket: without this, every category has the same
# amount profile and category spend-share features carry no size information.
CATEGORY_AMOUNT_MULTIPLIER = {
    "groceries": 0.9, "dining": 0.7, "ecommerce": 1.1, "entertainment": 0.8,
    "transport": 0.4, "fuel": 1.2, "utilities": 1.0, "healthcare": 1.3,
    "retail": 1.4, "travel": 3.5,
}

# Right-skew of transaction amounts (lognormal sigma). Real spending is
# many-small / few-large, never symmetric around the mean.
AMOUNT_LOGNORMAL_SIGMA = 0.65
# A lognormal's mean sits exp(sigma^2/2) above its median. Divide the target
# by this before taking logs so avg_txn_amount_gel means the actual MEAN
# ticket, not the median — otherwise every amount runs ~23% hot.
_AMOUNT_MEAN_FACTOR = float(np.exp(AMOUNT_LOGNORMAL_SIGMA ** 2 / 2))

# Which channels a category is plausibly bought through. Blended with the
# customer's own persona channel preference, so a digital persona buying
# groceries still mostly uses POS — just more e-commerce than a traditionalist.
PURCHASE_CHANNELS = ["pos", "ecommerce", "mobile_app", "internet_bank", "branch"]
CATEGORY_CHANNEL_AFFINITY = {
    "groceries":     {"pos": 0.88, "ecommerce": 0.08, "mobile_app": 0.02, "internet_bank": 0.01, "branch": 0.01},
    "dining":        {"pos": 0.90, "ecommerce": 0.07, "mobile_app": 0.02, "internet_bank": 0.005, "branch": 0.005},
    "ecommerce":     {"pos": 0.02, "ecommerce": 0.84, "mobile_app": 0.13, "internet_bank": 0.01, "branch": 0.00},
    "entertainment": {"pos": 0.55, "ecommerce": 0.34, "mobile_app": 0.08, "internet_bank": 0.02, "branch": 0.01},
    "transport":     {"pos": 0.60, "ecommerce": 0.03, "mobile_app": 0.35, "internet_bank": 0.01, "branch": 0.01},
    "fuel":          {"pos": 0.95, "ecommerce": 0.01, "mobile_app": 0.03, "internet_bank": 0.005, "branch": 0.005},
    "utilities":     {"pos": 0.10, "ecommerce": 0.02, "mobile_app": 0.45, "internet_bank": 0.30, "branch": 0.13},
    "healthcare":    {"pos": 0.80, "ecommerce": 0.10, "mobile_app": 0.05, "internet_bank": 0.02, "branch": 0.03},
    "retail":        {"pos": 0.70, "ecommerce": 0.25, "mobile_app": 0.04, "internet_bank": 0.005, "branch": 0.005},
    "travel":        {"pos": 0.20, "ecommerce": 0.55, "mobile_app": 0.20, "internet_bank": 0.04, "branch": 0.01},
}

# Share of purchases that cluster in the days just after the inflow lands.
PAYDAY_CLUSTER_SHARE = 0.35
PAYDAY_DECAY = 0.35              # geometric decay away from payday

# Share of monthly inflow that gets spent; the remainder accumulates as
# balance. Spending is bounded by income — without this, generated outflow
# exceeds inflow and every balance drains to the overdraft floor.
SPEND_RATIO = {
    "student_young_digital": 0.92,
    "mass_retail_family": 0.88,
    "solo_affluent": 0.72,
    "cash_traditionalist": 0.88,
    "pensioner": 0.90,
    "remittance_household": 0.90,
}

# Cash withdrawals absorb whatever the spending budget leaves after card
# purchases: a cash-preferring customer has low card spend and high ATM
# withdrawals because their spending happens off the card rails.
CASH_WITHDRAWAL_SIGMA = 0.45
CASH_ROUNDING_GEL = 10
CASH_MIN_GEL = 20.0

# Merchants per category, and a representative real MCC code for each.
N_MERCHANTS_PER_CATEGORY = 40
MCC_CODES = {
    "groceries": 5411, "dining": 5812, "ecommerce": 5399, "entertainment": 7832,
    "transport": 4111, "fuel": 5541, "utilities": 4900, "healthcare": 8011,
    "retail": 5651, "travel": 4722,
}


# ------------------------------------------------------------- dim_merchant
def build_merchants() -> pd.DataFrame:
    """One row per merchant. merchant_sk 0 is a 'Not Applicable' member used by
    cash withdrawals and inflows — a real star schema uses an N/A dimension row
    rather than a NULL foreign key, so every fact join stays an inner join."""
    rows = [{"merchant_sk": 0, "merchant_name": "Not Applicable",
             "mcc_code": 0, "mcc_category": "n/a"}]
    sk = 1
    for cat in config.MCC_CATEGORIES:
        label = cat.replace("_", " ").title()
        for i in range(1, N_MERCHANTS_PER_CATEGORY + 1):
            rows.append({
                "merchant_sk": sk,
                "merchant_name": f"{label} Merchant {i:02d}",
                "mcc_code": MCC_CODES[cat],
                "mcc_category": cat,
            })
            sk += 1
    return pd.DataFrame(rows)


def _category_offsets() -> dict[str, int]:
    """First merchant_sk for each category, so a merchant can be picked with
    arithmetic instead of a lookup."""
    offsets, sk = {}, 1
    for cat in config.MCC_CATEGORIES:
        offsets[cat] = sk
        sk += N_MERCHANTS_PER_CATEGORY
    return offsets


# ----------------------------------------------------- probability tensors
def _build_prob_tensors():
    """Pre-compute cumulative probability tables once per run.

    cat_cum      (n_personas, n_categories)              — spend mix per persona
    chan_cum     (n_personas, n_categories, n_channels)  — channel given persona+category
    atm_share    (n_personas,)                           — cash propensity

    Cumulative form lets a whole column of draws be resolved with one comparison
    against a uniform sample, with no per-row Python.
    """
    personas = list(config.PERSONAS.keys())
    cats = config.MCC_CATEGORIES

    cat_p = np.array([[config.PERSONAS[p]["spend_shares"][c] for c in cats] for p in personas])
    cat_cum = np.cumsum(cat_p, axis=1)

    chan = np.zeros((len(personas), len(cats), len(PURCHASE_CHANNELS)))
    for pi, p in enumerate(personas):
        pref = config.PERSONAS[p]["channel_shares"]
        # renormalise persona preference over purchase channels (ATM excluded —
        # cash is generated as its own event type, not as a purchase channel)
        base = np.array([pref[ch] for ch in PURCHASE_CHANNELS])
        base = base / base.sum()
        for ci, c in enumerate(cats):
            aff = np.array([CATEGORY_CHANNEL_AFFINITY[c][ch] for ch in PURCHASE_CHANNELS])
            w = base * aff + 1e-9          # epsilon guards an all-zero row
            chan[pi, ci] = w / w.sum()
    chan_cum = np.cumsum(chan, axis=2)

    atm_share = np.array([config.PERSONAS[p]["channel_shares"]["atm"] for p in personas])

    # E[category multiplier] per persona — needed to predict a customer's
    # monthly card spend before any transaction is drawn.
    exp_mult = np.array([
        sum(config.PERSONAS[p]["spend_shares"][c] * CATEGORY_AMOUNT_MULTIPLIER[c] for c in cats)
        for p in personas
    ])
    return cat_cum, chan_cum, atm_share, exp_mult, personas


def _draw_from_cum(rng, cum_rows: np.ndarray) -> np.ndarray:
    """Vectorised categorical draw. cum_rows is (n_rows, n_options) of
    cumulative probabilities; returns the chosen option index per row."""
    u = rng.random(len(cum_rows))[:, None]
    return (u > cum_rows).sum(axis=1).clip(0, cum_rows.shape[1] - 1)


# ------------------------------------------------------------- core builder
def build_transactions_chunk(master_chunk: pd.DataFrame, rng,
                             start_month: pd.Timestamp,
                             n_months: int = config.N_MONTHS) -> pd.DataFrame:
    """Generate all transactions for one batch of customers."""
    cat_cum, chan_cum, atm_share, exp_mult, personas = _build_prob_tensors()
    persona_pos = {p: i for i, p in enumerate(personas)}
    cats = np.array(config.MCC_CATEGORIES, dtype=object)
    offsets = _category_offsets()
    cat_offset = np.array([offsets[c] for c in config.MCC_CATEGORIES])
    cat_mult = np.array([CATEGORY_AMOUNT_MULTIPLIER[c] for c in config.MCC_CATEGORIES])

    n_cust = len(master_chunk)
    p_idx = master_chunk["persona"].map(persona_pos).to_numpy()
    cust_sk = master_chunk["customer_sk"].to_numpy()
    tpm = master_chunk["txns_per_month"].to_numpy()
    avg_amt = master_chunk["avg_txn_amount_gel"].to_numpy()
    inflow = master_chunk["monthly_inflow_gel"].to_numpy()
    regularity = master_chunk["salary_regularity"].to_numpy()

    payday = rng.integers(1, 11, size=n_cust)          # each customer's usual inflow day

    # ---------------------------- budget calibration (income bounds spending)
    # Predict each customer's monthly card spend from config, compare it with
    # what their income allows, and split the remainder into cash. If configured
    # card spend alone exceeds the budget, purchases are scaled down and the
    # shortfall is reported — that is a signal the config numbers for that
    # persona are internally inconsistent, not something to hide.
    spend_ratio = np.array([SPEND_RATIO[p] for p in personas])[p_idx]
    # Expected income, not headline income: salary_regularity is the probability
    # the credit lands in a given month, so a customer with irregular income
    # (student, remittance household) must budget against the lower average or
    # they overspend every month and drain to the overdraft floor.
    budget = inflow * regularity * spend_ratio
    n_purch_exp = tpm * (1.0 - atm_share[p_idx])
    n_cash_exp = np.maximum(tpm * atm_share[p_idx], 1e-9)
    card_spend_exp = n_purch_exp * avg_amt * exp_mult[p_idx]

    purchase_scale = np.minimum(1.0, budget / np.maximum(card_spend_exp, 1e-9))
    cash_budget = np.maximum(budget - card_spend_exp * purchase_scale, 0.0)
    cash_per_withdrawal = cash_budget / n_cash_exp

    months = [start_month + pd.DateOffset(months=m) for m in range(n_months)]
    frames = []

    for m_i, month_start in enumerate(months):
        dim = month_start.days_in_month
        season = MONTH_SEASONALITY[month_start.month]

        # split the month's activity into purchases and cash withdrawals
        expected = tpm * season
        n_cash = rng.poisson(expected * atm_share[p_idx])
        n_buy = rng.poisson(expected * (1.0 - atm_share[p_idx]))

        # ---------------------------------------------------------- purchases
        if n_buy.sum() > 0:
            rep = np.repeat(np.arange(n_cust), n_buy)
            n = len(rep)
            rp = p_idx[rep]

            ci = _draw_from_cum(rng, cat_cum[rp])
            chi = _draw_from_cum(rng, chan_cum[rp, ci])

            target_mean = avg_amt[rep] * cat_mult[ci] * purchase_scale[rep]
            amount = rng.lognormal(
                np.log(np.maximum(target_mean, 0.5) / _AMOUNT_MEAN_FACTOR),
                AMOUNT_LOGNORMAL_SIGMA)

            day = rng.integers(1, dim + 1, size=n)
            boost = rng.random(n) < PAYDAY_CLUSTER_SHARE
            if boost.any():
                off = rng.geometric(PAYDAY_DECAY, size=int(boost.sum())) - 1
                day[boost] = np.clip(payday[rep][boost] + off, 1, dim)

            merchant = cat_offset[ci] + rng.integers(0, N_MERCHANTS_PER_CATEGORY, size=n)

            frames.append(pd.DataFrame({
                "customer_sk": cust_sk[rep],
                "txn_date": month_start + pd.to_timedelta(day - 1, unit="D"),
                "txn_type": "purchase",
                "direction": "debit",
                "amount_gel": np.round(amount, 2),
                "mcc_category": cats[ci],
                "channel": np.array(PURCHASE_CHANNELS, dtype=object)[chi],
                "merchant_sk": merchant,
            }))

        # --------------------------------------------------- cash withdrawals
        if n_cash.sum() > 0:
            rep = np.repeat(np.arange(n_cust), n_cash)
            n = len(rep)
            base = np.maximum(cash_per_withdrawal[rep], CASH_MIN_GEL)
            amt = rng.lognormal(
                np.log(base / float(np.exp(CASH_WITHDRAWAL_SIGMA ** 2 / 2))),
                CASH_WITHDRAWAL_SIGMA)
            amt = np.maximum(np.round(amt / CASH_ROUNDING_GEL) * CASH_ROUNDING_GEL, CASH_MIN_GEL)

            day = rng.integers(1, dim + 1, size=n)
            boost = rng.random(n) < PAYDAY_CLUSTER_SHARE
            if boost.any():
                off = rng.geometric(PAYDAY_DECAY, size=int(boost.sum())) - 1
                day[boost] = np.clip(payday[rep][boost] + off, 1, dim)

            frames.append(pd.DataFrame({
                "customer_sk": cust_sk[rep],
                "txn_date": month_start + pd.to_timedelta(day - 1, unit="D"),
                "txn_type": "cash_withdrawal",
                "direction": "debit",
                "amount_gel": np.round(amt, 2),
                "mcc_category": "n/a",
                "channel": "atm",
                "merchant_sk": 0,
            }))

        # ------------------------------------------------------------ inflows
        # Regularity controls both whether the credit lands at all and how
        # tightly it sticks to the customer's usual payday.
        lands = rng.random(n_cust) < regularity
        if lands.any():
            idx = np.flatnonzero(lands)
            n = len(idx)
            jitter = np.round(rng.normal(0, (1.0 - regularity[idx]) * 8.0)).astype(int)
            day = np.clip(payday[idx] + jitter, 1, dim)
            amt = inflow[idx] * rng.normal(1.0, 0.06, size=n)
            frames.append(pd.DataFrame({
                "customer_sk": cust_sk[idx],
                "txn_date": month_start + pd.to_timedelta(day - 1, unit="D"),
                "txn_type": "inflow",
                "direction": "credit",
                "amount_gel": np.round(np.maximum(amt, 0.0), 2),
                "mcc_category": "n/a",
                "channel": "transfer",
                "merchant_sk": 0,
            }))

    txns = pd.concat(frames, ignore_index=True)
    # smart date key (YYYYMMDD) — the standard join key to dim_date
    txns["date_sk"] = (txns["txn_date"].dt.year * 10000
                       + txns["txn_date"].dt.month * 100
                       + txns["txn_date"].dt.day)
    return txns.sort_values(["customer_sk", "txn_date"]).reset_index(drop=True)


def generate_transactions(master: pd.DataFrame, rng, out_dir: str | Path,
                          start_month: str = "2025-01-01",
                          chunk_size: int = 2000) -> dict:
    """Generate transactions for all customers, one Parquet file per chunk.

    Chunking keeps peak memory flat regardless of customer count — the whole
    point of not building a 30M-row DataFrame in one go.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("txns_part_*.parquet"):
        old.unlink()

    start = pd.Timestamp(start_month)
    total_rows = 0
    for i, lo in enumerate(range(0, len(master), chunk_size)):
        chunk = master.iloc[lo: lo + chunk_size]
        txns = build_transactions_chunk(chunk, rng, start)
        txns.to_parquet(out / f"txns_part_{i:04d}.parquet", index=False)
        total_rows += len(txns)

    return {"rows": total_rows, "files": len(list(out.glob("txns_part_*.parquet"))),
            "path": str(out)}


def spend_calibration(master: pd.DataFrame) -> pd.DataFrame:
    """Per-persona view of configured card spend vs what income allows.

    scale < 1 means the persona's configured txns_per_month x
    avg_txn_amount_gel implies more card spend than SPEND_RATIO of their income
    permits, so purchases are scaled down. Treat a scale well below 1 as a
    prompt to revisit that persona's numbers in config.py.
    """
    _, _, atm_share, exp_mult, personas = _build_prob_tensors()
    pos = {p: i for i, p in enumerate(personas)}
    pi = master["persona"].map(pos).to_numpy()

    tpm = master["txns_per_month"].to_numpy()
    avg_amt = master["avg_txn_amount_gel"].to_numpy()
    inflow = master["monthly_inflow_gel"].to_numpy()

    regularity = master["salary_regularity"].to_numpy()
    budget = inflow * regularity * np.array([SPEND_RATIO[p] for p in personas])[pi]
    card = tpm * (1 - atm_share[pi]) * avg_amt * exp_mult[pi]
    scale = np.minimum(1.0, budget / np.maximum(card, 1e-9))
    cash = np.maximum(budget - card * scale, 0.0)

    return (pd.DataFrame({
        "persona": master["persona"].to_numpy(),
        "inflow": inflow, "exp_income": inflow * regularity, "budget": budget,
        "card_spend": card * scale, "cash_spend": cash, "scale": scale,
    }).groupby("persona").mean().round(2))


# ------------------------------------------------------------------ demo
if __name__ == "__main__":
    # module name differs if you saved layer 1 as customer.py rather than customers.py
    try:
        from .customers import build_customers
    except ImportError:
        from .customers import build_customers
    from .sampling import make_rng

    config.validate_config()
    rng = make_rng(config.RANDOM_SEED)
    master = build_customers(3000, rng)

    txns = build_transactions_chunk(master, rng, pd.Timestamp("2025-01-01"))
    print(f"\n{len(txns):,} transactions for {len(master):,} customers "
          f"({len(txns)/len(master):.0f} per customer over {config.N_MONTHS} months)\n")

    print("spend calibration (card + cash ~= budget; scale 1.0 = config fits income):")
    print(spend_calibration(master).to_string(), "\n")

    print("event mix:")
    print(txns["txn_type"].value_counts(normalize=True).round(3).to_string(), "\n")

    print("monthly volume (seasonality should show Dec peak, Feb dip):")
    mv = txns.groupby(txns["txn_date"].dt.month).size()
    for m, v in mv.items():
        print(f"  {m:2d}  {v:7,d}  {'█' * int(v / mv.max() * 40)}")

    print("\nspend share by persona — generated vs config (purchases only):")
    pur = txns[txns.txn_type == "purchase"].merge(
        master[["customer_sk", "persona"]], on="customer_sk")
    for p in ["student_young_digital", "pensioner"]:
        sub = pur[pur.persona == p]
        got = sub["mcc_category"].value_counts(normalize=True)
        want = config.PERSONAS[p]["spend_shares"]
        print(f"  {p}:")
        for c in sorted(want, key=want.get, reverse=True)[:4]:
            print(f"    {c:14s} generated {got.get(c, 0):.3f}   config {want[c]:.3f}")

    print("\nchannel mix by persona (purchases):")
    print(pd.crosstab(pur["persona"], pur["channel"], normalize="index").round(3).to_string())

    print("\namount distribution (GEL) — right-skewed by design:")
    a = txns.loc[txns.txn_type == "purchase", "amount_gel"]
    print(f"  median {a.median():.1f}   mean {a.mean():.1f}   p95 {a.quantile(.95):.1f}   max {a.max():.1f}")

    print("\navg ticket by category (travel should dominate):")
    print(pur.groupby("mcc_category")["amount_gel"].mean().round(1).sort_values(ascending=False).to_string())
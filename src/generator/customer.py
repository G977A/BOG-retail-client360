"""
Layer 1 of the generator — the customer master.

Builds one row per synthetic customer carrying three kinds of field:

  * OBSERVABLE   — what a bank would actually know (age, city, tenure, ...).
                   Projected to dim_customer.
  * LATENT       — behavioural knobs (txns/month, avg amount, inflow, ...) that
                   drive the transaction/balance layers. Not written to
                   dim_customer; a bank wouldn't store a customer's "true rate".
  * SEALED TRUTH — the planted persona, and the uplift archetype with true p0
                   and tau. Projected to gt_customer_persona / gt_customer_uplift
                   and never joined during modelling (decision record 0002).

Everything is drawn vectorised (whole columns at once) via persona-indexed
parameter arrays — fast enough to scale to hundreds of thousands of customers.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import config
from .sampling import make_rng, truncated_normal, weighted_choice, standardize

# ---- geography & occupation: local for now; promote to config if it grows ----
CITIES = ["Tbilisi", "Batumi", "Kutaisi", "Rustavi", "Gori", "Zugdidi", "Other"]
CITY_WEIGHTS = [0.48, 0.10, 0.08, 0.06, 0.03, 0.03, 0.22]

EMPLOYMENT_BY_PERSONA = {
    "student_young_digital": "student",
    "mass_retail_family": "employed",
    "solo_affluent": "employed",
    "cash_traditionalist": "employed",
    "pensioner": "retired",
    "remittance_household": "self_employed",
}

# mean of extra products held beyond the baseline current account + debit card
EXTRA_PRODUCTS_LAMBDA = {
    "student_young_digital": 0.6,
    "mass_retail_family": 1.6,
    "solo_affluent": 2.4,
    "cash_traditionalist": 1.0,
    "pensioner": 0.8,
    "remittance_household": 1.0,
}

TAU_COUPLING_SCALE = 0.05   # max size of the behaviour nudge on tau (GEL-free, prob units)


def build_customers(n_customers: int, rng, reference_date: date = date(2025, 12, 31)) -> pd.DataFrame:
    """Return the customer master DataFrame (observable + latent + sealed truth)."""
    persona_names = list(config.PERSONAS.keys())
    pop_weights = [config.PERSONAS[p]["population_share"] for p in persona_names]
    persona, persona_idx = weighted_choice(rng, persona_names, pop_weights, n_customers)

    # --- helpers that turn a per-persona parameter into a per-customer array ---
    def gauss_param(key):
        means = np.array([config.PERSONAS[p][key][0] for p in persona_names])
        stds = np.array([config.PERSONAS[p][key][1] for p in persona_names])
        return means[persona_idx], stds[persona_idx]

    def range_param(key):
        los = np.array([config.PERSONAS[p][key][0] for p in persona_names])
        his = np.array([config.PERSONAS[p][key][1] for p in persona_names])
        return los[persona_idx], his[persona_idx]

    def scalar_param(mapping):
        return np.array([mapping[p] for p in persona_names])[persona_idx]

    # ---------------------------------------------------- observable demographics
    age_lo, age_hi = range_param("age_range")
    age = rng.integers(age_lo, age_hi + 1)

    ten_lo, ten_hi = range_param("tenure_years_range")
    tenure_years = rng.integers(ten_lo, ten_hi + 1)
    start_offset_days = (tenure_years * 365) + rng.integers(0, 365, size=n_customers)
    relationship_start = pd.to_datetime(reference_date) - pd.to_timedelta(start_offset_days, unit="D")

    gender = np.where(rng.random(n_customers) < 0.5, "F", "M")
    city, _ = weighted_choice(rng, CITIES, CITY_WEIGHTS, n_customers)
    income_band = scalar_param({p: config.PERSONAS[p]["income_band"] for p in persona_names})
    employment = scalar_param(EMPLOYMENT_BY_PERSONA)
    extra_products = rng.poisson(scalar_param(EXTRA_PRODUCTS_LAMBDA))
    existing_product_count = 2 + extra_products   # everyone holds current account + debit

    # digital-engagement flag derived from the persona's channel mix
    def digital_share_of(p):
        cs = config.PERSONAS[p]["channel_shares"]
        return cs["mobile_app"] + cs["internet_bank"] + cs["ecommerce"]
    digital_share = np.array([digital_share_of(p) for p in persona_names])[persona_idx]
    digital_engagement_flag = digital_share > 0.5

    # --------------------------------------------------------- latent behaviour
    tpm_m, tpm_s = gauss_param("txns_per_month_gaussian")
    txns_per_month = truncated_normal(rng, tpm_m, tpm_s, low=1.0)

    amt_m, amt_s = gauss_param("avg_txn_amount_gel_gaussian")
    avg_txn_amount_gel = truncated_normal(rng, amt_m, amt_s, low=2.0)

    inc_m, inc_s = gauss_param("monthly_salary_gel_gaussian")
    monthly_inflow_gel = truncated_normal(rng, inc_m, inc_s, low=0.0)

    bal_m, bal_s = gauss_param("balance_gel_gaussian")
    balance_base_gel = truncated_normal(rng, bal_m, bal_s, low=0.0)

    salary_regularity = scalar_param({p: config.PERSONAS[p]["salary_regularity"] for p in persona_names})

    # ---------------------------------------------- sealed truth: uplift archetype
    arche_names = list(config.ARCHETYPES.keys())
    archetype = np.empty(n_customers, dtype=object)
    for pi, p in enumerate(persona_names):
        mask = persona_idx == pi
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        mix = config.PERSONAS[p]["archetype_mix"]
        w = [mix[a] for a in arche_names]
        picks = rng.choice(len(arche_names), p=w, size=cnt)
        archetype[mask] = np.asarray(arche_names, dtype=object)[picks]

    # true baseline p0 and true uplift tau, drawn within each archetype's range
    p0 = np.empty(n_customers)
    tau = np.empty(n_customers)
    for a in arche_names:
        mask = archetype == a
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        p0_lo, p0_hi = config.ARCHETYPES[a]["p0_range"]
        t_lo, t_hi = config.ARCHETYPES[a]["tau_range"]
        p0[mask] = rng.uniform(p0_lo, p0_hi, cnt)
        tau[mask] = rng.uniform(t_lo, t_hi, cnt)

    # behaviour -> tau nudge (the dotted "noise" edge): makes uplift partly
    # learnable from features without turning it into a function of persona.
    engagement = (
        standardize(np.log1p(txns_per_month))
        + standardize(digital_share)
        + standardize(np.log1p(monthly_inflow_gel))
    ) / 3.0
    tau = tau + config.BEHAVIOUR_UPLIFT_COUPLING * engagement * TAU_COUPLING_SCALE

    p1 = np.clip(p0 + tau, 0.0, 1.0)   # treated take-up probability

    master = pd.DataFrame({
        "customer_sk": np.arange(1, n_customers + 1),
        # observable
        "age": age,
        "gender": gender,
        "city": city,
        "income_band": income_band,
        "employment": employment,
        "tenure_years": tenure_years,
        "relationship_start_date": relationship_start,
        "existing_product_count": existing_product_count,
        "digital_engagement_flag": digital_engagement_flag,
        # latent behaviour (consumed by later layers, not written to dim_customer)
        "txns_per_month": txns_per_month,
        "avg_txn_amount_gel": avg_txn_amount_gel,
        "monthly_inflow_gel": monthly_inflow_gel,
        "balance_base_gel": balance_base_gel,
        "salary_regularity": salary_regularity,
        "digital_share": digital_share,
        # sealed truth
        "persona": persona,
        "archetype": archetype,
        "true_p0": p0,
        "true_tau": tau,
        "true_p1": p1,
    })
    return master


# ------------------------------------------------------------------ projections
OBSERVABLE_COLS = [
    "customer_sk", "age", "gender", "city", "income_band", "employment",
    "tenure_years", "relationship_start_date", "existing_product_count",
    "digital_engagement_flag",
]


def to_dim_customer(master: pd.DataFrame) -> pd.DataFrame:
    return master[OBSERVABLE_COLS].copy()


def to_gt_persona(master: pd.DataFrame) -> pd.DataFrame:
    return master[["customer_sk", "persona"]].copy()


def to_gt_uplift(master: pd.DataFrame) -> pd.DataFrame:
    return master[["customer_sk", "archetype", "true_p0", "true_tau", "true_p1"]].copy()


# ------------------------------------------------------------------ demo / check
if __name__ == "__main__":
    config.validate_config()
    rng = make_rng(config.RANDOM_SEED)
    N = 5000
    master = build_customers(N, rng)

    print(f"\ngenerated {len(master):,} customers\n")

    print("persona distribution (actual vs config):")
    for p in config.PERSONAS:
        actual = (master["persona"] == p).mean()
        target = config.PERSONAS[p]["population_share"]
        print(f"  {p:24s} {actual:6.3f}  (config {target:.3f})")

    print("\narchetype distribution and true-effect ranges:")
    for a in config.ARCHETYPES:
        m = master["archetype"] == a
        print(f"  {a:14s} n={m.sum():5d}  share={m.mean():.3f}  "
              f"p0=[{master.loc[m,'true_p0'].min():.3f},{master.loc[m,'true_p0'].max():.3f}]  "
              f"tau=[{master.loc[m,'true_tau'].min():+.3f},{master.loc[m,'true_tau'].max():+.3f}]")

    print("\ndim_customer sample:")
    print(to_dim_customer(master).head(8).to_string(index=False))

    print("\naverage true uplift by persona (sanity — should track archetype mix):")
    print(master.groupby("persona")["true_tau"].mean().round(4).to_string())
"""
Layer 4 of the generator — the campaign experiment.

Produces:

  dim_campaign            one row per campaign
  fact_campaign_response  one row per eligible customer x campaign

This is where the sealed ground truth turns into observable outcomes:

  control   customer takes the card with probability p0        (never contacted)
  treatment customer takes the card with probability p0 + tau  (contacted)

Assignment is a randomised holdout, so the two arms are statistically identical
apart from contact. The difference in their take-up rates is therefore an
unbiased estimate of average uplift — the benchmark the stage-5 model must beat
on targeting, and the number a naive "count everyone who responded" read gets
badly wrong.

Eligibility: customers who already hold a credit card are excluded. You do not
cross-sell a product the customer already has.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .accounts import campaign_eligible

# ------------------------------------------------------------------- knobs
CAMPAIGN_NAME = "Credit Card Cross-Sell Q1"
CONTACT_CHANNEL = "mobile_app"        # push/in-app offer
CONTACT_WAVE_DAYS = 10                # contacts are spread over this many days

# Response timing: most take-up lands soon after contact, tailing off.
RESPONSE_DECAY = 0.08                 # geometric p; mean ~12 days

# Some customers open a card but never activate it. Dormant cards earn nothing,
# so they do NOT count as a response — this is an operational metric only, and
# is deliberately kept out of the causal outcome.
P_DORMANT_OPEN_TREATMENT = 0.04
P_DORMANT_OPEN_CONTROL = 0.015

UNKNOWN_DATE_SK = 0                   # star-schema "Unknown" date member


def build_campaign_dim(campaign_start: str, products: pd.DataFrame) -> pd.DataFrame:
    start = pd.Timestamp(campaign_start)
    target_sk = int(products.loc[products.is_campaign_target, "product_sk"].iloc[0])
    return pd.DataFrame([{
        "campaign_sk": 1,
        "campaign_name": CAMPAIGN_NAME,
        "target_product_sk": target_sk,
        "start_date": start,
        "end_date": start + pd.Timedelta(days=CONTACT_WAVE_DAYS),
        "measurement_end_date": start + pd.Timedelta(days=config.ACTIVATION_WINDOW_DAYS
                                                     + CONTACT_WAVE_DAYS),
        "contact_channel": CONTACT_CHANNEL,
        "cost_per_contact_gel": config.COST_PER_CONTACT_GEL,
        "treatment_share": config.TREATMENT_SHARE,
    }])


def build_campaign_response(master: pd.DataFrame, holdings: pd.DataFrame,
                            rng, campaign_start: str = "2026-01-15") -> pd.DataFrame:
    """Randomise eligible customers into treatment/control and draw outcomes."""
    elig = campaign_eligible(master, holdings).reset_index(drop=True)
    n = len(elig)
    start = pd.Timestamp(campaign_start)

    # ---- randomised assignment -------------------------------------------
    # Simple randomisation: at this sample size the arms balance on their own.
    # Stratifying by persona would tighten the estimate slightly and is the
    # obvious refinement if the population were smaller.
    treated = rng.random(n) < config.TREATMENT_SHARE

    p0 = elig["true_p0"].to_numpy()
    tau = elig["true_tau"].to_numpy()
    p_takeup = np.where(treated, np.clip(p0 + tau, 0.0, 1.0), p0)
    responded = rng.random(n) < p_takeup

    # ---- contact and response dates --------------------------------------
    contact_offset = rng.integers(0, CONTACT_WAVE_DAYS, size=n)
    contact_date = np.where(treated, start + pd.to_timedelta(contact_offset, unit="D"),
                            np.datetime64("NaT"))
    contact_date = pd.to_datetime(contact_date)

    # treated respond relative to their contact; control take-up is organic and
    # spread across the same calendar window, so both arms are observed over an
    # identical period.
    delay = np.minimum(rng.geometric(RESPONSE_DECAY, size=n),
                       config.ACTIVATION_WINDOW_DAYS)
    organic = rng.integers(0, config.ACTIVATION_WINDOW_DAYS + CONTACT_WAVE_DAYS, size=n)
    resp_offset = np.where(treated, contact_offset + delay, organic)
    response_date = np.where(responded, start + pd.to_timedelta(resp_offset, unit="D"),
                             np.datetime64("NaT"))
    response_date = pd.to_datetime(response_date)

    # ---- dormant opens (operational metric, not the causal outcome) -------
    p_dormant = np.where(treated, P_DORMANT_OPEN_TREATMENT, P_DORMANT_OPEN_CONTROL)
    dormant = (~responded) & (rng.random(n) < p_dormant)
    card_opened = responded | dormant

    # ---- revenue proxy ----------------------------------------------------
    # Annual value of an activated card, scaled by how much the customer spends:
    # a heavy spender's card earns more interchange and carries more balance.
    spend_index = (elig["txns_per_month"].to_numpy() * elig["avg_txn_amount_gel"].to_numpy())
    spend_index = spend_index / np.median(spend_index)
    base_value = rng.normal(*config.CARD_ANNUAL_VALUE_GEL_GAUSSIAN, size=n)
    value = np.maximum(base_value, 20.0) * spend_index ** config.CARD_VALUE_SPEND_MULTIPLIER
    annual_value = np.where(responded, np.round(value, 2), 0.0)

    def to_sk(s):
        sk = (s.dt.year * 10000 + s.dt.month * 100 + s.dt.day)
        return sk.fillna(UNKNOWN_DATE_SK).astype("int64")

    out = pd.DataFrame({
        "customer_sk": elig["customer_sk"].to_numpy(),
        "campaign_sk": 1,
        "assignment": np.where(treated, "treatment", "control"),
        "contacted": treated,
        "contact_date": contact_date,
        "contact_date_sk": to_sk(pd.Series(contact_date)),
        "responded": responded,
        "response_date": response_date,
        "response_date_sk": to_sk(pd.Series(response_date)),
        "card_opened": card_opened,
        "activated": responded,
        "annual_value_gel": annual_value,
        "contact_cost_gel": np.where(treated, config.COST_PER_CONTACT_GEL, 0.0),
    })
    return out


def campaign_summary(resp: pd.DataFrame, master: pd.DataFrame) -> dict:
    """The headline read: naive attribution vs true incremental effect."""
    t = resp[resp.assignment == "treatment"]
    c = resp[resp.assignment == "control"]
    tr, cr = t.responded.mean(), c.responded.mean()
    lift = tr - cr

    # Standard error of a difference in two proportions, and the 95% interval.
    # A campaign lift reported without an interval is not a finding: with these
    # sample sizes the noise is a meaningful fraction of the effect, and the
    # interval is what tells you whether the campaign worked at all.
    n_t, n_c = len(t), len(c)
    se = float(np.sqrt(tr * (1 - tr) / n_t + cr * (1 - cr) / n_c))
    ci_lo, ci_hi = lift - 1.96 * se, lift + 1.96 * se

    naive_conversions = int(t.responded.sum())          # "the campaign made all of these"
    incremental_conversions = n_t * lift                # what it actually caused

    avg_value = t.loc[t.responded, "annual_value_gel"].mean()
    cost = t.contact_cost_gel.sum()

    # Two versions of the truth. Mean drawn tau is what the generator planted;
    # the realisable effect accounts for probabilities being bounded at 0 and 1
    # (a sleeping-dog with p0=0.08 and tau=-0.12 can only lose 0.08). The
    # experiment estimates the realisable effect, so that is the fair benchmark.
    mt = master.set_index("customer_sk").loc[t.customer_sk]
    truth = mt["true_tau"].mean()
    realisable = (np.clip(mt["true_p0"] + mt["true_tau"], 0, 1) - mt["true_p0"]).mean()

    return {
        "eligible": len(resp), "treated": n_t, "control": n_c,
        "lift_se": se, "lift_ci_low": ci_lo, "lift_ci_high": ci_hi,
        "treatment_response_rate": tr, "control_response_rate": cr,
        "observed_lift": lift, "true_mean_tau_treated": truth,
        "true_realisable_lift": realisable,
        "naive_conversions": naive_conversions,
        "incremental_conversions": incremental_conversions,
        "overstatement_factor": naive_conversions / max(incremental_conversions, 1e-9),
        "avg_annual_value_gel": avg_value,
        "campaign_cost_gel": cost,
        "incremental_revenue_gel": incremental_conversions * avg_value,
        "roi": (incremental_conversions * avg_value - cost) / max(cost, 1e-9),
    }


# ------------------------------------------------------------------ demo
if __name__ == "__main__":
    try:
        from .customers import build_customers
    except ImportError:
        from .customers import build_customers
    from .sampling import make_rng
    from .accounts import build_products, assign_holdings

    config.validate_config()
    rng = make_rng(config.RANDOM_SEED)

    master = build_customers(30000, rng)
    products = build_products()
    holdings = assign_holdings(master, rng)

    camp = build_campaign_dim("2026-01-15", products)
    resp = build_campaign_response(master, holdings, rng)

    s = campaign_summary(resp, master)
    print(f"\neligible {s['eligible']:,} — treated {s['treated']:,} / control {s['control']:,}\n")

    print("=== the experiment ===")
    print(f"  treatment take-up      {s['treatment_response_rate']:.4f}")
    print(f"  control take-up        {s['control_response_rate']:.4f}")
    print(f"  observed lift          {s['observed_lift']:+.4f}  "
          f"95% CI [{s['lift_ci_low']:+.4f}, {s['lift_ci_high']:+.4f}]")
    print(f"  true realisable lift   {s['true_realisable_lift']:+.4f}   <- should match")
    print(f"  mean drawn tau         {s['true_mean_tau_treated']:+.4f}")
    inside = s['lift_ci_low'] <= s['true_realisable_lift'] <= s['lift_ci_high']
    print(f"  truth inside 95% CI?   {'yes' if inside else 'NO'}  "
          f"(gap {s['observed_lift'] - s['true_realisable_lift']:+.4f} "
          f"= {abs(s['observed_lift'] - s['true_realisable_lift'])/s['lift_se']:.2f} SE)")

    print("\n=== naive vs incremental ===")
    print(f"  naive: all treated responders   {s['naive_conversions']:,}")
    print(f"  true incremental conversions    {s['incremental_conversions']:,.0f}")
    print(f"  naive overstates by             {s['overstatement_factor']:.1f}x")

    print("\n=== economics (whole-population blast) ===")
    print(f"  cost                 {s['campaign_cost_gel']:>12,.0f} GEL")
    print(f"  incremental revenue  {s['incremental_revenue_gel']:>12,.0f} GEL")
    print(f"  ROI                  {s['roi']:>12.1f}x")

    print("\n=== lift by persona (what targeting should exploit) ===")
    m = resp.merge(master[["customer_sk", "persona"]], on="customer_sk")
    by = m.pivot_table(index="persona", columns="assignment",
                       values="responded", aggfunc="mean")
    by["lift"] = by["treatment"] - by["control"]
    by["n"] = m.groupby("persona").size()
    print(by.sort_values("lift", ascending=False).round(4).to_string())

    print("\n=== activation (dormant cards earn nothing) ===")
    t = resp[resp.assignment == "treatment"]
    print(f"  opened    {t.card_opened.mean():.4f}")
    print(f"  activated {t.activated.mean():.4f}")
    print(f"  activation rate among opens {t.activated.sum()/t.card_opened.sum():.3f}")
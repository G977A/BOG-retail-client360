"""
Generator configuration — the single source of truth for every parameter that
shapes the synthetic data. Kept apart from generation code so the domain
assumptions (this file) can be reviewed and tuned without touching mechanics.

Personas are grounded in Bank of Georgia's real retail structure: the Student
Card segment, Mass Retail (Plus+ loyalty), SOLO premium banking, plus
demographically distinct groups (cash-preferring, pensioners, remittance
households) that reflect the Georgian market.

All monetary values are in GEL and are a first-pass estimate meant to be tuned
against real-market knowledge. For the pensioner and remittance personas the
`monthly_salary_gel_*` field represents pension / remittance inflow, not salary.

Design rationale for these values: docs/02-generator-design.md
"""

from __future__ import annotations

# ------------------------------------------------------------------ global run
RANDOM_SEED = 42
N_MONTHS = 12                 # months of history to generate
TREATMENT_SHARE = 0.70        # rest is randomised holdout control
ACTIVATION_WINDOW_DAYS = 60   # response must land within this many days of contact
COST_PER_CONTACT_GEL = 2.5

# ---------------------------------------------------------------- MCC universe
# Purchase categories used for spend-share fingerprints. ATM cash is handled
# separately via the channel mix, not as a spend category.
MCC_CATEGORIES = [
    "groceries", "dining", "ecommerce", "entertainment", "transport",
    "fuel", "utilities", "healthcare", "retail", "travel",
]

# Interaction channels. "atm" rows represent cash withdrawals.
CHANNELS = ["pos", "ecommerce", "atm", "mobile_app", "internet_bank", "branch"]

# ---------------------------------------------------------------- product list
PRODUCTS = [
    {"name": "Current Account",  "group": "daily_banking", "is_target": False},
    {"name": "Debit Card",       "group": "daily_banking", "is_target": False},
    {"name": "Credit Card",      "group": "lending",       "is_target": True},   # campaign product
    {"name": "Savings Account",  "group": "deposits",      "is_target": False},
    {"name": "Term Deposit",     "group": "deposits",      "is_target": False},
    {"name": "Consumer Loan",    "group": "lending",       "is_target": False},
]

# --------------------------------------------------------------------- personas
# Every persona is a full behavioural fingerprint. spend_shares and
# channel_shares each sum to 1.0. *_gaussian entries are (mean, std), sampled
# per customer and truncated at generation time.
#
# REVIEW TARGETS: monthly_salary_gel, balance_gel, avg_txn_amount_gel,
# txns_per_month carry the strongest market assumptions.

PERSONAS = {
    # 1. BoG Student Card segment — first-card appetite, all-digital
    "student_young_digital": {
        "population_share": 0.18,
        "age_range": (18, 26),
        "income_band": "low",
        "tenure_years_range": (1, 4),
        "txns_per_month_gaussian": (48, 12),
        "avg_txn_amount_gel_gaussian": (22, 7),
        "monthly_salary_gel_gaussian": (2000, 400),   # part-time / stipend / parental
        "balance_gel_gaussian": (1200, 300),
        "salary_regularity": 0.70,
        "spend_shares": {
            "dining": 0.22, "ecommerce": 0.20, "entertainment": 0.16,
            "transport": 0.12, "groceries": 0.10, "retail": 0.09,
            "utilities": 0.05, "travel": 0.03, "fuel": 0.02, "healthcare": 0.01,
        },
        "channel_shares": {
            "mobile_app": 0.40, "ecommerce": 0.27, "pos": 0.25,
            "atm": 0.04, "internet_bank": 0.03, "branch": 0.01,
        },
        "archetype_mix": {
            "persuadable": 0.48, "sure_thing": 0.27,
            "lost_cause": 0.20, "sleeping_dog": 0.05,
        },
    },

    # 2. Mass Retail core — the Plus+ loyalty base
    "mass_retail_family": {
        "population_share": 0.30,
        "age_range": (33, 50),
        "income_band": "mid",
        "tenure_years_range": (4, 12),
        "txns_per_month_gaussian": (30, 8),
        "avg_txn_amount_gel_gaussian": (55, 18),
        "monthly_salary_gel_gaussian": (2600, 600),
        "balance_gel_gaussian": (3500, 1500),
        "salary_regularity": 0.95,
        "spend_shares": {
            "groceries": 0.28, "utilities": 0.14, "retail": 0.12,
            "healthcare": 0.10, "fuel": 0.10, "dining": 0.08,
            "ecommerce": 0.08, "transport": 0.05, "entertainment": 0.03,
            "travel": 0.02,
        },
        "channel_shares": {
            "pos": 0.35, "mobile_app": 0.28, "atm": 0.15,
            "ecommerce": 0.12, "internet_bank": 0.06, "branch": 0.04,
        },
        "archetype_mix": {
            "persuadable": 0.50, "sure_thing": 0.25,
            "lost_cause": 0.20, "sleeping_dog": 0.05,
        },
    },

    # 3. SOLO premium — affluent, likely already holds a premium card
    "solo_affluent": {
        "population_share": 0.12,
        "age_range": (35, 55),
        "income_band": "high",
        "tenure_years_range": (6, 20),
        "txns_per_month_gaussian": (26, 7),
        "avg_txn_amount_gel_gaussian": (150, 55),
        "monthly_salary_gel_gaussian": (7000, 2200),
        "balance_gel_gaussian": (32000, 14000),
        "salary_regularity": 0.90,
        "spend_shares": {
            "travel": 0.18, "dining": 0.18, "retail": 0.15, "ecommerce": 0.12,
            "groceries": 0.10, "entertainment": 0.08, "fuel": 0.06,
            "healthcare": 0.05, "transport": 0.04, "utilities": 0.04,
        },
        "channel_shares": {
            "mobile_app": 0.35, "pos": 0.30, "ecommerce": 0.18,
            "internet_bank": 0.10, "atm": 0.05, "branch": 0.02,
        },
        "archetype_mix": {
            "persuadable": 0.20, "sure_thing": 0.55,
            "lost_cause": 0.15, "sleeping_dog": 0.10,
        },
    },

    # 4. Cash-preferring, still-working traditionalist — low digital adoption
    "cash_traditionalist": {
        "population_share": 0.16,
        "age_range": (45, 65),
        "income_band": "low_mid",
        "tenure_years_range": (8, 25),
        "txns_per_month_gaussian": (15, 5),
        "avg_txn_amount_gel_gaussian": (40, 15),
        "monthly_salary_gel_gaussian": (1900, 450),
        "balance_gel_gaussian": (2200, 900),
        "salary_regularity": 0.97,
        "spend_shares": {
            "groceries": 0.30, "fuel": 0.16, "utilities": 0.16,
            "healthcare": 0.12, "retail": 0.10, "dining": 0.06,
            "transport": 0.05, "ecommerce": 0.02, "entertainment": 0.02,
            "travel": 0.01,
        },
        "channel_shares": {
            "atm": 0.40, "pos": 0.30, "branch": 0.14,
            "mobile_app": 0.09, "internet_bank": 0.04, "ecommerce": 0.03,
        },
        "archetype_mix": {
            "persuadable": 0.10, "sure_thing": 0.15,
            "lost_cause": 0.55, "sleeping_dog": 0.20,
        },
    },

    # 5. Pensioner — regular pension inflow, low digital, low credit appetite
    "pensioner": {
        "population_share": 0.14,
        "age_range": (63, 80),
        "income_band": "low",
        "tenure_years_range": (10, 30),
        "txns_per_month_gaussian": (12, 5),
        "avg_txn_amount_gel_gaussian": (30, 12),
        "monthly_salary_gel_gaussian": (600, 250),   # pension inflow — Georgian base pension is low; tune
        "balance_gel_gaussian": (1500, 800),
        "salary_regularity": 0.99,
        "spend_shares": {
            "groceries": 0.32, "healthcare": 0.20, "utilities": 0.18,
            "retail": 0.09, "fuel": 0.06, "transport": 0.06,
            "dining": 0.05, "ecommerce": 0.02, "entertainment": 0.01,
            "travel": 0.01,
        },
        "channel_shares": {
            "atm": 0.42, "pos": 0.26, "branch": 0.22,
            "mobile_app": 0.06, "internet_bank": 0.02, "ecommerce": 0.02,
        },
        "archetype_mix": {
            "persuadable": 0.08, "sure_thing": 0.10,
            "lost_cause": 0.62, "sleeping_dog": 0.20,
        },
    },

    # 6. Remittance household — inflows from abroad, spending tracks the cycle
    "remittance_household": {
        "population_share": 0.10,
        "age_range": (28, 50),
        "income_band": "low_mid",
        "tenure_years_range": (2, 10),
        "txns_per_month_gaussian": (22, 8),
        "avg_txn_amount_gel_gaussian": (48, 20),
        "monthly_salary_gel_gaussian": (1600, 700),   # combined local + remittance inflow
        "balance_gel_gaussian": (1800, 1100),
        "salary_regularity": 0.75,                    # remittance timing varies
        "spend_shares": {
            "groceries": 0.28, "utilities": 0.16, "retail": 0.12,
            "healthcare": 0.10, "fuel": 0.08, "ecommerce": 0.08,
            "dining": 0.07, "transport": 0.06, "entertainment": 0.03,
            "travel": 0.02,
        },
        "channel_shares": {
            "pos": 0.30, "atm": 0.28, "mobile_app": 0.18,
            "branch": 0.12, "ecommerce": 0.07, "internet_bank": 0.05,
        },
        "archetype_mix": {
            "persuadable": 0.35, "sure_thing": 0.18,
            "lost_cause": 0.37, "sleeping_dog": 0.10,
        },
    },
}

# ------------------------------------------------------------- uplift archetypes
# (p0, tau) drawn per customer from these ranges (uniform within range unless
# noted). p1 = clip(p0 + tau, 0, 1). tau is the TRUE causal uplift and is what
# stage 5's model is graded against.
ARCHETYPES = {
    "persuadable":  {"p0_range": (0.06, 0.20), "tau_range": (0.12,  0.30)},
    "sure_thing":   {"p0_range": (0.45, 0.65), "tau_range": (0.00,  0.06)},
    "lost_cause":   {"p0_range": (0.01, 0.08), "tau_range": (0.00,  0.03)},
    "sleeping_dog": {"p0_range": (0.08, 0.22), "tau_range": (-0.12, -0.04)},
}

# Strength of the behaviour -> (p0, tau) nudge (the dotted "noise" edge in the
# design diagram). 0.0 = uplift depends only on archetype; higher = more
# learnable from features. Keep small so the signal is real but not trivial.
BEHAVIOUR_UPLIFT_COUPLING = 0.15

# ------------------------------------------------------------------ revenue proxy
CARD_ANNUAL_VALUE_GEL_GAUSSIAN = (250, 80)   # before the persona spend multiplier
CARD_VALUE_SPEND_MULTIPLIER = 0.6            # higher spenders are worth more


def validate_config() -> None:
    """Fail loudly if shares don't sum to 1 or population is off. Called at
    generator startup — a silent typo here corrupts the whole dataset."""
    tol = 1e-6
    pop = sum(p["population_share"] for p in PERSONAS.values())
    assert abs(pop - 1.0) < tol, f"population_share sums to {pop}, not 1.0"

    for name, p in PERSONAS.items():
        s = sum(p["spend_shares"].values())
        assert abs(s - 1.0) < tol, f"{name} spend_shares sum to {s}"
        c = sum(p["channel_shares"].values())
        assert abs(c - 1.0) < tol, f"{name} channel_shares sum to {c}"
        a = sum(p["archetype_mix"].values())
        assert abs(a - 1.0) < tol, f"{name} archetype_mix sums to {a}"
        for cat in p["spend_shares"]:
            assert cat in MCC_CATEGORIES, f"{name}: unknown category {cat}"
        for ch in p["channel_shares"]:
            assert ch in CHANNELS, f"{name}: unknown channel {ch}"

    assert any(pr["is_target"] for pr in PRODUCTS), "no target product flagged"


if __name__ == "__main__":
    validate_config()
    print("config OK:", len(PERSONAS), "personas,", len(ARCHETYPES), "archetypes")
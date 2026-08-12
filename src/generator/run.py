"""
Generator orchestrator — one command, one reproducible dataset.

Runs all four layers in order from a single seed and writes the complete star
schema plus the sealed ground truth:

    <out>/warehouse/          <- everything the analysis is allowed to see
        dim_customer.parquet
        dim_product.parquet
        dim_merchant.parquet
        dim_date.parquet
        dim_campaign.parquet
        fact_transaction/txns_part_*.parquet
        fact_account_monthly.parquet
        fact_campaign_response.parquet

    <out>/ground_truth/       <- SEALED. Not joined during modelling.
        gt_customer_persona.parquet
        gt_customer_uplift.parquet

The two trees are physically separate so decision record 0002 is enforced by
the filesystem rather than by good intentions: the feature pipeline points at
warehouse/ and cannot reach the answers by accident.

Usage:
    python -m src.generator.run --customers 100000
    python -m src.generator.run --customers 5000 --out data/dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import pandas as pd

from . import config
from .accounts import (
    aggregate_monthly_flows,
    assign_holdings,
    build_account_monthly,
    build_date_dim,
    build_products,
)
from .campaign import build_campaign_dim, build_campaign_response
from .sampling import make_rng
from .transactions import build_merchants, generate_transactions

try:                                    # layer 1 module name varies
    from .customers import build_customers, to_dim_customer, to_gt_persona, to_gt_uplift
except ImportError:                     # pragma: no cover
    from .customers import build_customers, to_dim_customer, to_gt_persona, to_gt_uplift


def config_fingerprint() -> str:
    """Short hash of the parameters that shape the data. Two datasets with the
    same fingerprint and seed are identical; a changed fingerprint means the
    assumptions moved and downstream results are not comparable."""
    payload = json.dumps({
        "personas": config.PERSONAS,
        "archetypes": config.ARCHETYPES,
        "products": config.PRODUCTS,
        "coupling": config.BEHAVIOUR_UPLIFT_COUPLING,
        "treatment_share": config.TREATMENT_SHARE,
        "n_months": config.N_MONTHS,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def verify(wh: Path, gt: Path, master: pd.DataFrame) -> list[str]:
    """Assertions that must hold before the dataset is usable. Cheap to run,
    and they catch the class of error that produces plausible-looking but
    wrong data — the kind that survives all the way to a dashboard."""
    problems = []

    dim_cust = pd.read_parquet(wh / "dim_customer.parquet")
    keys = set(dim_cust.customer_sk)

    if dim_cust.customer_sk.duplicated().any():
        problems.append("dim_customer has duplicate customer_sk")
    if dim_cust.isna().any().any():
        problems.append("dim_customer contains nulls")

    # referential integrity, read one column at a time to stay memory-flat
    seen = set()
    for p in sorted((wh / "fact_transaction").glob("txns_part_*.parquet")):
        seen |= set(pd.read_parquet(p, columns=["customer_sk"]).customer_sk.unique())
    if not seen <= keys:
        problems.append("fact_transaction references unknown customer_sk")

    snap = pd.read_parquet(wh / "fact_account_monthly.parquet", columns=["customer_sk"])
    if not set(snap.customer_sk) <= keys:
        problems.append("fact_account_monthly references unknown customer_sk")

    resp = pd.read_parquet(wh / "fact_campaign_response.parquet")
    if not set(resp.customer_sk) <= keys:
        problems.append("fact_campaign_response references unknown customer_sk")
    if resp.customer_sk.duplicated().any():
        problems.append("fact_campaign_response has duplicate customer_sk per campaign")

    # eligibility: no customer in the campaign may already hold a credit card
    prods = pd.read_parquet(wh / "dim_product.parquet")
    int(prods.loc[prods.product_name == "Credit Card", "product_sk"].iloc[0])
    holders = set(pd.read_parquet(wh / "fact_account_monthly.parquet",
                                  columns=["customer_sk", "product_sk"])
                  .query("product_sk == @cc_sk").customer_sk)
    # holders who took the card during the campaign are fine; pre-existing are not
    pre_existing = holders & set(resp.customer_sk)
    responders = set(resp.loc[resp.responded, "customer_sk"])
    if pre_existing - responders:
        problems.append(f"{len(pre_existing - responders)} campaign rows for "
                        "pre-existing credit card holders")

    # randomisation actually random
    share = resp.contacted.mean()
    if abs(share - config.TREATMENT_SHARE) > 0.02:
        problems.append(f"treatment share {share:.3f} far from "
                        f"config {config.TREATMENT_SHARE}")

    # sealed truth: exactly one row per customer, and nothing leaked into the warehouse
    for name in ("gt_customer_persona", "gt_customer_uplift"):
        g = pd.read_parquet(gt / f"{name}.parquet")
        if len(g) != len(dim_cust) or g.customer_sk.duplicated().any():
            problems.append(f"{name} is not one row per customer")
    leaked = {"persona", "archetype", "true_p0", "true_tau", "true_p1"} & set(dim_cust.columns)
    if leaked:
        problems.append(f"GROUND TRUTH LEAKED into dim_customer: {sorted(leaked)}")

    return problems


def run(n_customers: int, seed: int, out_dir: str, start_month: str,
        chunk_size: int, campaign_start: str | None = None) -> dict:
    t0 = time.time()
    config.validate_config()

    out = Path(out_dir)
    wh, gt = out / "warehouse", out / "ground_truth"
    for d in (wh, gt, wh / "fact_transaction"):
        d.mkdir(parents=True, exist_ok=True)

    if campaign_start is None:
        # campaign opens the month after the observation window closes
        campaign_start = str((pd.Timestamp(start_month)
                              + pd.DateOffset(months=config.N_MONTHS)
                              + pd.Timedelta(days=14)).date())

    rng = make_rng(seed)
    counts: dict[str, int] = {}

    print(f"seed={seed}  customers={n_customers:,}  config={config_fingerprint()}")

    print("  [1/4] customers ...", end=" ", flush=True)
    master = build_customers(n_customers, rng)
    to_dim_customer(master).to_parquet(wh / "dim_customer.parquet", index=False)
    to_gt_persona(master).to_parquet(gt / "gt_customer_persona.parquet", index=False)
    to_gt_uplift(master).to_parquet(gt / "gt_customer_uplift.parquet", index=False)
    counts["dim_customer"] = len(master)
    print(f"{len(master):,}")

    print("  [2/4] transactions ...", end=" ", flush=True)
    products = build_products()
    products.to_parquet(wh / "dim_product.parquet", index=False)
    build_merchants().to_parquet(wh / "dim_merchant.parquet", index=False)
    build_date_dim(start_month).to_parquet(wh / "dim_date.parquet", index=False)
    info = generate_transactions(master, rng, wh / "fact_transaction",
                                 start_month=start_month, chunk_size=chunk_size)
    counts["fact_transaction"] = info["rows"]
    print(f"{info['rows']:,} rows in {info['files']} files")

    print("  [3/4] accounts ...", end=" ", flush=True)
    holdings = assign_holdings(master, rng)
    flows = aggregate_monthly_flows(wh / "fact_transaction")
    snap = build_account_monthly(master, holdings, flows, products,
                                 start_month=start_month)
    snap.to_parquet(wh / "fact_account_monthly.parquet", index=False)
    counts["fact_account_monthly"] = len(snap)
    print(f"{len(snap):,}")

    print("  [4/4] campaign ...", end=" ", flush=True)
    build_campaign_dim(campaign_start, products).to_parquet(
        wh / "dim_campaign.parquet", index=False)
    resp = build_campaign_response(master, holdings, rng, campaign_start=campaign_start)
    resp.to_parquet(wh / "fact_campaign_response.parquet", index=False)
    counts["fact_campaign_response"] = len(resp)
    print(f"{len(resp):,} eligible")

    problems = verify(wh, gt, master)

    manifest = {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "seed": seed,
        "n_customers": n_customers,
        "n_months": config.N_MONTHS,
        "start_month": start_month,
        "campaign_start": campaign_start,
        "config_fingerprint": config_fingerprint(),
        "row_counts": counts,
        "elapsed_seconds": round(time.time() - t0, 1),
        "verification": "passed" if not problems else problems,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the synthetic warehouse.")
    ap.add_argument("--customers", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    ap.add_argument("--out", default="data/parquet")
    ap.add_argument("--start-month", default="2025-01-01")
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--campaign-start", default=None)
    a = ap.parse_args()

    m = run(a.customers, a.seed, a.out, a.start_month, a.chunk_size, a.campaign_start)

    print(f"\ndone in {m['elapsed_seconds']}s -> {a.out}")
    for k, v in m["row_counts"].items():
        print(f"  {k:24s} {v:>12,}")

    if m["verification"] == "passed":
        print("\nverification passed")
    else:
        print("\nVERIFICATION FAILED:")
        for p in m["verification"]:
            print(f"  - {p}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
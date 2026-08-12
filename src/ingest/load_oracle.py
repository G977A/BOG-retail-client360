"""
Load the generated Parquet warehouse into Oracle.

Run order for a full rebuild:

    sqlcl/DataGrip:  sql/ddl/00_drop_all.sql
                     sql/ddl/01_dimensions.sql
                     sql/ddl/02_facts.sql
    shell:           python -m src.ingest.load_oracle
    sqlcl/DataGrip:  sql/ddl/03_indexes.sql      <- AFTER the load

The load follows the standard bulk pattern: disable foreign keys, truncate,
insert in batches, then re-enable the keys with VALIDATE so Oracle checks
every loaded row in one pass. Enforcing referential integrity row by row
during a 34M-row insert costs far more than validating once at the end, and
the constraints end up in exactly the same state either way.

Ground truth is deliberately NOT loaded (decision record 0002). It stays in
data/parquet/ground_truth/ and is read only at validation time.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import oracledb
import pandas as pd
from dotenv import load_dotenv

# Load order matters: dimensions before the facts that reference them.
DIMENSIONS = ["dim_date", "dim_customer", "dim_product", "dim_merchant", "dim_campaign"]
FACTS = ["fact_transaction", "fact_account_monthly", "fact_campaign_response"]

# fact_transaction is written as many part files; the rest are single files.
PARTITIONED = {"fact_transaction"}

# The DDL seeds this row, but TRUNCATE removes it, so the load puts it back.
# fact_campaign_response stores date_sk = 0 for control customers (never
# contacted) and non-responders, and those foreign keys need a target.
UNKNOWN_DATE_ROW = {
    "date_sk": 0, "full_date": None, "year": None, "quarter": None,
    "month": None, "month_name": "Unknown", "day_of_month": None,
    "day_of_week": None, "day_name": "Unknown", "is_weekend": None,
    "is_month_end": None, "year_month": None,
}


def get_connection() -> oracledb.Connection:
    """Connect in thin mode — no Oracle Instant Client required."""
    load_dotenv()
    user = os.getenv("ORACLE_USER", "rc360")
    password = os.getenv("APP_USER_PASSWORD") or os.getenv("ORACLE_PASSWORD")
    if not password:
        raise RuntimeError("Set APP_USER_PASSWORD (or ORACLE_PASSWORD) in .env")
    dsn = (f"{os.getenv('ORACLE_HOST', 'localhost')}:"
           f"{os.getenv('ORACLE_PORT', '1521')}/"
           f"{os.getenv('ORACLE_SERVICE', 'FREEPDB1')}")
    return oracledb.connect(user=user, password=password, dsn=dsn)


# ------------------------------------------------------------------ typing
def to_rows(df: pd.DataFrame) -> list[tuple]:
    """Convert a DataFrame to bind-variable tuples.

    Three conversions matter, and each one is a load failure if skipped:
      * bool  -> 0/1        the schema stores booleans as NUMBER(1)
      * NaT/NaN -> None     Oracle needs None for NULL; NaN binds as a number
      * numpy scalars -> Python scalars   the driver rejects numpy types
    """
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("int8")
        elif pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].astype(object).where(out[col].notna(), None)

    out = out.astype(object).where(pd.notnull(out), None)

    rows = []
    for rec in out.itertuples(index=False, name=None):
        rows.append(tuple(v.item() if hasattr(v, "item") else v for v in rec))
    return rows


def insert_df(conn, table: str, df: pd.DataFrame, batch_size: int) -> int:
    """Batch-insert a DataFrame. Columns are named explicitly so Parquet
    column order does not have to match the DDL."""
    cols = list(df.columns)
    binds = ", ".join(f":{i + 1}" for i in range(len(cols)))
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({binds})"

    rows = to_rows(df)
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            cur.executemany(sql, rows[i: i + batch_size])
    conn.commit()
    return len(rows)


# ------------------------------------------------------- constraint control
def fk_constraints(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, constraint_name
            FROM   user_constraints
            WHERE  constraint_type = 'R'
            ORDER  BY table_name, constraint_name
        """)
        return cur.fetchall()


def set_fks(conn, constraints, enable: bool) -> None:
    """Disable, or re-enable with VALIDATE.

    ENABLE VALIDATE makes Oracle verify every existing row against the
    constraint. That is the point: correctness is checked once over the
    finished table instead of on every insert. ENABLE NOVALIDATE would be
    faster still but leaves already-loaded rows unchecked, which defeats
    the purpose of having the constraint.
    """
    action = "ENABLE VALIDATE" if enable else "DISABLE"
    with conn.cursor() as cur:
        for table, name in constraints:
            cur.execute(f"ALTER TABLE {table} {action} CONSTRAINT {name}")


def truncate_all(conn) -> None:
    with conn.cursor() as cur:
        for t in FACTS + DIMENSIONS:      # facts first
            cur.execute(f"TRUNCATE TABLE {t}")


# ------------------------------------------------------------------ loading
def load_table(conn, wh: Path, table: str, batch_size: int) -> int:
    if table in PARTITIONED:
        parts = sorted((wh / table).glob("*.parquet"))
        if not parts:
            raise FileNotFoundError(f"no parquet files under {wh / table}")
        total = 0
        for i, p in enumerate(parts, 1):
            total += insert_df(conn, table, pd.read_parquet(p), batch_size)
            print(f"      part {i}/{len(parts)}  {total:>12,} rows", end="\r", flush=True)
        print(" " * 48, end="\r")
        return total

    df = pd.read_parquet(wh / f"{table}.parquet")
    return insert_df(conn, table, df, batch_size)


def parquet_count(wh: Path, table: str) -> int:
    if table in PARTITIONED:
        return sum(len(pd.read_parquet(p, columns=["customer_sk"]))
                   for p in sorted((wh / table).glob("*.parquet")))
    return len(pd.read_parquet(wh / f"{table}.parquet", columns=None))


def verify(conn, wh: Path) -> list[str]:
    """Row counts in Oracle must match the source Parquet exactly. A silent
    shortfall is the classic bulk-load failure — rows rejected in a batch
    that nobody checked."""
    problems = []
    with conn.cursor() as cur:
        for t in DIMENSIONS + FACTS:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            got = cur.fetchone()[0]
            want = parquet_count(wh, t)
            if t == "dim_date":
                want += 1                      # the seeded Unknown member
            flag = "ok" if got == want else "MISMATCH"
            print(f"  {t:24s} oracle {got:>12,}   parquet {want:>12,}   {flag}")
            if got != want:
                problems.append(f"{t}: oracle {got} vs parquet {want}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the Parquet warehouse into Oracle.")
    ap.add_argument("--data-dir", default="data/parquet")
    ap.add_argument("--batch-size", type=int, default=50_000)
    a = ap.parse_args()

    wh = Path(a.data_dir) / "warehouse"
    if not wh.exists():
        raise SystemExit(f"{wh} not found — run src.generator.run first")

    t0 = time.time()
    conn = get_connection()
    print(f"connected to Oracle {conn.version}\n")

    fks = fk_constraints(conn)
    print(f"disabling {len(fks)} foreign keys")
    set_fks(conn, fks, enable=False)

    print("truncating tables")
    truncate_all(conn)

    print("\nloading dimensions")
    for t in DIMENSIONS:
        n = load_table(conn, wh, t, a.batch_size)
        if t == "dim_date":
            insert_df(conn, "dim_date", pd.DataFrame([UNKNOWN_DATE_ROW]), a.batch_size)
            n += 1
        print(f"  {t:24s} {n:>12,}")

    print("\nloading facts")
    for t in FACTS:
        s = time.time()
        n = load_table(conn, wh, t, a.batch_size)
        rate = n / max(time.time() - s, 1e-9)
        print(f"  {t:24s} {n:>12,}   ({rate:,.0f} rows/s)")

    print(f"\nre-enabling {len(fks)} foreign keys with VALIDATE")
    try:
        set_fks(conn, fks, enable=True)
    except oracledb.DatabaseError as e:
        # ORA-02298 means loaded rows violate a foreign key: a fact points at
        # a dimension member that was never loaded. That is a real data bug,
        # not a load hiccup.
        print(f"\nFOREIGN KEY VALIDATION FAILED: {e}")
        print("A fact table references a dimension key that does not exist.")
        raise SystemExit(1)

    print("\nverifying row counts")
    problems = verify(conn, wh)

    conn.close()
    print(f"\ndone in {time.time() - t0:.1f}s")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("verification passed")
    print("\nNext: run sql/ddl/03_indexes.sql to build indexes and gather statistics.")


if __name__ == "__main__":
    main()
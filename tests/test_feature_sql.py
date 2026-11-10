"""Every op an expr can call in SQL gives what numpy gives, on each engine: the edge values
(zero, negative zero, NULL, NaN, +-inf, huge) and random ones, a NULL and a NaN both read
as missing. DuckDB always runs; Spark when pyspark 4 and Java 17+ are there; BigQuery when
FEATURE_SQL_BIGQUERY_PROJECT names a project to run in (literal-only queries, no table
read)."""
from __future__ import annotations

import itertools
import os

import numpy as np
import pytest

ibis = pytest.importorskip("ibis")
pa = pytest.importorskip("pyarrow")
pd = pytest.importorskip("pandas")

from gcpbricks.feature_manager import sql as S
from gcpbricks.feature_manager.expr import evaluate, parse

EDGES = [0.0, -0.0, 1.0, -1.0, 2.5, -2.5, 1e-12, -1e-12, 1e300, -1e300, 700.0, 710.0, -750.0,
         float("inf"), float("-inf"), float("nan"), None]
M = "cols('m{i}', i=range(5))"
EXPRS = [
    "a + b", "a - b", "a * b", "a / b", "a ** 2", "a ** 3", "a ** 0.5", "a ** -1",
    "a < b", "a <= b", "a > b", "a >= b", "a == b", "a != b", "-a",
    "(a > 0) & (b > 0)", "(a > 0) | (b > 0)", "~(a > 0)", "a & b", "~a",
    "log(a)", "log1p(a)", "exp(a)", "expm1(a)", "sqrt(a)", "abs(a)", "tanh(a)", "isnan(a)",
    "fillna(a, 7)", "fillna(a, b)", "where(a > b, a, b)", "where(a, b, c)",
    "maximum(a, b)", "minimum(a, b)", "clip(a, lo=-1, hi=2)", "clip(a, lo=0)", "float64(a)",
    f"row_sum({M})", f"row_mean({M})", f"row_max({M})", f"row_min({M})",
    f"row_std({M})", f"row_std({M}, ddof=1)", f"row_slope({M})",
    f"row_slope({M}, x=[1, 3, 7, 10, 14], eps=1e-4)", f"row_polyfit({M}, deg=2, k=1)",
    f"row_polyfit({M}, deg=1, k=0, x=[1, 3, 7, 10, 14])", f"row_argmax({M})",
    f"row_argmax({M}, last=1)", f"row_argmax({M} > 0)", f"row_sum(row_diff({M}))",
    f"row_max(col_slice({M}, start=1, stop=4))", f"row_max(row_detrend({M}, x=[1, 3, 7, 10, 14]))",
    f"row_mean(fillna({M}, 0) / maximum(a, 1e-12))",
    "tanh(where(b > 1e-4, a / (b + 1e-4), 0) / 2) * 3",
]


def _frame() -> pd.DataFrame:
    """Every pair of edge values for a and b (c random), then random rows; the matrix
    columns m0..m4 mostly random with some edges. None is a NULL, NaN a NaN."""
    rng = np.random.default_rng(0)
    pairs = list(itertools.product(EDGES, EDGES))
    n = len(pairs) + 400
    rand = lambda: list(rng.normal(0, 3, n - len(pairs)))
    cols = {"a": [p[0] for p in pairs] + rand(), "b": [p[1] for p in pairs] + rand(),
            "c": list(rng.choice([0.0, 1.0, -2.0, float("nan"), None], len(pairs))) + rand()}
    for i in range(5):
        cells = list(rng.normal(0, 3, n))
        for j in rng.choice(n, 60, replace=False):
            cells[j] = EDGES[rng.integers(len(EDGES))]
        cols[f"m{i}"] = cells
    return pd.DataFrame({k: pd.array(v, dtype="Float64") for k, v in cols.items()})


def _arrow(df: pd.DataFrame):
    """None as a NULL, NaN as a NaN: pandas' Float64 keeps the two apart."""
    return pa.table({c: pa.array([None if v is pd.NA else float(v) for v in df[c].astype(object)],
                                 pa.float64()) for c in df.columns})


def _numpy(df: pd.DataFrame, source: str) -> np.ndarray:
    x = {c: df[c].astype(float).to_numpy() for c in df.columns}
    return np.asarray(evaluate(parse(source, {}), x.__getitem__, len(df)), dtype=np.float64)


def _sql(dialect: str, table=None) -> str:
    """Every expr as a column of one program over table t's columns, in `dialect`;
    `table`, a sqlglot source standing in for t."""
    import sqlglot

    from gcpbricks.feature_manager import sqlx as X

    program, reads = S.Program(), {}

    def lookup(name):
        if name not in reads:
            reads[name] = program.bind(S.column(name))
        return reads[name]

    low = S.Lowering(program, lookup)
    for i, e in enumerate(EXPRS):
        program.bind(_float(low.lower(parse(e, {}))), name=f"e{i}")
    source = sqlglot.parse_one("SELECT * FROM t") if table is None else sqlglot.exp.select("*").from_(table)
    return X.render(program.sql(source, [f"e{i}" for i in range(len(EXPRS))]), dialect)


def _float(v):
    v = S._v(v)
    return S._num(v)


def _check(df, got: dict[str, np.ndarray], skip=None):
    """`skip`: (expr -> row mask) cases an engine is known to give otherwise."""
    bad = {}
    for i, e in enumerate(EXPRS):
        want, have = _numpy(df, e), np.asarray(got[f"e{i}"], dtype=np.float64)
        close = np.isclose(have, want, rtol=1e-12, atol=1e-12, equal_nan=True)
        if skip and e in skip:
            close |= skip[e](df)
        if not close.all():
            j = int(np.argmin(close))
            bad[e] = (f"{int((~close).sum())} rows, e.g. a={df.a[j]!r} b={df.b[j]!r}: "
                      f"sql {have[j]!r} vs numpy {want[j]!r}")
    assert not bad, "\n".join(f"{k}: {v}" for k, v in bad.items())


def test_what_sql_cannot_state_is_named():
    assert S.unsupported(parse("a % 7", {})) == ["% (numpy's remainder is exact; BigQuery has no FLOAT64 MOD)"]
    assert S.unsupported(parse("row_gini(cols('m{i}', i=range(5))) + float32(a)", {})) == ["row_gini()", "float32()"]
    assert S.unsupported(parse("a ** 1.5", {}))[0].startswith("** 1.5")
    assert S.unsupported(parse("log1p(a / b) ** 2", {})) == []


def test_duckdb_gives_what_numpy_gives():
    duckdb = pytest.importorskip("duckdb")
    df = _frame()
    con = duckdb.connect()
    con.register("t", _arrow(df))
    out = con.sql(_sql("duckdb")).arrow()
    out = out.read_all() if hasattr(out, "read_all") else out
    _check(df, {c: out.column(c).to_numpy(zero_copy_only=False) for c in out.column_names})


def _spark_ready() -> bool:
    import shutil
    import subprocess

    try:
        import pyspark  # noqa: F401
    except ImportError:
        return False
    java = shutil.which("java")
    out = subprocess.run([java, "-version"], capture_output=True, text=True, check=False).stderr if java else ""
    return any(f'version "{v}' in out for v in ("17", "21", "25"))


@pytest.mark.skipif(not _spark_ready(), reason="needs pyspark 4 and Java 17+")
def test_spark_gives_what_numpy_gives():
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[1]").config("spark.ui.enabled", "false")
             .config("spark.sql.session.timeZone", "UTC").getOrCreate())
    df = _frame()
    rows = [tuple(None if v is pd.NA else float(v) for v in r) for r in df.astype(object).itertuples(index=False)]
    spark.createDataFrame(rows, schema=", ".join(f"{c} double" for c in df.columns)).createOrReplaceTempView("t")
    rows = spark.sql(_sql("spark")).toPandas()

    def neg_zero(col):
        return lambda d: np.array([v is not pd.NA and v == 0 and np.signbit(v) for v in d[col]])
    # Spark's atan2 is +pi for -0.0 too, so the zero's sign is lost: x / -0.0 is +inf there.
    _check(df, {c: rows[c].astype(float).to_numpy() for c in rows.columns},
           skip={"a / b": neg_zero("b"), "a ** -1": neg_zero("a")})


@pytest.mark.skipif(not os.environ.get("FEATURE_SQL_BIGQUERY_PROJECT"),
                    reason="set FEATURE_SQL_BIGQUERY_PROJECT to run on BigQuery")
def test_bigquery_gives_what_numpy_gives():
    import sqlglot
    from google.cloud import bigquery

    df = _frame()

    def lit(v):
        if v is pd.NA:
            return "CAST(NULL AS FLOAT64)"
        v = float(v)
        return f"CAST('{v}' AS FLOAT64)" if not np.isfinite(v) else repr(v)

    rows = ", ".join("STRUCT(" + ", ".join(f"{lit(v)} AS {c}" for c, v in zip(df.columns, r)) + ")"
                     for r in df.astype(object).itertuples(index=False))
    source = sqlglot.parse_one(f"SELECT * FROM UNNEST([{rows}])", read="bigquery").subquery("t")
    client = bigquery.Client(project=os.environ["FEATURE_SQL_BIGQUERY_PROJECT"])
    job = client.query(_sql("bigquery", source))
    out = job.result().to_arrow()
    assert job.total_bytes_processed in (0, None)
    _check(df, {c: out.column(c).to_numpy(zero_copy_only=False) for c in out.column_names})

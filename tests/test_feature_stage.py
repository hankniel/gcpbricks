"""A features.yaml's SQL stage computed by the feature store gives what the pipeline
computes itself: aggregations over a long view's rows (checked against pandas, by the
window rules: ending at the decision point, that day included; an empty window's sum and
count 0, the rest NaN), transforms against numpy, and the preprocessing after them the same
on either frame."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import numpy as np
import pytest
import yaml

pytest.importorskip("ibis")
pytest.importorskip("duckdb")
pd = pytest.importorskip("pandas")

from gcpbricks.feature_manager import ExprError, FeaturePipeline, compile_spec
from gcpbricks.features import FeatureStore

AT = 3
DAYS = [datetime(2026, 9, d) for d in range(1, 5)]
SEGMENTS = ["a", "b", "c", "quiet"]          # "quiet" has no long rows at all
KEYS = [{"name": "segment", "data_type": "String", "meta": {"entity": True}},
        {"name": "snapshot_date", "data_type": "String", "meta": {"entity": True}},
        {"name": "feature_timestamp", "data_type": "DateTime", "meta": {"feature_timestamp": True}}]

SPEC = {
    "vars": {"eps": 1e-9},
    "aggregations": [
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "3d"},
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "lifetime"},
        {"source": "lng", "input": "rev", "function": "sum",
         "time_window": {"window_size": "2d", "offset": "-1d"}},
        {"source": "lng", "input": {"name": "rev_per_sess", "formula": "rev / maximum(sess, 1)"},
         "function": "mean", "time_window": "7d"},
        {"source": "lng", "input": "rev", "function": "max", "time_window": "3d"},
        {"source": "lng", "input": "rev", "function": "count", "time_window": "3d"},
        {"source": "lng", "input": "sess", "function": "last", "time_window": "3d"},
        {"source": "lng", "input": "sess", "function": "first", "time_window": "lifetime"},
        {"name": "rev_series", "source": "lng", "input": "rev", "function": "sum",
         "time_window": {"series_start": "-3d", "window_size": "1d"}, "keep": False},
    ],
    "transforms": [
        {"name": "forecast", "formula": "fillna(cols('val_d{d}', d=range(0, 4)), 0) / maximum(cost, eps)",
         "keep": False},
        {"name": "pulse", "formula": "row_mean(forecast)"},
        {"name": "trend", "formula": "row_slope(rev_series)"},
        {"name": "momentum", "formula": "rev_sum_3d / maximum(rev_sum_2d_offset_1d, eps)"},
        {"name": "log_cost", "formula": "log1p(cost)"},
    ],
    "preprocessing": [
        {"name": "pulse_med", "fit": "median", "of": "pulse", "by": "geo"},
        {"name": "excess", "formula": "forecast - pulse_med", "keep": False},
        {"name": "excess_mean", "formula": "row_mean(excess)"},
        {"name": "gini", "formula": "row_gini(forecast)"},
    ],
    "inputs": {"default": ["+outputs", "geo"]},
}


def _models(fs_path, lng_path):
    return {"version": 2, "models": [
        {"name": "fs_c", "meta": {"feature_store": {"offline_table": str(fs_path)}},
         "columns": copy.deepcopy(KEYS) + [{"name": "cost", "data_type": "Float64"},
                                           {"name": "geo", "data_type": "String"}]
         + [{"name": f"val_d{d}", "data_type": "Float64"} for d in range(4)]},
        {"name": "lng", "meta": {"feature_store": {"offline_table": str(lng_path)}},
         "columns": copy.deepcopy(KEYS) + [{"name": "rev", "data_type": "Float64"},
                                           {"name": "sess", "data_type": "Float64"}]},
    ]}


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


ENGINES = ["duckdb", pytest.param("spark", marks=pytest.mark.skipif(
    not _spark_ready(), reason="needs pyspark 4 and Java 17+"))]
SPARK_CONF = {"spark.master": "local[1]", "spark.sql.session.timeZone": "UTC", "spark.ui.enabled": "false"}


@pytest.fixture(params=ENGINES)
def data(tmp_path, request):
    return _data(tmp_path, request.param)


def _data(tmp_path, engine="duckdb"):
    rng = np.random.default_rng(3)
    wide, long = [], []
    for c in SEGMENTS:
        for d in DAYS:
            row = {"segment": c, "snapshot_date": d.strftime("%Y-%m-%d"), "feature_timestamp": d,
                   "cost": float(rng.choice([0.0, 2.0, 10.0, np.nan])), "geo": rng.choice(["us", "vn"])}
            row.update({f"val_d{i}": float(rng.choice([0.0, 1.5, np.nan, 4.0])) for i in range(4)})
            wide.append(row)
            if c == "quiet":
                continue
            for age in range(8):          # ages past the decision point must not count
                if rng.uniform() < 0.25:
                    continue
                rev = rng.choice([1.0, 2.0, 5.0, np.nan, None])
                long.append({"segment": c, "snapshot_date": d.strftime("%Y-%m-%d"),
                             "feature_timestamp": d + timedelta(days=age), "age": age,
                             "rev": rev, "sess": float(rng.integers(0, 4))})
    us = {"coerce_timestamps": "us", "allow_truncated_timestamps": True}
    w, ln = pd.DataFrame(wide), pd.DataFrame(long)
    w.to_parquet(tmp_path / "fs.parquet", **us)
    ln.drop(columns="age").astype({"rev": "Float64"}).to_parquet(tmp_path / "lng.parquet", **us)
    models = tmp_path / "dbt-features" / "p" / "models"
    models.mkdir(parents=True)
    (models / "_models.yml").write_text(yaml.safe_dump(_models(tmp_path / "fs.parquet", tmp_path / "lng.parquet")))
    store = FeatureStore(str(tmp_path / "dbt-features"), engine=engine,
                         spark_conf=SPARK_CONF if engine == "spark" else None)
    return store, w, ln


def _expected_aggregations(w: pd.DataFrame, ln: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """The aggregations by hand: a long row's age counts when inside the window's ages."""
    out = []
    for _, r in w.iterrows():
        rows = ln[(ln.segment == r.segment) & (ln.snapshot_date == r.snapshot_date)]
        rev = rows.rev.astype(float)
        ok = rev.notna()

        def ages(lo, hi, ok=ok, rows=rows):
            return ok & (rows.age >= lo) & (rows.age <= hi)
        per = (rows.rev.astype(float) / np.maximum(rows.sess, 1))
        e = {"rev_sum_3d": rev[ages(1, AT)].sum(), "rev_sum_lifetime": rev[ages(-99, AT)].sum(),
             "rev_sum_2d_offset_1d": rev[ages(1, 2)].sum(),
             "rev_per_sess_mean_7d": per[per.notna() & (rows.age <= AT)].mean(),
             "rev_max_3d": rev[ages(1, AT)].max(), "rev_count_3d": float(ages(1, AT).sum())}
        window = rows[(rows.age >= 1) & (rows.age <= AT)].sort_values("age")
        e["sess_last_3d"] = window.sess.iloc[-1] if len(window) else np.nan
        life = rows[rows.age <= AT].sort_values("age")
        e["sess_first_lifetime"] = life.sess.iloc[0] if len(life) else np.nan
        e.update({f"rev_series__{i}": rev[ages(a, a)].sum() for i, a in enumerate([1, 2, 3])})
        out.append({prefix + k: v for k, v in e.items()})
    return pd.DataFrame(out, index=w.index)


def test_the_feature_stores_stage_gives_what_the_pipeline_computes(data):
    fs, w, ln = data
    plan = compile_spec(SPEC)
    stage = plan.sql_stage()
    fv = fs.get_feature_view("fs_c")
    bfv = fs.batch_feature_view("m", "fs_c", stage, at_days=AT)
    by_sql = fs.create_training_set(fv.spine(), [bfv, fv[list(stage.python_columns)]]).load_df()
    key = ["segment", "snapshot_date"]
    by_sql = by_sql.sort_values(key, ignore_index=True)
    # The raw columns only the transforms read stay in the warehouse.
    assert "val_d0" not in by_sql and "cost" not in by_sql and "geo" in by_sql

    # The reference: aggregations by hand (the feature store's only), transforms by numpy.
    raw = w.sort_values(key, ignore_index=True)
    reference_in = pd.concat([raw, _expected_aggregations(raw, ln, stage.prefix)], axis=1)
    sql_aggs = [c for c in by_sql if c.startswith(stage.prefix) and "rev_" in c and "series" not in c]
    for c in sql_aggs:
        np.testing.assert_allclose(by_sql[c].astype(float), reference_in[c].astype(float),
                                   rtol=1e-12, equal_nan=True, err_msg=c)

    on_sql = FeaturePipeline(SPEC).fit(by_sql)
    on_raw = FeaturePipeline(SPEC).fit(reference_in)
    outputs = list(on_sql.plan_.outputs)
    got = on_sql.transform(by_sql)[outputs].to_numpy(np.float64)
    want = on_raw.transform(reference_in)[outputs].to_numpy(np.float64)
    close = np.isclose(got, want, rtol=1e-12, atol=1e-12, equal_nan=True)
    assert close.all(), {outputs[j]: (got[~close[:, j], j][:3], want[~close[:, j], j][:3])
                         for j in np.where(~close.all(axis=0))[0]}
    assert not [c for c in on_sql.transform(by_sql).columns if c.startswith("__sql_")]


def test_a_frame_of_another_specs_sql_columns_is_not_read_as_this_ones(data):
    _, w, ln = data
    other = copy.deepcopy(SPEC)
    other["transforms"][1]["formula"] = "row_sum(forecast)"
    plan, other_plan = compile_spec(SPEC), compile_spec(other)
    assert plan.sql_prefix != other_plan.sql_prefix
    raw = w.sort_values(["segment", "snapshot_date"], ignore_index=True)
    frame = pd.concat([raw, _expected_aggregations(raw, ln, plan.sql_prefix)], axis=1)
    frame[other_plan.sql_prefix + "pulse"] = -12345.0     # another spec's value, same name
    out = FeaturePipeline(SPEC).fit(frame).transform(frame)
    assert not (out["pulse"] == -12345.0).any()
    assert out["pulse"].notna().any()


def test_an_aggregation_the_frame_lacks_fails(data):
    _, w, _ = data
    with pytest.raises(KeyError, match="computed by the feature store"):
        FeaturePipeline(SPEC).fit(w)


def test_the_blocks_keep_sql_and_python_apart():
    bad = copy.deepcopy(SPEC)
    bad["transforms"].append({"name": "g2", "formula": "row_gini(forecast)"})
    with pytest.raises(ExprError, match="move it to preprocessing"):
        compile_spec(bad)
    back = copy.deepcopy(SPEC)
    back["transforms"].append({"name": "late", "formula": "pulse_med * 2"})
    with pytest.raises(ExprError, match="before they are declared"):
        compile_spec(back)
    both = {**copy.deepcopy(SPEC), "define": [{"name": "x", "expr": "cost"}]}
    with pytest.raises(ValueError, match="not both"):
        compile_spec(both)
    stage = compile_spec(SPEC).sql_stage()
    assert set(stage.needed) >= {"forecast", "pulse", "trend", "momentum", "rev_sum_3d"}
    assert stage.python_columns == ("geo",)


def test_aggregations_need_the_decision_point(data):
    fs, _, _ = data
    with pytest.raises(ValueError, match="at_days"):
        fs.batch_feature_view("m", "fs_c", compile_spec(SPEC).sql_stage())


@pytest.mark.skipif(not __import__("os").environ.get("FEATURE_SQL_BIGQUERY_PROJECT"),
                    reason="set FEATURE_SQL_BIGQUERY_PROJECT to run on BigQuery")
def test_on_bigquery_the_stage_gives_what_duckdb_gives(tmp_path):
    """The whole query on BigQuery, each view's table its rows inline (no table read)."""
    import os

    duck, _, _ = _data(tmp_path)
    by_duckdb = _stage_frame(duck)

    bq = inline_bigquery(str(tmp_path / "dbt-features"), os.environ["FEATURE_SQL_BIGQUERY_PROJECT"])
    by_bigquery = _stage_frame(bq)
    cols = [c for c in by_duckdb.columns if c.startswith("__sql_")]
    for c in cols:
        np.testing.assert_allclose(by_bigquery[c].astype(float), by_duckdb[c].astype(float),
                                   rtol=1e-12, atol=1e-12, equal_nan=True, err_msg=c)


def inline_bigquery(dbt_ml: str, project: str) -> FeatureStore:
    """A BigQuery store whose views read their parquet rows inline: no table is scanned."""
    import pyarrow.parquet as pq

    def literal(v, kind):
        if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
            return f"CAST(NULL AS {kind})"
        if kind == "TIMESTAMP":
            return f"TIMESTAMP '{pd.Timestamp(v).isoformat()}'"
        if kind == "INT64":
            return str(int(v))
        return f"'{v}'" if kind == "STRING" else repr(float(v))

    class InlineBigQuery(FeatureStore):
        def _from(self, g):
            table = pq.read_table(g.offline_table).to_pandas()
            kinds = {c: "STRING" if table[c].dtype == object else
                     "TIMESTAMP" if "datetime" in str(table[c].dtype) else
                     "INT64" if str(table[c].dtype) == "int64" else "FLOAT64" for c in table}
            rows = ", ".join("STRUCT(" + ", ".join(f"{literal(r[c], kinds[c])} AS {c}" for c in table)
                             + ")" for r in table.astype(object).to_dict("records"))
            return f"UNNEST([{rows}])"

    return InlineBigQuery(dbt_ml, engine="bigquery", bigquery_project=project)


def _stage_frame(fs):
    stage = compile_spec(SPEC).sql_stage()
    fv = fs.get_feature_view("fs_c")
    bfv = fs.batch_feature_view("m", "fs_c", stage, at_days=AT)
    df = fs.create_training_set(fv.spine(), [bfv, fv[list(stage.python_columns)]]).load_df()
    return df.sort_values(["segment", "snapshot_date"], ignore_index=True)

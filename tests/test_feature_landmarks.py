"""Long training rows (landmarks): each spine row at each decision day `a` and horizon `H`,
its features over the long view's rows up to day `a` (age windows past the anchor, others
relative to the decision point), its labels up to day `H`, aggregated over the long view's
keys the spine lacks (`over`). A decision day the data doesn't reach has no row, a horizon
it doesn't reach no label. Checked against pandas."""
from __future__ import annotations

import copy
from datetime import timedelta

import numpy as np
import pytest

pytest.importorskip("ibis")
pytest.importorskip("duckdb")
pd = pytest.importorskip("pandas")

import yaml
from test_feature_stage import DAYS, ENGINES, KEYS, SPARK_CONF, inline_bigquery

from gcpbricks.feature_manager import ExprError, compile_spec
from gcpbricks.features import FeatureStore, Spine

AT, HORIZONS = [1, 3], [5, 6]
SPEC = {
    "landmarks": {"at": AT, "horizons": HORIZONS},
    "aggregations": [
        {"name": "rev_d{d}_cum", "source": "lng", "input": "rev", "function": "sum",
         "time_window": {"from_day": 0, "to_day": "{d}"}, "over": ["age"], "for_each": {"d": [0, 1]}},
        {"name": "rev_d1", "source": "lng", "input": "rev", "function": "max",
         "time_window": {"from_day": 1, "to_day": 1}, "over": ["age"]},
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "lifetime", "over": ["age"]},
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "2d", "over": ["age"]},
        {"source": "lng", "input": "rev", "function": "count", "time_window": "2d", "over": ["age"]},
    ],
    "transforms": [{"name": "growth", "formula": "rev_sum_lifetime / maximum(rev_d0_cum, 1)"}],
    "labels": [
        {"name": "rev_cum", "source": "lng", "input": "rev", "function": "sum",
         "time_window": "lifetime", "over": ["age"]},
        {"name": "rev_after", "source": "lng", "input": "rev", "function": "sum",
         "time_window": "lifetime", "over": ["age"], "since_decision": True},
    ],
}
KEY = ["cohort", "install_date", "decision_day", "horizon_day"]


def _models(fs_path, lng_path):
    lng_keys = copy.deepcopy(KEYS)
    lng_keys.insert(2, {"name": "age", "data_type": "Int64", "meta": {"entity": True}})
    return {"version": 2, "models": [
        {"name": "fs_c", "meta": {"feature_store": {"offline_table": str(fs_path)}},
         "columns": copy.deepcopy(KEYS) + [{"name": "cost", "data_type": "Float64"}]},
        {"name": "lng", "meta": {"feature_store": {"offline_table": str(lng_path)}},
         "columns": lng_keys + [{"name": "rev", "data_type": "Float64"}]},
    ]}


@pytest.fixture(params=ENGINES)
def data(tmp_path, request):
    rng = np.random.default_rng(5)
    wide, long = [], []
    for c in ["a", "b", "quiet"]:
        for d in DAYS:
            wide.append({"cohort": c, "install_date": d.strftime("%Y-%m-%d"), "feature_timestamp": d,
                         "cost": 1.0})
            if c == "quiet":
                continue
            # Ages up to the last day the data reaches: DAYS[0] + 5.
            for age in range((DAYS[0] + timedelta(days=5) - d).days + 1):
                if rng.uniform() < 0.2:
                    continue
                long.append({"cohort": c, "install_date": d.strftime("%Y-%m-%d"), "age": age,
                             "feature_timestamp": d + timedelta(days=age),
                             "rev": rng.choice([1.0, 2.0, 5.0, np.nan, None])})
    us = {"coerce_timestamps": "us", "allow_truncated_timestamps": True}
    w, ln = pd.DataFrame(wide), pd.DataFrame(long).astype({"rev": "Float64", "age": "int64"})
    w.to_parquet(tmp_path / "fs.parquet", **us)
    ln.to_parquet(tmp_path / "lng.parquet", **us)
    models = tmp_path / "dbt-features" / "p" / "models"
    models.mkdir(parents=True)
    (models / "_models.yml").write_text(yaml.safe_dump(_models(tmp_path / "fs.parquet", tmp_path / "lng.parquet")))
    store = FeatureStore(str(tmp_path / "dbt-features"), engine=request.param,
                         spark_conf=SPARK_CONF if request.param == "spark" else None)
    return store, w, ln


def _expected(w: pd.DataFrame, ln: pd.DataFrame, prefix: str) -> pd.DataFrame:
    last = ln.feature_timestamp.max()
    out = []
    for _, r in w.iterrows():
        rows = ln[(ln.cohort == r.cohort) & (ln.install_date == r.install_date)]
        rev = rows.rev.astype(float)
        ok = rev.notna()
        for a in AT:
            if r.feature_timestamp + timedelta(days=a) > last:
                continue
            for h in HORIZONS:
                def ages(lo, hi, ok=ok, rows=rows):
                    return ok & (rows.age >= lo) & (rows.age <= hi)
                e = {"cohort": r.cohort, "install_date": r.install_date, "decision_day": a, "horizon_day": h,
                     prefix + "rev_d0_cum": rev[ages(0, 0)].sum(), prefix + "rev_d1_cum": rev[ages(0, 1)].sum(),
                     prefix + "rev_d1": rev[ages(1, 1)].max(),
                     prefix + "rev_sum_lifetime": rev[ages(0, a)].sum(),
                     prefix + "rev_sum_2d": rev[ages(a - 1, a)].sum(),
                     prefix + "rev_count_2d": float(ages(a - 1, a).sum())}
                mature = r.feature_timestamp + timedelta(days=h) <= last
                e["rev_cum"] = rev[ages(0, h)].sum() if mature else np.nan
                e["rev_after"] = rev[ages(a + 1, h)].sum() if mature else np.nan
                out.append(e)
    return pd.DataFrame(out).sort_values(KEY, ignore_index=True)


def _training(fs, spine):
    plan = compile_spec(SPEC)
    stage = plan.sql_stage()
    bfv = fs.batch_feature_view("m", "fs_c", stage)
    df = fs.create_training_set(spine, [bfv]).load_df()
    return plan, df.sort_values(KEY, ignore_index=True)


def test_landmark_rows_have_their_days_features_and_labels(data):
    fs, w, ln = data
    plan, got = _training(fs, fs.get_feature_view("fs_c").spine())
    want = _expected(w, ln, plan.sql_prefix)
    assert len(got) == len(want)
    for c in KEY[:2]:
        assert (got[c] == want[c]).all(), c
    for c in KEY[2:]:
        assert (got[c].astype(int) == want[c]).all(), c
    for c in want.columns[len(KEY):]:
        np.testing.assert_allclose(got[c].astype(float), want[c].astype(float), rtol=1e-12,
                                   equal_nan=True, err_msg=c)
    growth = got[plan.sql_prefix + "growth"].astype(float)
    np.testing.assert_allclose(growth, want[plan.sql_prefix + "rev_sum_lifetime"]
                               / np.maximum(want[plan.sql_prefix + "rev_d0_cum"], 1), rtol=1e-12)
    # Some rows dropped (decision day unreached) and some labels NULL (horizon unreached).
    assert len(got) < len(w) * len(AT) * len(HORIZONS)
    assert got.rev_cum.isna().any() and got.rev_cum.notna().any()


def test_a_bounded_spine_reads_the_same(data):
    fs, w, ln = data
    spine = Spine("fs_c", start=DAYS[1], end=DAYS[2])
    plan, got = _training(fs, spine)
    want = _expected(w[w.feature_timestamp.between(DAYS[1], DAYS[2])], ln, plan.sql_prefix)
    assert len(got) == len(want) > 0
    for c in want.columns[len(KEY):]:
        np.testing.assert_allclose(got[c].astype(float), want[c].astype(float), rtol=1e-12,
                                   equal_nan=True, err_msg=c)


def test_a_key_the_spine_lacks_needs_over(data):
    fs, _, _ = data
    spec = copy.deepcopy(SPEC)
    spec["aggregations"][2]["over"] = []
    spec["aggregations"] = spec["aggregations"][2:3]
    spec["transforms"] = []
    stage = compile_spec(spec).sql_stage()
    bfv = fs.batch_feature_view("m", "fs_c", stage)
    with pytest.raises(KeyError, match="over"):
        fs.create_training_set(fs.get_feature_view("fs_c").spine(), [bfv]).load_df()


def test_landmarks_never_read_past_the_first_decision_day():
    spec = copy.deepcopy(SPEC)
    spec["aggregations"][0]["for_each"] = {"d": [0, 2]}
    with pytest.raises(ExprError, match="past decision day"):
        compile_spec(spec)
    spec = copy.deepcopy(SPEC)
    spec["transforms"] = [{"name": "x", "formula": "rev_day_3 * 2"}]
    with pytest.raises(ExprError, match="wide table"):
        compile_spec(spec)
    spec = copy.deepcopy(SPEC)
    spec["landmarks"]["horizons"] = [3]
    with pytest.raises(ValueError, match="horizons"):
        compile_spec(spec)


FIXED_DAY = {
    "landmarks": {"at": AT, "horizons": HORIZONS},
    "aggregations": [
        {"name": "rev_le1", "source": "lng", "function": "sum", "time_window": "lifetime", "over": ["age"],
         "input": {"name": "rev_if_le1", "formula": "where(age <= 1, rev, nan)"}},
    ],
    "transforms": [
        {"name": "rev_d1_cum_known", "formula": "where(decision_day >= 3, rev_le1, nan)"},
        {"name": "k", "formula": "decision_day + 0"},
        {"name": "to_horizon", "formula": "horizon_day - decision_day"},
    ],
}


def test_a_transform_reads_the_rows_decision_and_horizon_day(data):
    """A value as of a fixed age (days 0..1 here), known only from decision day 3 on: a
    lifetime sum of the ages it covers, masked by the row's decision_day."""
    fs, w, ln = data
    plan = compile_spec(FIXED_DAY)
    assert not {"decision_day", "horizon_day"} & {*plan.raw_columns, *plan.numeric_columns}
    bfv = fs.batch_feature_view("m", "fs_c", plan.sql_stage())
    got = fs.create_training_set(fs.get_feature_view("fs_c").spine(), [bfv]).load_df()
    got = got.sort_values(KEY, ignore_index=True)
    want = _expected(w, ln, "")
    assert len(got) == len(want)
    p = plan.sql_prefix
    known = want.decision_day >= 3
    np.testing.assert_allclose(got[p + "rev_d1_cum_known"].astype(float),
                               np.where(known, want.rev_d1_cum, np.nan), rtol=1e-12, equal_nan=True)
    assert (got[p + "k"].astype(int) == want.decision_day).all()
    assert (got[p + "to_horizon"].astype(int) == want.horizon_day - want.decision_day).all()


def test_decision_day_is_a_raw_column_without_landmarks():
    spec = {"transforms": [{"name": "k", "formula": "decision_day + 0"}]}
    assert "decision_day" in compile_spec(spec).raw_columns


@pytest.mark.skipif(not __import__("os").environ.get("FEATURE_SQL_BIGQUERY_PROJECT"),
                    reason="set FEATURE_SQL_BIGQUERY_PROJECT to run on BigQuery")
@pytest.mark.parametrize("data", ["duckdb"], indirect=True)
def test_on_bigquery_landmarks_give_what_duckdb_gives(data, tmp_path):
    import os

    duck, _, _ = data
    _, want = _training(duck, duck.get_feature_view("fs_c").spine())
    bq = inline_bigquery(str(tmp_path / "dbt-features"), os.environ["FEATURE_SQL_BIGQUERY_PROJECT"])
    _, got = _training(bq, bq.get_feature_view("fs_c").spine())
    assert len(got) == len(want)
    for c in want.columns:
        if c == "feature_timestamp":
            assert (pd.to_datetime(got[c], utc=True) == pd.to_datetime(want[c], utc=True)).all()
        elif c in KEY[:2]:
            assert (got[c] == want[c]).all(), c
        else:
            np.testing.assert_allclose(got[c].astype(float), want[c].astype(float), rtol=1e-12,
                                       equal_nan=True, err_msg=c)


ROLLING = {
    "aggregations": [
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "3d", "over": ["age"]},
        {"name": "rev_daily", "source": "lng", "input": "rev", "function": "sum", "over": ["age"],
         "time_window": {"series_start": "-3d", "window_size": "1d"}, "keep": False},
    ],
    "transforms": [{"name": "rev_trend", "formula": "row_slope(rev_daily)"}],
}


def test_a_long_spines_rows_read_their_entitys_recent_rows(data):
    """The long view as the spine, at_days=0: each (cohort, age) row keeps its age and
    reads its cohort's rows of the 3 days up to its own (`over: [age]`)."""
    fs, _, ln = data
    plan = compile_spec(ROLLING)
    bfv = fs.batch_feature_view("m", "lng", plan.sql_stage(), at_days=0)
    got = fs.create_training_set(fs.get_feature_view("lng").spine(), [bfv]).load_df()
    key = ["cohort", "install_date", "age"]
    got = got.sort_values(key, ignore_index=True)
    want = ln.sort_values(key, ignore_index=True)
    assert len(got) == len(want) and (got.age.astype(int) == want.age).all()

    def day(r, back):
        rows = ln[(ln.cohort == r.cohort) & (ln.install_date == r.install_date)
                  & (ln.age == r.age - back)]
        return rows.rev.astype(float).sum()
    daily = np.array([[day(r, b) for b in (2, 1, 0)] for r in want.itertuples()])
    np.testing.assert_allclose(got[plan.sql_prefix + "rev_sum_3d"].astype(float), daily.sum(axis=1),
                               rtol=1e-12)
    x = np.arange(3) - 1.0
    slope = (daily - daily.mean(axis=1, keepdims=True)) @ x / (x @ x)
    np.testing.assert_allclose(got[plan.sql_prefix + "rev_trend"].astype(float), slope,
                               rtol=1e-9, atol=1e-12)


def test_a_window_before_the_decision_point_does_not_cut_the_frontier(data):
    """A feature over the spine's own earlier days (Tecton's sliding window on its view) reads that view
    only up to the window's end, before the decision point; that cut is not where the data ends. Every
    spine row keeps its row, and a label whose horizon the data reaches keeps its value."""
    fs, w, ln = data
    spec = {"landmarks": {"at": [0], "horizons": [2]},
            "aggregations": [{"name": "cost_before", "source": "fs_c", "input": "cost", "function": "sum",
                              "time_window": {"window_size": "2d", "offset": "-1d"}, "over": ["install_date"]}],
            "labels": [{"name": "rev_cum", "source": "lng", "input": "rev", "function": "sum",
                        "time_window": "lifetime", "over": ["age"]}]}
    plan = compile_spec(spec)
    bfv = fs.batch_feature_view("m", "fs_c", plan.sql_stage())
    got = fs.create_training_set(Spine("fs_c", start=DAYS[0], end=DAYS[1]), [bfv]).load_df()
    spine = w[w.feature_timestamp.between(DAYS[0], DAYS[1])]
    assert len(got) == len(spine)                           # the window's last day is not dropped
    last = ln.feature_timestamp.max()                       # DAYS[0] + 5: every horizon here is reached
    assert all(d + timedelta(days=2) <= last for d in spine.feature_timestamp)
    assert got.rev_cum.notna().all()
    by = got.set_index(["cohort", "install_date"])[plan.sql_prefix + "cost_before"]
    assert by[("a", DAYS[1].strftime("%Y-%m-%d"))] == 1.0   # the day before, one row of cost 1
    assert by[("a", DAYS[0].strftime("%Y-%m-%d"))] == 0.0   # nothing before the first day

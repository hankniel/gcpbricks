"""gcpbricks.feature_manager: a `features` spec compiles to a plan, fits like any
sklearn transformer, keeps fit-time statistics frozen, scores a batch independently of what
else shares it, and survives being pickled into an artifact."""
from __future__ import annotations

import subprocess
import sys
import textwrap

import cloudpickle
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from sklearn.exceptions import NotFittedError

from gcpbricks import feature_manager
from gcpbricks.feature_manager import ExprError, FeaturePipeline, compile_spec
from gcpbricks.feature_manager.plan import schema_problems
from gcpbricks.feature_manager.spec import FeaturesConfig

Q = [0, 0.25, 0.5, 0.75, 1]


def _frame(n: int = 40, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "platform": rng.choice(["ios", "android"], n),
        "cost": rng.uniform(1, 100, n),
        **{f"val_day_{d}": rng.uniform(0, 10, n) for d in range(4)},
    })
    df["val_day_3_cum"] = df[[f"val_day_{d}" for d in range(4)]].sum(axis=1)
    df["val_day_1_cum"] = df[["val_day_0", "val_day_1"]].sum(axis=1)
    return df


SPEC = {
    "vars": {"days": [0, 1, 2, 3], "eps": 1e-4},
    "define": [
        {"name": "rev_growth_d{x}_d{y}", "expr": "(val_day_{y}_cum - val_day_{x}_cum) / (val_day_{x}_cum + eps)",
         "for_each": {"x,y": [[1, 3]]}},
        {"name": "ratio_daily", "expr": "cols('val_day_{d}', d=days) / maximum(cost, 1e-12)", "keep": False},
        {"name": "log_ratio", "expr": "log1p(row_sum(ratio_daily))", "keep": False},
        {"name": "log_ratio_med", "fit": "median", "of": "log_ratio", "by": "platform", "keep": False},
        {"name": "ratio_alpha", "expr": "log_ratio - log_ratio_med"},
        {"name": "forecast_std", "expr": "row_std(ratio_daily)"},
        {"name": "cost_bin", "fit": "quantile_bin", "of": "cost", "q": Q},
    ],
}


# --- spec schema --------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    {"name": "a", "expr": "x", "fit": "median", "of": "x"},     # two kinds
    {"name": "a"},                                               # no kind
    {"name": "a", "expr": "x", "typo": 1},                       # extra="forbid"
    {"name": "a", "fit": "quantile_bin", "of": "x"},             # no q
    {"name": "a", "fit": "median", "of": "x", "q": Q},           # q on median
    {"name": "a", "expr": "x", "by": "platform"},                # fit param on expr
    {"name": "a", "custom": "mod:fn"},                           # no outputs
])
def test_spec_rejects_malformed_entries(entry):
    with pytest.raises(ValidationError):
        FeaturesConfig(define=[entry])



# --- compile ------------------------------------------------------------------------

def test_for_each_expands_names_and_exprs_and_keeps_cols_placeholders():
    plan = compile_spec({"vars": {"days": [1, 3, 7]}, "define": [
        {"name": "g_d{x}_d{y}", "expr": "val_day_{y}_cum / val_day_{x}_cum", "for_each": {"x,y": {"pairs": "days"}}},
        {"name": "s_{p}", "expr": "row_sum(cols('{p}_day_{d}', d=range(0, 2)))", "for_each": {"p": ["a", "b"]}},
    ]})
    assert plan.outputs == ("g_d1_d3", "g_d1_d7", "g_d3_d7", "s_a", "s_b")
    assert plan.raw_columns[-4:] == ("a_day_0", "a_day_1", "b_day_0", "b_day_1")


def test_plan_separates_frame_columns_from_features():
    plan = compile_spec(SPEC)
    assert set(plan.raw_columns) == {"val_day_3_cum", "val_day_1_cum", "cost", "platform",
                                     "val_day_0", "val_day_1", "val_day_2", "val_day_3"}
    assert plan.outputs == ("rev_growth_d1_d3", "ratio_alpha", "forecast_std", "cost_bin")
    assert plan.intermediates == ("log_ratio", "log_ratio_med")


@pytest.mark.parametrize("expr", [
    "np.exp(x)",                    # attribute access
    "__import__('os')",            # unknown call
    "(lambda: 1)()",               # lambda
    "x[0]",                         # subscript
    "row_sum(x)",                   # row reduction of a per-row vector
    "cols('a_{d}', d=[1]) + 'b'",   # string constant
])
def test_compile_rejects_anything_outside_the_op_whitelist(expr):
    with pytest.raises(ExprError):
        compile_spec({"define": [{"name": "f", "expr": expr}]})


@pytest.mark.parametrize("define, match", [
    ([{"name": "a", "expr": "b + 1"}, {"name": "b", "expr": "x * 2"}], "before they are declared"),
    ([{"name": "a", "expr": "x"}, {"name": "a", "expr": "y"}], "more than once"),
    ([{"name": "m", "expr": "cols('x_{d}', d=[0, 1])"}], "is a matrix"),
    ([{"name": "c", "expr": "1 + 2"}], "is a constant"),
])
def test_compile_rejects_bad_structure(define, match):
    with pytest.raises(ExprError, match=match):
        compile_spec({"define": define})


# --- fit / transform ----------------------------------------------------------------

def test_expressions_match_the_numpy_they_stand_for():
    df = _frame()
    out = FeaturePipeline(SPEC).fit_transform(df)
    forecast = df[[f"val_day_{d}" for d in range(4)]].to_numpy() / df["cost"].to_numpy()[:, None]
    np.testing.assert_allclose(out["rev_growth_d1_d3"],
                               (df["val_day_3_cum"] - df["val_day_1_cum"]) / (df["val_day_1_cum"] + 1e-4))
    np.testing.assert_allclose(out["forecast_std"], forecast.std(axis=1, ddof=0))
    log_ratio = np.log1p(forecast.sum(axis=1))
    med = pd.Series(log_ratio).groupby(df["platform"].to_numpy()).median()
    np.testing.assert_allclose(out["ratio_alpha"], log_ratio - df["platform"].map(med).to_numpy())


def test_output_keeps_input_columns_and_drops_intermediates():
    df = _frame()
    out = FeaturePipeline(SPEC).fit_transform(df)
    assert list(out.columns) == [*df.columns, "rev_growth_d1_d3", "ratio_alpha", "forecast_std", "cost_bin"]
    assert out.index.equals(df.index)


def test_global_pandas_output_config_leaves_the_frame_alone():
    from sklearn import config_context
    df = _frame()
    with config_context(transform_output="pandas"):
        out = FeaturePipeline(SPEC).fit(df).transform(df)
    assert list(out.columns)[:len(df.columns)] == list(df.columns)


def test_transform_before_fit_raises():
    with pytest.raises(NotFittedError):
        FeaturePipeline(SPEC).transform(_frame())


def test_fit_statistics_stay_frozen_and_unseen_groups_fall_back():
    train = _frame(seed=0)
    pipe = FeaturePipeline({"define": [
        {"name": "cost_med", "fit": "median", "of": "cost", "by": "platform"},
        {"name": "cost_bin", "fit": "quantile_bin", "of": "cost", "q": Q},
    ]}).fit(train)
    later = pd.DataFrame({"platform": ["ios", "web", "ios", "ios"], "cost": [1e6, 5.0, -1e6, np.nan]})
    out = pipe.transform(later)
    meds = train.groupby("platform")["cost"].median()
    assert out["cost_med"].tolist() == [meds["ios"], train["cost"].median(), meds["ios"], meds["ios"]]
    assert out["cost_bin"].tolist() == [4, 1, 1, 0]  # beyond either fit edge -> end bins; NaN -> 0


def test_target_statistics_need_y():
    df = _frame()
    spec = {"define": [{"name": "te", "fit": "mean", "of": "@target", "by": "platform"}]}
    y = np.arange(len(df), dtype=float)
    out = FeaturePipeline(spec).fit_transform(df, y)
    expected = pd.Series(y).groupby(df["platform"].to_numpy()).mean()
    np.testing.assert_allclose(out["te"], df["platform"].map(expected))
    with pytest.raises(ValueError, match="no y"):
        FeaturePipeline(spec).fit(df)


def test_a_row_scores_the_same_whatever_shares_its_batch():
    """Serving scores in chunks; every non-custom step must be row-independent."""
    pipe = FeaturePipeline(SPEC).fit(_frame(seed=0))
    a, b = _frame(seed=1), _frame(seed=2).set_axis(range(40, 80))
    together = pipe.transform(pd.concat([a, b]))
    apart = pd.concat([pipe.transform(a), pipe.transform(b)])
    pd.testing.assert_frame_equal(together, apart)


def _fit_transform(define, df, y=None):
    return FeaturePipeline({"define": define}).fit_transform(df, y)


def test_row_curve_ops_match_numpy_per_row():
    rng = np.random.default_rng(3)
    m = rng.uniform(0, 5, (30, 8))
    m[rng.uniform(size=m.shape) < 0.3] = 0
    df = pd.DataFrame(m, columns=[f"v_{d}" for d in range(8)])
    out = _fit_transform([
        {"name": "M", "expr": "cols('v_{d}', d=range(0, 8))", "keep": False},
        {"name": "slope", "expr": "row_slope(M)"},
        {"name": "quad", "expr": "row_polyfit(M, deg=2, k=2)"},
        {"name": "resid_energy", "expr": "row_mean(row_detrend(M) ** 2)"},
        {"name": "gini", "expr": "row_gini(M)"},
        {"name": "rises", "expr": "row_sum(row_diff(M) > 0)"},
        {"name": "head", "expr": "row_sum(col_slice(M, stop=3))"},
    ], df)
    x = np.arange(8)
    fits = [np.polyfit(x, row, 2) for row in m]
    lines = [np.polyfit(x, row, 1) for row in m]
    np.testing.assert_allclose(out["slope"], [b for b, _ in lines], atol=1e-12)
    np.testing.assert_allclose(out["quad"], [c[0] for c in fits], atol=1e-12)
    np.testing.assert_allclose(out["resid_energy"],
                               [np.mean((row - np.polyval(fit, x)) ** 2) for row, fit in zip(m, lines)], atol=1e-12)
    srt = np.sort(m, axis=1)
    gini = 2 * (srt * np.arange(1, 9)).sum(1) / (8 * srt.sum(1)) - 9 / 8
    np.testing.assert_allclose(out["gini"], np.where(srt.sum(1) < 1e-12, 0, gini))
    np.testing.assert_array_equal(out["rises"], (np.diff(m, axis=1) > 0).sum(1))
    np.testing.assert_allclose(out["head"], m[:, :3].sum(1))


def test_grouped_fit_options():
    df = pd.DataFrame({"p": ["a"] * 6 + ["b"] * 2 + ["c"] * 4, "g": ["x", "y"] * 6,
                       "v": np.arange(12, dtype=float)})
    y = np.arange(12, dtype=float) * 10
    out = _fit_transform([
        {"name": "iqr_pg", "fit": "iqr", "of": "v", "by": ["p", "g"], "min_count": 2, "default": "nan"},
        {"name": "iqr_p", "fit": "iqr", "of": "v", "by": "p", "default": "table_mean"},
        {"name": "te", "fit": "mean", "of": "@target", "by": "p", "smoothing": 2},
        {"name": "q90", "fit": "quantile", "of": "where(v > 3, v, nan)", "q": 0.9},
    ], df, y)
    g = df.assign(y=y).groupby("p")
    iqr = g["v"].quantile(0.75) - g["v"].quantile(0.25)
    assert out.loc[df.p == "a", "iqr_p"].unique().tolist() == [iqr["a"]]
    # (b, x) and (b, y) have one row each: under min_count, so NaN rather than a group IQR.
    assert out.loc[df.p == "b", "iqr_pg"].isna().all() and out.loc[df.p == "a", "iqr_pg"].notna().all()
    smooth = (g["y"].count() * g["y"].mean() + 2 * y.mean()) / (g["y"].count() + 2)
    np.testing.assert_allclose(out["te"], df.p.map(smooth))
    assert out["q90"].unique().tolist() == [np.quantile(np.arange(4, 12), 0.9)]
    unseen = FeaturePipeline({"define": [
        {"name": "iqr_p", "fit": "iqr", "of": "v", "by": "p", "default": "table_mean"}]}).fit(df)
    assert unseen.transform(df.assign(p="zz"))["iqr_p"].unique().tolist() == [iqr.mean()]


def test_missing_group_key_is_no_group_of_its_own():
    df = pd.DataFrame({"p": ["a", "a", None, None], "v": [1.0, 3.0, 100.0, 200.0]})
    out = _fit_transform([{"name": "med", "fit": "median", "of": "v", "by": "p"}], df)
    assert out["med"].tolist() == [2.0, 2.0, 51.5, 51.5]  # missing key -> overall median


def test_datetime_columns_read_as_days_since_epoch():
    df = pd.DataFrame({"d": pd.to_datetime(["2026-03-02", "2026-03-08", None])})
    out = _fit_transform([{"name": "weekday", "expr": "(d + 3) % 7"}], df)
    assert out["weekday"].iloc[:2].tolist() == [0.0, 6.0] and np.isnan(out["weekday"].iloc[2])


@pytest.mark.parametrize("entry, error", [
    ({"name": "a", "fit": "quantile", "of": "x", "q": [0.5]}, ValidationError),
    ({"name": "a", "fit": "median", "of": "x", "min_count": 3}, ValidationError),
    ({"name": "a", "fit": "mean", "of": "x", "by": "p", "q": 0.5}, ValidationError),
    ({"name": "a", "fit": "quantile_bin", "of": "@target", "q": Q}, ExprError),
    ({"name": "a", "fit": "median", "of": "cols('x_{d}', d=[0, 1])"}, ExprError),
])
def test_fit_entries_reject_params_their_stat_cannot_use(entry, error):
    with pytest.raises(error):
        compile_spec({"define": [entry]})


SCHEMA_SPEC = {"define": [
    {"name": "ratio_d7", "expr": "val_day_7 / maximum(cost, 1e-12)"},
    {"name": "forecast_med", "fit": "median", "of": "ratio_d7", "by": "platform"},
    {"name": "weekday", "expr": "(snapshot_date + 3) % 7"},
    {"name": "sib", "custom": "no_such_module:fn", "inputs": ["campaign"], "outputs": ["sib_n"]},
]}
LIVE = {"val_day_7": "FLOAT64", "cost": "FLOAT64", "platform": "STRING",
        "snapshot_date": "DATE", "campaign": "STRING"}


def test_plan_tells_columns_computed_with_from_group_keys_and_custom_inputs():
    plan = compile_spec(SCHEMA_SPEC, resolve_custom=False)
    assert set(plan.raw_columns) == set(LIVE)
    assert plan.numeric_columns == ("val_day_7", "cost", "snapshot_date")


@pytest.mark.parametrize("schema, problems", [
    (LIVE, []),
    ({**LIVE, "cost": "STRING"}, ["read as numbers but not numeric: ['cost is STRING (read by ratio_d7)']"]),
    ({k: v for k, v in LIVE.items() if k != "campaign"}, ["not in the feature table: ['campaign (read by sib)']"]),
])
def test_schema_problems_name_the_column_and_the_feature_reading_it(schema, problems):
    assert schema_problems(compile_spec(SCHEMA_SPEC, resolve_custom=False), schema) == problems


def test_a_string_column_is_blamed_on_the_feature_computing_with_it():
    spec = {"define": [*SCHEMA_SPEC["define"], {"name": "oops", "expr": "platform * 2"}]}
    assert schema_problems(compile_spec(spec, resolve_custom=False), LIVE) == [
        "read as numbers but not numeric: ['platform is STRING (read by oops)']"]


def test_a_string_column_a_submodel_rule_reads_is_blamed_on_the_rule():
    spec = {"define": [{"name": "f", "expr": "x * 2"}],
            "submodels": {"a": {"when": "c > 1", "inputs": ["f"]}}}
    assert schema_problems(compile_spec(spec), {"x": "FLOAT64", "c": "STRING"}) == [
        "read as numbers but not numeric: [\"c is STRING (read by submodel a's when)\"]"]


def test_compiling_needs_no_sklearn_and_no_ibis():
    """A spec compiles where neither is installed, e.g. a project's CI runner."""
    script = textwrap.dedent("""
        import sys
        sys.modules["sklearn"] = sys.modules["ibis"] = None  # any import of them now fails
        from gcpbricks.feature_manager import compile_spec
        compile_spec({"define": [{"name": "f", "expr": "a + b"}]})
        compile_spec({"transforms": [{"name": "f", "formula": "log1p(a) / b"}]})
    """)
    subprocess.run([sys.executable, "-c", script], check=True)


def test_bigquery_date_columns_read_as_days():
    pytest.importorskip("db_dtypes")
    df = pd.DataFrame({"d": pd.Series(["2026-03-02", "2026-03-08"], dtype="dbdate")})
    assert _fit_transform([{"name": "weekday", "expr": "(d + 3) % 7"}], df)["weekday"].tolist() == [0.0, 6.0]


def _days_frame(seed=5, n=40, days=(1, 3, 7, 10, 14)):
    rng = np.random.default_rng(seed)
    m = rng.uniform(0, 5, (n, len(days)))
    m[rng.uniform(size=m.shape) < 0.25] = 0
    return pd.DataFrame(m, columns=[f"v_{d}" for d in days]), m, np.array(days, float)


def test_curve_fits_take_the_days_as_x():
    df, m, x = _days_frame()
    out = FeaturePipeline({"vars": {"days": [1, 3, 7, 10, 14]}, "define": [
        {"name": "M", "expr": "cols('v_{d}', d=days)", "keep": False},
        {"name": "slope", "expr": "row_slope(M, x=days)"},
        {"name": "slope_eps", "expr": "row_slope(M, x=days, eps=0.5)"},
        {"name": "resid", "expr": "row_mean(row_detrend(M, x=[1, 3, 7, 10, 14]) ** 2)"},
        {"name": "r2", "expr": "row_polyfit_r2(log(M + 1), deg=2, x=days)"},
    ]}).fit_transform(df)
    xc = x - x.mean()
    np.testing.assert_allclose(out["slope"], [np.polyfit(x, r, 1)[0] for r in m], atol=1e-12)
    np.testing.assert_allclose(out["slope_eps"], (m - m.mean(1, keepdims=True)) @ xc / (xc @ xc + 0.5))
    np.testing.assert_allclose(out["resid"], [np.mean((r - np.polyval(np.polyfit(x, r, 1), x)) ** 2) for r in m],
                               atol=1e-12)
    y = np.log(m + 1)
    a = np.vander(x, 3, increasing=True)
    fit = (y @ a @ np.linalg.inv(a.T @ a)) @ a.T
    ss_tot = ((y - y.mean(1, keepdims=True)) ** 2).sum(1)
    expected = np.where(ss_tot > 1e-12, 1 - ((y - fit) ** 2).sum(1) / np.maximum(ss_tot, 1e-12), 0)
    np.testing.assert_allclose(out["r2"], expected)


def test_argmax_float32_and_lookup():
    df, m, _ = _days_frame()
    df["geo"] = ["US", "JP", None, "ZZ"] * 10
    out = _fit_transform([
        {"name": "M", "expr": "cols('v_{d}', d=[1, 3, 7, 10, 14])", "keep": False},
        {"name": "first_max", "expr": "row_argmax(M)"},
        {"name": "last_active", "expr": "row_argmax(M > 0, last=1)"},
        {"name": "g32", "expr": "(float32(v_3) - float32(v_1)) / (float32(v_1) + 1e-4)"},
        {"name": "tier", "lookup": "geo", "table": {"US": 1, "JP": 2}, "otherwise": 3},
        {"name": "tier_nan", "lookup": "geo", "table": {"US": 1}},
    ], df)
    np.testing.assert_array_equal(out["first_max"], m.argmax(1))
    np.testing.assert_array_equal(out["last_active"], 4 - (m[:, ::-1] > 0).argmax(1))
    v1, v3 = m[:, 0].astype(np.float32), m[:, 1].astype(np.float32)
    assert out["g32"].dtype == np.float32
    np.testing.assert_array_equal(out["g32"], (v3 - v1) / (v1 + 1e-4))
    # A numpy float64 eps promotes the float32 difference: float64() states that.
    mixed = _fit_transform([{"name": "g", "expr": "(float32(v_3) - float32(v_1)) / (float64(float32(v_1)) + 1e-4)"}], df)
    np.testing.assert_array_equal(mixed["g"], (v3 - v1) / (v1 + np.float64(1e-4)))
    assert out["tier"].tolist()[:4] == [1.0, 2.0, 3.0, 3.0]
    assert out["tier_nan"].iloc[0] == 1.0 and out["tier_nan"].iloc[1:4].isna().all()


def test_rank_code_and_yeo_johnson_match_their_hand_written_forms():
    from sklearn.preprocessing import PowerTransformer
    rng = np.random.default_rng(6)
    train = pd.DataFrame({"p": rng.choice(["ios", "android"], 50), "v": rng.normal(0, 1, 50),
                          "c": rng.choice(["b", "a", "c"], 50)})
    later = pd.DataFrame({"p": ["ios", "web", "android"], "v": [0.1, 0.2, -3.0], "c": ["a", "zz", None]})
    pipe = FeaturePipeline({"define": [
        {"name": "rank_p", "fit": "rank", "of": "v", "by": "p"},
        {"name": "rank_all", "fit": "rank", "of": "v", "side": "left"},
        {"name": "c_code", "fit": "code", "of": "c"},
        {"name": "v_yj", "fit": "yeo_johnson", "of": "v"},
    ]}).fit(train)
    out = pipe.transform(later)
    grids = {p: np.sort(train.v[train.p == p]) for p in train.p.unique()}
    overall = np.sort(train.v)
    expected = [np.searchsorted(grids.get(p, overall), v, side="right") / len(grids.get(p, overall))
                for p, v in zip(later.p, later.v)]
    np.testing.assert_allclose(out["rank_p"], expected)
    np.testing.assert_allclose(out["rank_all"], np.searchsorted(overall, later.v, side="left") / 50)
    assert out["c_code"].tolist() == [0.0, -1.0, -1.0]
    pt = PowerTransformer(method="yeo-johnson", standardize=False).fit(train[["v"]])
    np.testing.assert_allclose(out["v_yj"], pt.transform(later[["v"]])[:, 0])


def test_missing_key_group_makes_nan_a_group_of_its_own():
    df = pd.DataFrame({"g": ["a", "a", None, None], "v": [1.0, 3.0, 100.0, 200.0]})
    out = _fit_transform([{"name": "m", "fit": "mean", "of": "v", "by": "g", "missing_key": "group"}], df)
    assert out["m"].tolist() == [2.0, 2.0, 150.0, 150.0]


def test_rank_leaves_missing_values_out_of_the_grid_and_unranked():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0, np.nan]})
    out = _fit_transform([{"name": "r", "fit": "rank", "of": "v"}], df)
    np.testing.assert_allclose(out["r"], [1 / 3, 2 / 3, 1.0, np.nan])


def test_a_feature_may_not_overwrite_an_input_column():
    with pytest.raises(ValueError, match="overwrite"):
        FeaturePipeline({"define": [{"name": "cost", "expr": "val_day_0 * 2"}]}).fit_transform(_frame())


def _add(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"r0_plus_r1": df["val_day_0"] + df["val_day_1"]}, index=df.index)


def _bad_index(df: pd.DataFrame) -> pd.DataFrame:
    return _add(df).reset_index(drop=True).set_axis(range(1, len(df) + 1))


def test_custom_steps_feed_later_expressions_and_must_keep_the_index():
    spec = {"define": [
        {"name": "addition", "custom": f"{__name__}:_add", "inputs": ["val_day_0", "val_day_1"],
         "outputs": ["r0_plus_r1"], "keep": False},
        {"name": "doubled", "expr": "r0_plus_r1 * 2"},
    ]}
    df = _frame()
    pipe = FeaturePipeline(spec)
    assert {"val_day_0", "val_day_1"} <= set(pipe.plan().raw_columns)
    out = pipe.fit_transform(df)
    np.testing.assert_allclose(out["doubled"], 2 * (df["val_day_0"] + df["val_day_1"]))
    assert "r0_plus_r1" not in out
    spec["define"][0]["custom"] = f"{__name__}:_bad_index"
    with pytest.raises(ValueError, match="index"):
        FeaturePipeline(spec).fit_transform(df)


def test_pickled_by_value_it_scores_without_importing_feature_manager(tmp_path):
    """How a project stores an artifact: an older version is scored on a newer release, so
    the fitted pipeline must not re-import this package."""
    df = _frame()
    pipe = FeaturePipeline(SPEC).fit(df)
    cloudpickle.register_pickle_by_value(feature_manager)
    try:
        (tmp_path / "pipe.pkl").write_bytes(cloudpickle.dumps(pipe))
    finally:
        cloudpickle.unregister_pickle_by_value(feature_manager)
    df.to_pickle(tmp_path / "df.pkl")
    script = textwrap.dedent("""
        import sys, cloudpickle, pandas as pd
        pipe = cloudpickle.loads(open("pipe.pkl", "rb").read())
        assert "gcpbricks.feature_manager" not in sys.modules
        pipe.transform(pd.read_pickle("df.pkl")).to_pickle("out.pkl")
    """)
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True, env={"PATH": ""})
    pd.testing.assert_frame_equal(pd.read_pickle(tmp_path / "out.pkl"), pipe.transform(df))

INPUTS_SPEC = {
    "vars": {"days": [1, 3]},
    "define": [{"name": "forecast", "expr": "val_day_3_cum / cost"},
               {"name": "forecast_raw", "expr": "val_day_1_cum / cost", "keep": False}],
    "inputs": {"common": ["cost", {"each": "val_day_{d}_cum", "d": "days"}],
               "up": ["+common", "forecast"],
               "low": ["forecast", "+common", "val_day_0"]},
    "fetch": ["platform", "cost"],
}


def test_inputs_resolve_to_ordered_sets_and_drive_the_fetch():
    plan = compile_spec(INPUTS_SPEC)
    assert plan.inputs == {"common": ("cost", "val_day_1_cum", "val_day_3_cum"),
                           "up": ("cost", "val_day_1_cum", "val_day_3_cum", "forecast"),
                           "low": ("forecast", "cost", "val_day_1_cum", "val_day_3_cum", "val_day_0")}
    # What the defines read, then raw columns the sets pass through, then `fetch`; once each.
    assert plan.fetch_columns == ("val_day_3_cum", "cost", "val_day_1_cum", "val_day_0", "platform")
    assert feature_manager.fetch_columns(INPUTS_SPEC) == list(plan.fetch_columns)


def test_inputs_come_from_the_fitted_plan():
    pipe = FeaturePipeline(INPUTS_SPEC)
    with pytest.raises(NotFittedError):
        pipe.inputs("up")
    feat = pipe.fit_transform(_frame())
    assert list(feat[pipe.inputs("up")].columns) == ["cost", "val_day_1_cum", "val_day_3_cum", "forecast"]
    with pytest.raises(KeyError, match="declared"):
        pipe.inputs("nope")


@pytest.mark.parametrize("inputs, error", [
    ({"a": ["forecast_raw"]}, "keep: false"),
    ({"a": ["+b"], "b": ["+a"]}, "includes itself"),
    ({"a": ["+missing"]}, "no set 'missing'"),
    ({"a": [{"each": "val_day_{d}_cum"}]}, "needs `each` and an axis"),
    ({"a": [{"each": "rev_{x}_day_{d}", "d": [1]}]}, "no axis fills"),
    ({"outputs": ["cost"]}, "reserved"),
])
def test_bad_inputs_fail_at_compile(inputs, error):
    with pytest.raises(ExprError, match=error):
        compile_spec({**INPUTS_SPEC, "inputs": inputs})


def test_plus_outputs_is_every_kept_feature_in_declaration_order():
    spec = {**INPUTS_SPEC, "inputs": {"default": ["+outputs", "cost"]}}
    assert compile_spec(spec).inputs == {"default": ("forecast", "cost")}


GROUP_SPEC = {
    "vars": {"days": [1, 3]},
    "define": [
        {"name": "{p}_ratio_{d}", "expr": "val_day_{d}_cum / cost", "group": "ratios_{p}",
         "for_each": {"p": ["rev"], "d": "days"}},
        {"name": "forecast", "expr": "val_day_3_cum / cost", "group": "ratios_rev"},
        {"name": "hidden", "expr": "val_day_1_cum * 2", "group": "ratios_rev", "keep": False},
        {"name": "lone", "expr": "cost * 2"},
    ],
    "inputs": {"default": ["+group:ratios_rev", "cost"]},
}


def test_plus_group_is_its_kept_features_in_declaration_order():
    plan = compile_spec(GROUP_SPEC)
    assert plan.groups == {"ratios_rev": ("rev_ratio_1", "rev_ratio_3", "forecast")}
    assert plan.inputs["default"] == ("rev_ratio_1", "rev_ratio_3", "forecast", "cost")


def test_an_unknown_group_fails_at_compile():
    with pytest.raises(ExprError, match="no define group 'nope'"):
        compile_spec({**GROUP_SPEC, "inputs": {"default": ["+group:nope"]}})


def test_an_inputs_set_is_an_ordered_union():
    """Overlapping groups and sets compose: a repeated column keeps its first position."""
    spec = {**GROUP_SPEC, "inputs": {"common": ["forecast", "cost"],
                                     "low": ["+common", "+group:ratios_rev", "cost"]}}
    assert compile_spec(spec).inputs["low"] == ("forecast", "cost", "rev_ratio_1", "rev_ratio_3")


SUBMODEL_SPEC = {
    "define": [{"name": "forecast", "expr": "val_day_3_cum / cost"},
               {"name": "scratch", "expr": "cost * 2", "keep": False}],
    "inputs": {"common": ["forecast"]},
    "submodels": {
        "big": {"when": "cost >= 50", "inputs": ["+common", "cost"], "params": {"max_depth": 6}},
        "small": {"when": "(cost > 0) & (cost < 50)", "inputs": ["+common"]},
    },
}


def test_submodels_route_rows_and_carry_their_inputs_and_params():
    pipe = FeaturePipeline(SUBMODEL_SPEC)
    feat = pipe.fit_transform(_frame())
    assert pipe.submodels() == ["big", "small"]
    assert pipe.inputs("big") == ["forecast", "cost"] and pipe.inputs("small") == ["forecast"]
    assert pipe.params("big") == {"max_depth": 6} and pipe.params("small") == {}
    owner = pipe.route(feat)
    assert (owner[feat["cost"] >= 50] == "big").all()
    assert (owner[(feat["cost"] > 0) & (feat["cost"] < 50)] == "small").all()


def test_a_row_no_rule_matches_is_unrouted_and_one_two_rules_match_raises():
    pipe = FeaturePipeline(SUBMODEL_SPEC)
    feat = pipe.fit_transform(_frame())
    feat.loc[feat.index[0], "cost"] = np.nan
    assert pipe.route(feat).iloc[0] is None
    overlapping = {**SUBMODEL_SPEC, "submodels": {
        "a": {"when": "cost >= 0", "inputs": ["forecast"]}, "b": {"when": "cost >= 50", "inputs": ["forecast"]}}}
    pipe = FeaturePipeline(overlapping)
    with pytest.raises(ValueError, match="match submodel 'b' and an earlier one"):
        pipe.route(pipe.fit_transform(_frame()))


def test_a_rule_reads_raw_columns_into_the_fetch():
    spec = {**SUBMODEL_SPEC, "submodels": {"a": {"when": "signups > 0", "inputs": ["forecast"]}}}
    plan = compile_spec(spec)
    assert "signups" in plan.fetch_columns and "signups" in plan.numeric_columns


@pytest.mark.parametrize("submodels, inputs, error", [
    ({"common": {"when": "cost > 0", "inputs": ["forecast"]}}, {"common": ["forecast"]}, "share a name"),
    ({"a": {"when": "scratch > 0", "inputs": ["forecast"]}}, {}, "keep: false"),
    ({"a": {"when": "1 > 0", "inputs": ["forecast"]}}, {}, "one value per row"),
])
def test_bad_submodels_fail_at_compile(submodels, inputs, error):
    with pytest.raises(ExprError, match=error):
        compile_spec({**SUBMODEL_SPEC, "inputs": inputs, "submodels": submodels})


def test_inputs_may_not_read_a_matrix_feature():
    spec = {"define": [{"name": "m", "expr": "cols('x{d}', d=[1, 2])", "keep": False},
                       {"name": "s", "expr": "row_sum(m)"}],
            "inputs": {"default": ["m", "s"]}}
    with pytest.raises(ExprError, match="keep: false"):
        compile_spec(spec)

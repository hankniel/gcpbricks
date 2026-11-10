"""A features.yaml that states its spine and decision runs from the spec alone; the two
mistakes that would compute silently fail; explain() says what each feature is."""
from __future__ import annotations

import copy

import numpy as np
import pytest

pytest.importorskip("ibis")
pytest.importorskip("duckdb")
pd = pytest.importorskip("pandas")

from test_feature_landmarks import KEY, ROLLING, SPEC, _training, data  # noqa: F401

from gcpbricks.feature_manager import ExprError, compile_spec, explain, preview

ROLLING_SPEC = {**copy.deepcopy(ROLLING), "spine": "lng", "decision": {"at_days": 0}}


def test_a_spec_with_its_spine_runs_from_the_spec_alone(data):  # noqa: F811
    fs, _, _ = data
    plan, want = _training(fs, fs.get_feature_view("fs_c").spine())
    spec_plan = compile_spec({**SPEC, "spine": "fs_c"})
    got = fs.training_set_from_spec(spec_plan).load_df().sort_values(KEY, ignore_index=True)
    assert len(got) == len(want)
    names = {plan.sql_prefix + c[len(spec_plan.sql_prefix):]: c for c in got if c.startswith(spec_plan.sql_prefix)}
    for c, d in names.items():
        np.testing.assert_allclose(got[d].astype(float), want[c].astype(float), equal_nan=True, err_msg=c)
    for c in ("rev_cum", "rev_after"):
        np.testing.assert_allclose(got[c].astype(float), want[c].astype(float), equal_nan=True)

    key = ["segment", "snapshot_date", "age"]
    spec_plan, by_python = compile_spec(ROLLING_SPEC), compile_spec(ROLLING)
    rolling = fs.training_set_from_spec(spec_plan).load_df().sort_values(key, ignore_index=True)
    bfv = fs.batch_feature_view("r", "lng", by_python.sql_stage(), at_days=0)
    want = (fs.create_training_set(fs.get_feature_view("lng").spine(), [bfv]).load_df()
            .sort_values(key, ignore_index=True))
    assert len(rolling) == len(want)
    for c in ("rev_sum_3d", "rev_trend"):
        np.testing.assert_allclose(rolling[spec_plan.sql_prefix + c].astype(float),
                                   want[by_python.sql_prefix + c].astype(float), err_msg=c)


def test_the_spine_as_source_needs_over_stated():
    spec = copy.deepcopy(ROLLING_SPEC)
    del spec["aggregations"][0]["over"]
    with pytest.raises(ExprError, match="its source is the spine"):
        compile_spec(spec)
    spec["aggregations"][0]["over"] = []
    compile_spec(spec)


def test_the_spine_as_source_needs_over_stated_in_python_too(data):  # noqa: F811
    fs, _, _ = data
    spec = copy.deepcopy(ROLLING)
    del spec["aggregations"][0]["over"]
    with pytest.raises(ExprError, match="its source is the spine"):
        fs.batch_feature_view("m", "lng", compile_spec(spec).sql_stage(), at_days=0)


def test_an_age_window_past_the_decision_day_fails(data):  # noqa: F811
    spec = {"spine": "fs_c", "decision": {"at_days": 2},
            "aggregations": [{"name": "d3", "source": "lng", "input": "rev", "function": "sum",
                              "time_window": {"from_day": 0, "to_day": 3}, "over": ["age"]}]}
    with pytest.raises(ExprError, match="past the decision day"):
        compile_spec(spec)
    fs, _, _ = data
    del spec["spine"], spec["decision"]
    with pytest.raises(ExprError, match="past the decision day"):
        fs.batch_feature_view("m", "fs_c", compile_spec(spec).sql_stage(), at_days=2)


def test_the_spec_states_one_decision():
    with pytest.raises(ValueError, match="not both"):
        compile_spec({**SPEC, "decision": {"at_days": 3}})
    no_decision = {k: v for k, v in ROLLING_SPEC.items() if k != "decision"}
    with pytest.raises(ValueError, match="need `decision"):
        compile_spec(no_decision)


def test_landmark_days_as_a_range():
    spec = copy.deepcopy(SPEC)
    spec["aggregations"] = spec["aggregations"][2:]
    spec["transforms"] = []
    spec["landmarks"] = {"at": {"range": [0, 10, 3]}, "horizons": [12]}
    assert compile_spec(spec).landmarks["at"] == [0, 3, 6, 9]


def test_unset_fields_keep_an_older_specs_hash():
    """The hash names a spec's stored SQL columns; it was this before spine, decision,
    landmarks, labels and over existed."""
    spec = {"vars": {"eps": 1e-9},
            "aggregations": [{"source": "lng", "input": "rev", "function": "sum", "time_window": "3d"}],
            "transforms": [{"name": "r", "formula": "rev_sum_3d / maximum(cost, eps)"}]}
    assert compile_spec(spec).sql_hash == "ba88a8aea98e"
    assert compile_spec({**spec, "spine": "fs_c", "decision": {"at_days": 3}}).sql_hash != "ba88a8aea98e"


def test_explain_says_each_features_days():
    said = dict(explain({**SPEC, "spine": "fs_c"}))
    assert "decision day a in [1, 3] x horizon H in [5, 6]" in said["rows"]
    assert "sum of rev in lng, D(a-1)..Da" in said["rev_sum_2d"]
    assert said["val_d1"].startswith("max of rev in lng, D1,") and said["val_d1"].endswith("NULL")
    assert "D(a+1)..DH" in said["rev_after"]
    wide = dict(explain({"spine": "fs_c", "decision": {"at_days": 7}, "aggregations": [
        {"source": "lng", "input": "rev", "function": "sum", "time_window": "3d", "over": ["age"]}]}))
    assert "D5..D7" in wide["rev_sum_3d"] and wide["rev_sum_3d"].endswith("none there: 0")


def test_preview_drops_the_hash_prefix():
    df = pd.DataFrame({"__sql_0123456789ab__forecast": [1.0], "geo": ["us"],
                       "__sql_0123456789ab__x": [1.0], "__sql_ba9876543210__x": [2.0]})
    assert list(preview(df).columns) == ["forecast", "geo", "__sql_0123456789ab__x", "__sql_ba9876543210__x"]

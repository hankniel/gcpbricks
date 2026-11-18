"""The anomaly detector's SQL: TimesFM through AI.DETECT_ANOMALIES and Elementary's z-score,
their Training and Error statuses and the direction a point must leave its bounds in."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from gcpbricks.anomalies import AnomalyDetector, detect_sql

SERIES = "SELECT series_id, ts, value FROM points"


def test_timesfm_is_the_default_and_pinned():
    sql = detect_sql(SERIES, AnomalyDetector())
    assert "AI.DETECT_ANOMALIES(" in sql and "model => 'TimesFM 2.5'" in sql
    assert "target_start_timestamp => @cutoff" in sql and "anomaly_prob_threshold => 0.95" in sql
    assert "id_cols => ['series_id']" in sql


def test_a_series_short_of_history_is_training_and_never_an_anomaly():
    sql = detect_sql(SERIES, AnomalyDetector(min_history=10))
    assert "HAVING COUNT(*) >= 10" in sql and "WHEN r.series_id IS NULL THEN 'Training'" in sql
    assert "IF(b.status IS NULL, IFNULL(b.flagged AND" in sql
    z = detect_sql(SERIES, AnomalyDetector(model="zscore", min_history=10))
    assert "IF(n < 10 OR sd IS NULL, 'Training', NULL) AS status" in z


def test_the_model_saying_why_it_couldnt_judge_is_an_error():
    sql = detect_sql(SERIES, AnomalyDetector())
    assert "IFNULL(d.ai_detect_anomalies_status, '') != '' THEN 'Error'" in sql


@pytest.mark.parametrize(("direction", "outside"), [
    ("both", "(t.value < b.lower_bound OR t.value > b.upper_bound)"),
    ("drop", "t.value < b.lower_bound"), ("spike", "t.value > b.upper_bound")])
def test_a_point_is_an_anomaly_outside_its_bounds_in_the_direction_asked(direction, outside):
    sql = detect_sql(SERIES, AnomalyDetector(direction=direction))
    assert f"IFNULL(b.flagged AND {outside}, FALSE)" in sql


def test_zscore_reads_the_last_training_points_of_its_week_phase():
    sql = detect_sql(SERIES, AnomalyDetector(model="zscore", seasonality="day_of_week", training_points=8,
                                             sensitivity=2.5))
    assert "AI.DETECT_ANOMALIES" not in sql
    assert "PARTITION BY series_id, MOD(DIV(UNIX_SECONDS(ts), 60), 10080) ORDER BY ts DESC" in sql
    assert "r.phase = MOD(DIV(UNIX_SECONDS(t.ts), 60), 10080) AND r.k <= 8" in sql
    assert "mu - 2.5 * sd AS lower_bound, mu + 2.5 * sd AS upper_bound" in sql
    flat = detect_sql(SERIES, AnomalyDetector(model="zscore"))
    assert "r.phase = 0 AND r.k <= 14" in flat


def test_history_is_before_the_cutoff_and_null_values_are_no_points():
    sql = detect_sql(SERIES, AnomalyDetector(), cutoff="TIMESTAMP '2026-11-01'")
    assert "WHERE ts < TIMESTAMP '2026-11-01' AND value IS NOT NULL" in sql
    assert "WHERE ts >= TIMESTAMP '2026-11-01' AND value IS NOT NULL" in sql


def test_unknown_models_and_thresholds_are_refused():
    with pytest.raises(ValidationError):
        AnomalyDetector(model="prophet")
    with pytest.raises(ValidationError):
        AnomalyDetector(anomaly_prob_threshold=1)
    with pytest.raises(ValidationError):
        AnomalyDetector(min_history=2)

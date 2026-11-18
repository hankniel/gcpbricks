"""Anomaly detection on metric series, in BigQuery: the detector the quality monitors'
`anomaly_detection` and the schema monitors' completeness checks share.

A series is (series_id, ts, value), one point per window. Points before the cutoff are the
history, points from it the targets; each target gets an expected range (lower_bound,
upper_bound) from its series' history and is an anomaly outside it, in the direction asked:

- `TimesFM 2.5` / `TimesFM 3.0`: BigQuery's AI.DETECT_ANOMALIES, Google's pretrained
  forecasting model: no training, trend and seasonality learnt from the history itself; its
  anomaly_probability against anomaly_prob_threshold (a higher threshold, wider bounds).
- `zscore`: Elementary's, the mean ± sensitivity standard deviations of the last
  training_points history points (with seasonality day_of_week, only those a whole number of
  weeks before the target). No probability.

A series with fewer than min_history history points (zscore: comparable ones) is Training:
no bounds, never an anomaly.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MODELS = ("TimesFM 2.5", "TimesFM 3.0", "zscore")
#: Minutes in a week: day_of_week seasonality compares points a whole number of weeks apart.
WEEK_MINUTES = 7 * 24 * 60


class AnomalyDetector(BaseModel):
    """How a series' targets are judged: gcpbricks' own keys (Databricks publishes
    no model)."""

    model_config = ConfigDict(extra="forbid")

    model: Literal[MODELS] = "TimesFM 2.5"
    #: both: outside either bound; drop: under the lower; spike: over the upper.
    direction: Literal["both", "drop", "spike"] = "both"
    #: TimesFM's: a point is an anomaly when its anomaly_probability exceeds it.
    anomaly_prob_threshold: float = Field(default=0.95, ge=0, lt=1)
    #: zscore's: standard deviations from the mean that the bounds are.
    sensitivity: float = Field(default=3, gt=0)
    #: zscore's: history points the mean and deviation are of (Elementary's 14).
    training_points: int = Field(default=14, ge=2, le=1000)
    #: zscore's: compare a point only with those a whole number of weeks before it.
    seasonality: Literal["day_of_week"] | None = None
    #: History points a series needs before it is judged; until then it is Training.
    min_history: int = Field(default=7, ge=3, le=1000)


def _string(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def detect_sql(series: str, d: AnomalyDetector, cutoff: str = "@cutoff") -> str:
    """A query judging each target point of `series` (a query of series_id STRING, ts
    TIMESTAMP, value FLOAT64; one point per series and ts; a NULL value is no point): series_id, ts, value,
    lower_bound, upper_bound, anomaly_probability, is_anomaly and status (Healthy,
    Unhealthy, Training, Error). `cutoff` is a TIMESTAMP expression: points before it are
    the history, points from it the targets."""
    head = (f"WITH s AS (\n{series}\n),\n"
            "hist AS (SELECT series_id, ts, value FROM s WHERE ts < {cutoff} AND value IS NOT NULL),\n"
            "targets AS (SELECT series_id, ts, value FROM s WHERE ts >= {cutoff} AND value IS NOT NULL)"
            ).format(cutoff=cutoff)
    out = _timesfm(d, cutoff) if d.model != "zscore" else _zscore(d)
    if d.direction == "both":
        outside = "(t.value < b.lower_bound OR t.value > b.upper_bound)"
    elif d.direction == "drop":
        outside = "t.value < b.lower_bound"
    else:
        outside = "t.value > b.upper_bound"
    flagged = f"b.flagged AND {outside}"
    return (f"{head},\n{out}\n"
            "SELECT t.series_id, t.ts, t.value, b.lower_bound, b.upper_bound, b.anomaly_probability,\n"
            f"    IF(b.status IS NULL, IFNULL({flagged}, FALSE), FALSE) AS is_anomaly,\n"
            f"    IFNULL(b.status, IF(IFNULL({flagged}, FALSE), 'Unhealthy', 'Healthy')) AS status\n"
            "FROM targets AS t\nLEFT JOIN bounds AS b ON b.series_id = t.series_id AND b.ts = t.ts")


def _timesfm(d: AnomalyDetector, cutoff: str) -> str:
    """bounds from AI.DETECT_ANOMALIES over the series with min_history history points;
    status Training for the rest, Error where the model says why it couldn't judge."""
    return (
        f"ready AS (SELECT series_id FROM hist GROUP BY series_id HAVING COUNT(*) >= {d.min_history}),\n"
        "det AS (\n    SELECT * FROM AI.DETECT_ANOMALIES(\n"
        "        (SELECT s.series_id, s.ts, s.value FROM s JOIN ready USING (series_id) WHERE s.value IS NOT NULL),\n"
        "        data_col => 'value', timestamp_col => 'ts', id_cols => ['series_id'],\n"
        f"        target_start_timestamp => {cutoff}, model => {_string(d.model)},\n"
        f"        anomaly_prob_threshold => {d.anomaly_prob_threshold!r})\n),\n"
        "bounds AS (\n"
        "    SELECT t.series_id, t.ts, d.lower_bound, d.upper_bound, d.anomaly_probability,\n"
        "        IFNULL(d.is_anomaly, FALSE) AS flagged,\n"
        "        CASE WHEN r.series_id IS NULL THEN 'Training'\n"
        "             WHEN d.series_id IS NULL OR IFNULL(d.ai_detect_anomalies_status, '') != '' THEN 'Error'\n"
        "        END AS status\n"
        "    FROM targets AS t\n    LEFT JOIN ready AS r ON r.series_id = t.series_id\n"
        "    LEFT JOIN det AS d ON d.series_id = t.series_id AND d.time_series_timestamp = t.ts\n)")


def _zscore(d: AnomalyDetector) -> str:
    """bounds from the mean and deviation of each target's training points: the last
    training_points of its series' history, or with day_of_week seasonality the last
    training_points a whole number of weeks before it."""
    weekly = d.seasonality == "day_of_week"
    phase = f"MOD(DIV(UNIX_SECONDS(ts), 60), {WEEK_MINUTES})" if weekly else "0"
    return (
        f"ranked AS (\n    SELECT series_id, ts, value, {phase} AS phase,\n"
        f"        ROW_NUMBER() OVER (PARTITION BY series_id, {phase} ORDER BY ts DESC) AS k\n"
        "    FROM hist\n),\n"
        "stats AS (\n"
        f"    SELECT t.series_id, t.ts, AVG(r.value) AS mu, STDDEV_SAMP(r.value) AS sd, COUNT(r.value) AS n\n"
        "    FROM targets AS t\n"
        f"    LEFT JOIN ranked AS r ON r.series_id = t.series_id AND r.phase = {phase.replace('ts', 't.ts')}"
        f" AND r.k <= {d.training_points}\n"
        "    GROUP BY t.series_id, t.ts\n),\n"
        "bounds AS (\n"
        f"    SELECT series_id, ts, mu - {d.sensitivity!r} * sd AS lower_bound, mu + {d.sensitivity!r} * sd"
        " AS upper_bound,\n"
        "        CAST(NULL AS FLOAT64) AS anomaly_probability, TRUE AS flagged,\n"
        f"        IF(n < {d.min_history} OR sd IS NULL, 'Training', NULL) AS status\n"
        "    FROM stats\n)")

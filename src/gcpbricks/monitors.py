"""Databricks' data profiling (formerly Lakehouse Monitoring) on BigQuery: a bundle's
`resources.quality_monitors`, and the refresh that computes their metric tables.

    resources:
      quality_monitors:
        forecast:
          table_name: p.ml_monitoring.forecast_30d_inference     # predictions joined to labels
          output_schema_name: p.ml_monitoring
          baseline_table_name: p.ml_monitoring.forecast_30d_validation
          slicing_exprs: [country, "spend > 1000"]
          inference_log: {problem_type: PROBLEM_TYPE_REGRESSION, prediction_col: pred,
                          label_col: forecast_30d, model_id_col: model_id,
                          timestamp_col: snapshot_date, granularities: ["1 day", "1 week"]}
          schedule: {quartz_cron_expression: "0 0 6 * * ?", timezone_id: UTC}

A refresh writes `<output_schema>.<table>_profile_metrics` and `<table>_drift_metrics` with
Databricks' columns and keys. The scan runs in BigQuery: one query per granularity over the
lookback, every statistic of every column per window, model_id and slice (plus a
classification's prediction and label counts, and a classifier's probability counts),
staged in the output schema. One script then builds the profile rows (model quality, log
loss and ROC AUC, fairness, derived metrics), computes drift from their quantiles and
frequent items (JavaScript UDFs for the tests' statistics) and custom drift metrics, and
replaces what the refresh recomputed in both metric tables, in one transaction. No metric
row passes through the refresh's own process.

Each refresh recomputes every window of the lookback, so labels that arrive late fill in
their windows' model quality. Databricks reads only appended rows when change data feed is
on; BigQuery has none, and the monitored table is usually a view.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gcpbricks.anomalies import AnomalyDetector, detect_sql

#: Databricks' granularities, and how a window of each starts.
GRANULARITIES = ("5 minutes", "30 minutes", "1 hour", "1 day", "1 week", "2 weeks", "3 weeks",
                 "4 weeks", "1 month", "1 year")
#: Days a TimeSeries or InferenceLog refresh recomputes, as Databricks' 30-day lookback.
LOOKBACK_DAYS = 30
#: Quantiles kept per numeric column (Databricks keeps 1000; 100 keep the rows small).
QUANTILES = 100
#: Frequent items kept per column, as Databricks' top 100.
TOP_ITEMS = 100
#: Fixed-width windows count from a Monday at midnight UTC, so weeks start on Mondays.
ORIGIN = datetime(1970, 1, 5, tzinfo=UTC)
TABLE = ":table"
NUMERIC = {"INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
CATEGORICAL = {"STRING", "BOOL", "BOOLEAN"}
#: Profiled with counts and distinct values only.
OTHER = {"DATE", "DATETIME", "TIMESTAMP", "TIME", "BYTES"}
_IDENT = r"^[A-Za-z_][A-Za-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitorCronSchedule(_Strict):
    quartz_cron_expression: str
    timezone_id: str
    pause_status: Literal["PAUSED", "UNPAUSED"] | None = None


class MonitorDestination(_Strict):
    email_addresses: list[str] = Field(default=[], max_length=5)


class MonitorNotifications(_Strict):
    on_failure: MonitorDestination | None = None


def _granularities(v: list[str]) -> list[str]:
    bad = [g for g in v if g not in GRANULARITIES]
    if bad:
        raise ValueError(f"granularities {bad}: each one of {list(GRANULARITIES)}")
    if len(set(v)) != len(v):
        raise ValueError(f"granularities {v}: listed twice")
    return v


class MonitorInferenceLog(_Strict):
    problem_type: Literal["PROBLEM_TYPE_CLASSIFICATION", "PROBLEM_TYPE_REGRESSION"]
    prediction_col: str = Field(pattern=_IDENT)
    timestamp_col: str = Field(pattern=_IDENT)
    model_id_col: str = Field(pattern=_IDENT)
    granularities: list[str] = Field(min_length=1)
    label_col: str | None = Field(default=None, pattern=_IDENT)
    #: Each class label's probability: Databricks' map, as BigQuery holds one,
    #: ARRAY<STRUCT<key, value>> (key the label as a string, value its probability).
    prediction_proba_col: str | None = Field(default=None, pattern=_IDENT)

    @model_validator(mode="after")
    def _check(self):
        _granularities(self.granularities)
        if self.prediction_proba_col is not None and self.problem_type != "PROBLEM_TYPE_CLASSIFICATION":
            raise ValueError("inference_log.prediction_proba_col: a classifier's (log_loss, roc_auc_score)")
        return self


class MonitorTimeSeries(_Strict):
    timestamp_col: str = Field(pattern=_IDENT)
    granularities: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self):
        _granularities(self.granularities)
        return self


class MonitorSnapshot(_Strict):
    pass


_DRIFT_PLACEHOLDER = re.compile(r"\{\{\s*(current_df|base_df)\s*\}\}")
CUSTOM_TYPES = ("CUSTOM_METRIC_TYPE_AGGREGATE", "CUSTOM_METRIC_TYPE_DERIVED", "CUSTOM_METRIC_TYPE_DRIFT")
#: A custom metric's output_data_type (Spark's type name, or its StructField JSON) in BigQuery.
OUTPUT_TYPES = {"double": "FLOAT64", "float": "FLOAT64", "long": "INT64", "bigint": "INT64",
                "integer": "INT64", "int": "INT64", "string": "STRING", "boolean": "BOOL"}


class MonitorMetric(_Strict):
    type: Literal[CUSTOM_TYPES]
    name: str = Field(pattern=_IDENT)
    input_columns: list[str] = Field(min_length=1)
    definition: str
    output_data_type: str

    @model_validator(mode="after")
    def _check(self):
        self.bigquery_type  # noqa: B018 - checks it
        if self.type == "CUSTOM_METRIC_TYPE_DRIFT" and not _DRIFT_PLACEHOLDER.search(self.definition):
            raise ValueError(f"custom metric {self.name!r}: a drift metric's definition compares "
                             "{{current_df}} with {{base_df}}")
        return self

    @property
    def bigquery_type(self) -> str:
        t = self.output_data_type.strip()
        if t.startswith("{"):
            t = json.loads(t).get("type", "")
        if not isinstance(t, str) or t.lower() not in OUTPUT_TYPES:
            raise ValueError(f"custom metric {self.name!r}: output_data_type {self.output_data_type!r} "
                             f"is not one of {sorted(OUTPUT_TYPES)}")
        return OUTPUT_TYPES[t.lower()]


class MonitorAnomalyDetection(AnomalyDetector):
    """gcpbricks' own key (Databricks' anomaly detection is schema-level, row counts
    only): anomalies in the series of a monitor's profile metrics, one per granularity,
    model_id, slice, column and metric, judged after each refresh. A window missing from a
    `count` series (a slice with no rows that window) counts 0."""

    metrics: list[str] = Field(min_length=1)
    #: The profile rows watched: `:table` (rows, model quality, table custom metrics) or columns.
    columns: list[str] = Field(default=[":table"], min_length=1)
    #: The latest finished windows each refresh judges (Elementary's detection period).
    detection_windows: int = Field(default=2, ge=1, le=100)


class QualityMonitor(_Strict):
    """Databricks' quality monitor (its bundle resource's keys): BigQuery names in Unity
    Catalog's places, `project.dataset.table` and `project.dataset`."""

    table_name: str = Field(pattern=r"^[\w-]+\.\w+\.\w+$")
    output_schema_name: str = Field(pattern=r"^[\w-]+\.\w+$")
    #: Databricks' workspace folder for the monitor's dashboard; the console shows the
    #: metrics instead, so it is accepted and unused.
    assets_dir: str | None = None
    baseline_table_name: str | None = Field(default=None, pattern=r"^[\w-]+\.\w+\.\w+$")
    slicing_exprs: list[str] = []
    custom_metrics: list[MonitorMetric] = []
    inference_log: MonitorInferenceLog | None = None
    time_series: MonitorTimeSeries | None = None
    snapshot: MonitorSnapshot | None = None
    schedule: MonitorCronSchedule | None = None
    notifications: MonitorNotifications | None = None
    #: The console's Quality tab is the dashboard: there is none to skip.
    skip_builtin_dashboard: bool | None = None
    #: Days a refresh recomputes (LOOKBACK_DAYS, Databricks' fixed 30, when unset): longer
    #: for labels that arrive later than that. gcpbricks' own key, not Databricks'.
    lookback_days: int | None = Field(default=None, ge=1, le=366)
    anomaly_detection: MonitorAnomalyDetection | None = None

    @model_validator(mode="after")
    def _check(self):
        kinds = [k for k in ("inference_log", "time_series", "snapshot") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("a quality monitor takes one of inference_log, time_series or snapshot")
        names = [m.name for m in self.custom_metrics]
        if len(names) != len(set(names)):
            raise ValueError(f"custom_metrics: two share a name: {sorted(names)}")
        clash = sorted(set(names) & (set(PROFILE_COLUMNS) | set(DRIFT_COLUMNS)))
        if clash:
            raise ValueError(f"custom_metrics {clash}: a metric table already has that column")
        if len(set(self.slicing_exprs)) != len(self.slicing_exprs):
            raise ValueError(f"slicing_exprs {self.slicing_exprs}: listed twice")
        if self.lookback_days is not None and self.snapshot is not None:
            raise ValueError("lookback_days: a snapshot profiles the whole table, it has no lookback")
        if self.anomaly_detection:
            numeric = {n for n, t in PROFILE_COLUMNS.items() if t in ("INT64", "FLOAT64") and n not in _KEYS}
            numeric |= {cm.name for cm in self.custom_metrics if cm.type != "CUSTOM_METRIC_TYPE_DRIFT"
                        and cm.bigquery_type in ("INT64", "FLOAT64")}
            bad = [x for x in self.anomaly_detection.metrics if x not in numeric]
            if bad:
                raise ValueError(f"anomaly_detection.metrics {bad}: each a numeric profile metric or custom "
                                 f"metric, one of {sorted(numeric)}")
        return self

    @property
    def profile_type(self) -> str:
        return "InferenceLog" if self.inference_log else "TimeSeries" if self.time_series else "Snapshot"

    @property
    def timestamp_col(self) -> str | None:
        p = self.inference_log or self.time_series
        return p.timestamp_col if p else None

    @property
    def granularities(self) -> list[str]:
        p = self.inference_log or self.time_series
        return list(p.granularities) if p else []

    def output_tables(self) -> tuple[str, str]:
        table = self.table_name.rsplit(".", 1)[1]
        return (f"{self.output_schema_name}.{table}_profile_metrics",
                f"{self.output_schema_name}.{table}_drift_metrics")

    def anomalies_table(self) -> str | None:
        table = self.table_name.rsplit(".", 1)[1]
        return f"{self.output_schema_name}.{table}_anomalies" if self.anomaly_detection else None


# --- the refresh job a monitor becomes ----------------------------------------------------

def job_key(key: str) -> str:
    """The job a monitor's refresh runs as: on its schedule, by `mlops bundle run`, or from
    another job's run_job_task (Databricks' run_refresh at the end of a job)."""
    return f"{key}-monitor"


#: The label the refresh job carries: the console finds a monitor's runs by it.
LABEL = "mlops_monitor"


def config_param(raw: dict) -> str:
    """The monitor as the task's --config: JSON, its custom metrics' {{...}} placeholders
    escaped (JSON's \\u007b), which a job run would otherwise resolve as its own values."""
    return json.dumps(raw, sort_keys=True).replace("{{", "{\\u007b")


def refresh_job(key: str, raw: dict, image: str, *, project: str, bucket: str, state_object: str) -> dict:
    """The refresh job, as the bundle would declare it: one task running `mlops-task monitor`
    on the platform runtime. `raw` is the monitor as declared, `${...}` left for the loader."""
    m = raw or {}
    on_failure = ((m.get("notifications") or {}).get("on_failure") or {}).get("email_addresses") or []
    task = {"task_key": "refresh", "job_cluster_key": "refresh",
            "python_wheel_task": {"package_name": "gcpbricks", "entry_point": "mlops-task",
                                  "parameters": ["monitor", "--key", key, "--project", project, "--bucket", bucket,
                                                 "--state-object", state_object,
                                                 "--config", config_param(m)]}}
    job = {"name": f"Quality monitor: {m.get('table_name', key)}",
           "description": f"Refreshes quality monitor {key}'s profile and drift metrics.",
           "tags": {LABEL: key.replace("_", "-")},
           "job_clusters": [{"job_cluster_key": "refresh",
                             "new_cluster": {"docker_image": {"url": image}, "cpu": 1, "memory": "2Gi"}}],
           "tasks": [task]}
    if m.get("schedule"):
        job["schedule"] = m["schedule"]
    if on_failure:
        job["email_notifications"] = {"on_failure": on_failure}
    return job


# --- the metric tables --------------------------------------------------------------------

_WINDOW = ("RECORD", [("start", "TIMESTAMP"), ("end", "TIMESTAMP")])
_TEST = ("RECORD", [("statistic", "FLOAT64"), ("pvalue", "FLOAT64")])
_PER_CLASS = ("RECORD", [("one_vs_all", ("REPEATED", [("key", "STRING"), ("value", "FLOAT64")])),
                         ("macro", "FLOAT64"), ("weighted", "FLOAT64")])
_ONE_VS_ALL = ("REPEATED", [("key", "STRING"), ("value", "FLOAT64")])
_KEYS = {"window": _WINDOW, "granularity": "STRING", "model_id": "STRING", "log_type": "STRING",
         "slice_key": "STRING", "slice_value": "STRING", "column_name": "STRING",
         "data_type": "STRING", "logging_table_commit_version": "INT64", "monitor_version": "INT64"}
PROFILE_COLUMNS: dict[str, object] = {
    **_KEYS,
    "count": "INT64", "num_nulls": "INT64", "avg": "FLOAT64",
    "quantiles": ("REPEATED", "FLOAT64"), "distinct_count": "INT64", "min": "FLOAT64",
    "max": "FLOAT64", "stddev": "FLOAT64", "num_zeros": "INT64", "num_nan": "INT64",
    "min_size": "FLOAT64", "max_size": "FLOAT64", "avg_size": "FLOAT64",
    "min_len": "FLOAT64", "max_len": "FLOAT64", "avg_len": "FLOAT64",
    "frequent_items": ("REPEATED", [("item", "STRING"), ("count", "INT64")]),
    "non_null_columns": ("REPEATED", "STRING"), "median": "FLOAT64",
    "percent_null": "FLOAT64", "percent_zeros": "FLOAT64", "percent_distinct": "FLOAT64",
    "accuracy_score": "FLOAT64",
    "confusion_matrix": ("REPEATED", [("prediction", "STRING"), ("label", "STRING"), ("count", "INT64")]),
    "precision": _PER_CLASS, "recall": _PER_CLASS, "f1_score": _PER_CLASS,
    "mean_squared_error": "FLOAT64", "root_mean_squared_error": "FLOAT64",
    "mean_average_error": "FLOAT64", "mean_absolute_percentage_error": "FLOAT64", "r2_score": "FLOAT64",
    "log_loss": "FLOAT64",
    "roc_auc_score": ("RECORD", [("one_vs_one", [("weighted", "FLOAT64"), ("macro", "FLOAT64")])]),
    "predictive_parity": _ONE_VS_ALL, "predictive_equality": _ONE_VS_ALL, "equal_opportunity": _ONE_VS_ALL,
    "statistical_parity": _ONE_VS_ALL,
}
DRIFT_COLUMNS: dict[str, object] = {
    "window": _WINDOW, "window_cmp": _WINDOW, "drift_type": "STRING", "granularity": "STRING",
    "model_id": "STRING", "slice_key": "STRING", "slice_value": "STRING", "column_name": "STRING",
    "data_type": "STRING", "monitor_version": "INT64",
    "count_delta": "FLOAT64", "avg_delta": "FLOAT64", "percent_null_delta": "FLOAT64",
    "percent_zeros_delta": "FLOAT64", "percent_distinct_delta": "FLOAT64",
    "non_null_columns_delta": ("RECORD", [("added", "INT64"), ("missing", "INT64")]),
    "chi_squared_test": _TEST, "ks_test": _TEST, "tv_distance": "FLOAT64",
    "l_infinity_distance": "FLOAT64", "js_distance": "FLOAT64", "wasserstein_distance": "FLOAT64",
    "population_stability_index": "FLOAT64",
}


ANOMALY_COLUMNS: dict[str, object] = {
    "window": _WINDOW, "granularity": "STRING", "model_id": "STRING", "slice_key": "STRING",
    "slice_value": "STRING", "column_name": "STRING", "metric": "STRING", "value": "FLOAT64",
    "lower_bound": "FLOAT64", "upper_bound": "FLOAT64", "anomaly_probability": "FLOAT64",
    "is_anomaly": "BOOL", "status": "STRING", "detector": "STRING", "monitor_version": "INT64",
}


# --- windows ------------------------------------------------------------------------------

_FIXED = {"5 minutes": timedelta(minutes=5), "30 minutes": timedelta(minutes=30),
          "1 hour": timedelta(hours=1), "1 day": timedelta(days=1), "1 week": timedelta(weeks=1),
          "2 weeks": timedelta(weeks=2), "3 weeks": timedelta(weeks=3), "4 weeks": timedelta(weeks=4)}
_BQ_INTERVAL = {"5 minutes": "5 MINUTE", "30 minutes": "30 MINUTE", "1 hour": "1 HOUR",
                "1 day": "1 DAY", "1 week": "7 DAY", "2 weeks": "14 DAY", "3 weeks": "21 DAY",
                "4 weeks": "28 DAY"}


def window_floor(ts: datetime, granularity: str) -> datetime:
    """The start of the window `ts` falls in (UTC), as window_sql computes it."""
    if granularity in _FIXED:
        width = _FIXED[granularity]
        return ORIGIN + ((ts - ORIGIN) // width) * width
    if granularity == "1 month":
        return datetime(ts.year, ts.month, 1, tzinfo=UTC)
    return datetime(ts.year, 1, 1, tzinfo=UTC)


def window_add(start: datetime, granularity: str, n: int = 1) -> datetime:
    if granularity in _FIXED:
        return start + n * _FIXED[granularity]
    if granularity == "1 month":
        months = start.year * 12 + start.month - 1 + n
        return datetime(months // 12, months % 12 + 1, 1, tzinfo=UTC)
    return datetime(start.year + n, 1, 1, tzinfo=UTC)


def window_sql(ts: str, granularity: str) -> str:
    """BigQuery's start of the window a TIMESTAMP expression falls in."""
    if granularity in _BQ_INTERVAL:
        return (f"TIMESTAMP_BUCKET({ts}, INTERVAL {_BQ_INTERVAL[granularity]}, "
                f"TIMESTAMP '{ORIGIN:%Y-%m-%d %H:%M:%S}+00')")
    return f"TIMESTAMP_TRUNC({ts}, {'MONTH' if granularity == '1 month' else 'YEAR'})"


def scan_start(now: datetime, granularity: str, lookback_days: int = LOOKBACK_DAYS) -> datetime:
    """Where a refresh starts reading: the window holding now - lookback, and one before it,
    so the lookback's first window has a consecutive window to drift from."""
    return window_add(window_floor(now - timedelta(days=lookback_days), granularity), granularity, -1)

# --- the scan -----------------------------------------------------------------------------

def q(name: str) -> str:
    return f"`{name}`"


class Column(BaseModel):
    name: str
    type: str  # BigQuery's

    @property
    def kind(self) -> str:
        t = self.type.upper()
        return "numeric" if t in NUMERIC else "categorical" if t in CATEGORICAL else "other"


def profiled_columns(fields: list[tuple[str, str, str]], skip: set[str]) -> list[Column]:
    """(name, type, mode) of a table's top-level fields: the scalar ones it profiles. Records
    and arrays are left out (Databricks' size statistics are not computed)."""
    return [Column(name=n, type=t.upper()) for n, t, mode in fields
            if n not in skip and mode != "REPEATED" and t.upper() not in ("RECORD", "STRUCT", "JSON",
                                                                          "GEOGRAPHY", "RANGE", "INTERVAL")]


def timestamp_sql(col: str, bq_type: str) -> str:
    t = bq_type.upper()
    if t == "TIMESTAMP":
        return q(col)
    if t in ("INT64", "INTEGER"):
        return f"TIMESTAMP_SECONDS({q(col)})"
    return f"TIMESTAMP({q(col)})"


def range_filter(col: str, bq_type: str) -> str:
    """The scan's time filter, on the column itself where it can be, so a partitioned table
    reads only the lookback's partitions."""
    t = bq_type.upper()
    if t == "DATE":
        return f"{q(col)} >= DATE(@from_ts) AND {q(col)} < DATE(@to_ts)"
    if t == "DATETIME":
        return f"{q(col)} >= DATETIME(@from_ts) AND {q(col)} < DATETIME(@to_ts)"
    expr = timestamp_sql(col, t)
    return f"{expr} >= @from_ts AND {expr} < @to_ts"


def _regression(m: QualityMonitor) -> bool:
    return m.inference_log is not None and m.inference_log.problem_type == "PROBLEM_TYPE_REGRESSION"


def _classification(m: QualityMonitor) -> bool:
    return m.inference_log is not None and not _regression(m)


def _classifier(m: QualityMonitor) -> bool:
    """A classification with labels: its model quality is computed."""
    return bool(_classification(m) and m.inference_log.label_col)


def _probabilities(m: QualityMonitor) -> bool:
    return bool(_classifier(m) and m.inference_log.prediction_proba_col)


def render_definition(definition: str, m: QualityMonitor, column: str | None) -> str:
    """A custom metric's definition with Databricks' placeholders filled in."""
    il = m.inference_log
    out = definition
    for ph, value in (("input_column", column), ("prediction_col", il.prediction_col if il else None),
                      ("label_col", il.label_col if il else None)):
        pattern = re.compile(r"\{\{\s*" + ph + r"\s*\}\}")
        if pattern.search(out):
            if value is None:
                raise ValueError(f"custom metric definition {definition!r}: {{{{{ph}}}}} has no value here")
            out = pattern.sub(value, out)
    return out


def _aggregates(m: QualityMonitor) -> list[MonitorMetric]:
    return [cm for cm in m.custom_metrics if cm.type == "CUSTOM_METRIC_TYPE_AGGREGATE"]


def _derived(m: QualityMonitor) -> list[MonitorMetric]:
    return [cm for cm in m.custom_metrics if cm.type == "CUSTOM_METRIC_TYPE_DERIVED"]


def _drifts(m: QualityMonitor) -> list[MonitorMetric]:
    return [cm for cm in m.custom_metrics if cm.type == "CUSTOM_METRIC_TYPE_DRIFT"]


#: The scan's statistics of one column, one STRUCT each.
_COL_FIELDS = (("column_name", "STRING"), ("data_type", "STRING"), ("count", "INT64"),
               ("num_nulls", "INT64"), ("avg", "FLOAT64"), ("quantiles", "ARRAY<FLOAT64>"),
               ("distinct_count", "INT64"), ("min", "FLOAT64"), ("max", "FLOAT64"), ("stddev", "FLOAT64"),
               ("num_zeros", "INT64"), ("num_nan", "INT64"), ("min_len", "FLOAT64"), ("max_len", "FLOAT64"),
               ("avg_len", "FLOAT64"), ("frequent_items", "ARRAY<STRUCT<value STRING, count INT64>>"))


def _col_fields(m: QualityMonitor) -> list[tuple[str, str]]:
    return [*_COL_FIELDS, *((f"cm_{cm.name}", cm.bigquery_type) for cm in _aggregates(m))]


def _column_type(m: QualityMonitor) -> str:
    return f"STRUCT<{', '.join(f'{n} {t}' for n, t in _col_fields(m))}>"


def _tuple(m: QualityMonitor, values: dict[str, str]) -> str:
    """A column STRUCT's values in field order, a typed NULL for each one not given (an array
    literal does not coerce a bare NULL)."""
    return "(" + ", ".join(values.get(n, f"CAST(NULL AS {t})") for n, t in _col_fields(m)) + ")"


def _column_tuple(m: QualityMonitor, c: Column) -> str:
    x = q(c.name)
    v = {"column_name": _string(c.name), "data_type": _string(c.type), "count": f"COUNT({x})",
         "num_nulls": f"COUNTIF({x} IS NULL)", "distinct_count": f"APPROX_COUNT_DISTINCT({x})"}
    if c.kind == "numeric":
        f = f"CAST({x} AS FLOAT64)"
        v |= {"avg": f"AVG({f})", "quantiles": f"APPROX_QUANTILES(IF(IS_NAN({f}), NULL, {f}), {QUANTILES})",
              "min": f"MIN({f})", "max": f"MAX({f})", "stddev": f"STDDEV_SAMP({f})",
              "num_zeros": f"COUNTIF({f} = 0)", "num_nan": f"COUNTIF(IS_NAN({f}))"}
    elif c.kind == "categorical":
        v["frequent_items"] = f"APPROX_TOP_COUNT(CAST({x} AS STRING), {TOP_ITEMS})"
    if c.type == "STRING":
        v |= {"min_len": f"MIN(CHAR_LENGTH({x}))", "max_len": f"MAX(CHAR_LENGTH({x}))",
              "avg_len": f"AVG(CHAR_LENGTH({x}))"}
    for cm in _aggregates(m):
        if c.name in cm.input_columns:
            v[f"cm_{cm.name}"] = f"({render_definition(cm.definition, m, c.name)})"
    return _tuple(m, v)


def _quality_aggregates(m: QualityMonitor) -> list[str]:
    il = m.inference_log
    if il is None or il.label_col is None:
        return []
    cast = "FLOAT64" if _regression(m) else "STRING"
    p, y = f"CAST({q(il.prediction_col)} AS {cast})", f"CAST({q(il.label_col)} AS {cast})"
    ok = f"{p} IS NOT NULL AND {y} IS NOT NULL"
    if not _regression(m):
        return [f"AVG(IF({ok}, IF({p} = {y}, 1, 0), NULL)) AS q_accuracy"]
    return [f"AVG(IF({ok}, POW({y} - {p}, 2), NULL)) AS q_mse",
            f"AVG(IF({ok}, ABS({y} - {p}), NULL)) AS q_mae",
            f"AVG(IF({ok} AND {y} != 0, ABS(({y} - {p}) / {y}), NULL)) AS q_mape",
            f"VAR_POP(IF({ok}, {y}, NULL)) AS q_label_var"]


def slices_sql(exprs: list[str]) -> str:
    items = ["STRUCT(CAST(NULL AS STRING) AS slice_key, CAST(NULL AS STRING) AS slice_value)"]
    items += [f"STRUCT({_string(e)}, CAST(({e}) AS STRING))" for e in exprs]
    return f"UNNEST([{', '.join(items)}])"


def _string(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _groups(m: QualityMonitor, by_model: bool, slices: list[str]) -> list[str]:
    model = f"CAST({q(m.inference_log.model_id_col)} AS STRING)" if by_model else None
    return [f"CROSS JOIN UNNEST([{model}, CAST(NULL AS STRING)]) AS mdl" if model
            else "CROSS JOIN UNNEST([CAST(NULL AS STRING)]) AS mdl",
            f"CROSS JOIN {slices_sql(slices)} AS s"]


class Scan(BaseModel):
    """One read of a table: the INPUT rows of one granularity (window: the window start's
    SQL), a snapshot's INPUT rows, or the BASELINE."""

    table: str
    log_type: Literal["INPUT", "BASELINE"]
    granularity: str | None = None
    window: str | None = None
    #: (column, BigQuery type) the scan's time filter reads; None: the whole table.
    ts: tuple[str, str] | None = None
    by_model: bool = False
    slices: list[str] = []
    #: A snapshot's INPUT: its window is the refresh's time (@now).
    snapshot: bool = False

    def start_sql(self, col: str) -> str:
        return "@now" if self.snapshot else col if self.window else "CAST(NULL AS TIMESTAMP)"

    def gkey(self, start: str, model: str, slice_key: str, slice_value: str) -> str:
        gran = _string(self.granularity) if self.granularity else "CAST(NULL AS STRING)"
        return f"FORMAT('%T', ('{self.log_type}', {gran}, {start}, {model}, {slice_key}, {slice_value}))"

    def fkey(self, start: str, model: str, slice_key: str) -> str:
        """The group a slice's two values share: a fairness slice's protected (true) and
        unprotected (false) rows."""
        gran = _string(self.granularity) if self.granularity else "CAST(NULL AS STRING)"
        return f"FORMAT('%T', ('{self.log_type}', {gran}, {start}, {model}, {slice_key}))"


def scan_sql(m: QualityMonitor, scan: Scan, columns: list[Column]) -> str:
    """One pass over the table: every column's statistics (as an array, one STRUCT each),
    model quality and the table's custom aggregates, grouped by window, model_id (and all
    models together) and slice (and the whole table)."""
    tuples = ",\n        ".join(_column_tuple(m, c) for c in columns)
    select = [f"{scan.window or 'CAST(NULL AS TIMESTAMP)'} AS window_start", "mdl AS model_id",
              "s.slice_key", "s.slice_value", "COUNT(*) AS t_count",
              f"ARRAY<{_column_type(m)}>[\n        {tuples}\n    ] AS cols" if columns
              else f"ARRAY<{_column_type(m)}>[] AS cols",
              *_quality_aggregates(m),
              *(f"({render_definition(cm.definition, m, None)}) AS t_cm_{cm.name}"
                for cm in _aggregates(m) if TABLE in cm.input_columns)]
    where = f"\nWHERE {range_filter(*scan.ts)}" if scan.ts else ""
    return (f"SELECT\n    {(',' + chr(10) + '    ').join(select)}\nFROM {q(scan.table)}\n"
            + "\n".join(_groups(m, scan.by_model, scan.slices)) + where
            + "\nGROUP BY window_start, model_id, slice_key, slice_value")


def window_end_sql(start: str, granularity: str) -> str:
    if granularity in _BQ_INTERVAL:
        return f"TIMESTAMP_ADD({start}, INTERVAL {_BQ_INTERVAL[granularity]})"
    unit = "MONTH" if granularity == "1 month" else "YEAR"
    return f"TIMESTAMP(DATE_ADD(DATE({start}), INTERVAL 1 {unit}))"


#: The profile metrics a scan computes, in the table's order; the rest are joined in when the
#: rows are written (a classifier's confusion matrix and per-class metrics, its probability
#: metrics and fairness, derived metrics).
_CLASSIFICATION = ("confusion_matrix", "precision", "recall", "f1_score")
_PROBABILITY = ("log_loss", "roc_auc_score")
_FAIRNESS = ("predictive_parity", "predictive_equality", "equal_opportunity", "statistical_parity")
_JOINED = (*_CLASSIFICATION, *_PROBABILITY, *_FAIRNESS)


def profile_rows_sql(m: QualityMonitor, scan: Scan, columns: list[Column]) -> str:
    """The scan's rows as profile metric rows: one per column and one `:table` row per
    (window, model_id, slice), keyed by _gkey for the confusion counts of the same group."""
    start = scan.start_sql("s.window_start")
    window = (f"STRUCT({start} AS start, {start} AS `end`)" if scan.snapshot
              else f"STRUCT({start} AS start, {window_end_sql(start, scan.granularity)} AS `end`)"
              if scan.window else "CAST(NULL AS STRUCT<start TIMESTAMP, `end` TIMESTAMP>)")
    gran = _string(scan.granularity) if scan.granularity else "CAST(NULL AS STRING)"
    table_row = f"ARRAY<{_column_type(m)}>[{_tuple(m, {'column_name': repr(TABLE), 'count': 's.t_count'})}]"
    is_table = f"c.column_name = '{TABLE}'"
    il = m.inference_log
    out = {
        "window": window, "granularity": gran, "model_id": "s.model_id", "log_type": f"'{scan.log_type}'",
        "slice_key": "s.slice_key", "slice_value": "s.slice_value", "column_name": "c.column_name",
        "data_type": "c.data_type", "logging_table_commit_version": "CAST(NULL AS INT64)",
        "monitor_version": "@monitor_version", "count": "c.count", "num_nulls": "c.num_nulls",
        "avg": "c.avg", "quantiles": "IFNULL(c.quantiles, [])", "distinct_count": "c.distinct_count",
        "min": "c.min", "max": "c.max", "stddev": "c.stddev", "num_zeros": "c.num_zeros",
        "num_nan": "c.num_nan", "min_size": "CAST(NULL AS FLOAT64)", "max_size": "CAST(NULL AS FLOAT64)",
        "avg_size": "CAST(NULL AS FLOAT64)", "min_len": "c.min_len", "max_len": "c.max_len",
        "avg_len": "c.avg_len",
        "frequent_items": "ARRAY(SELECT AS STRUCT t.value AS item, t.count FROM UNNEST(c.frequent_items) "
                          "AS t WHERE t.value IS NOT NULL)",
        "non_null_columns": f"IF({is_table}, ARRAY(SELECT x.column_name FROM UNNEST(s.cols) AS x "
                            f"WHERE x.count > 0), [])",
        "median": f"c.quantiles[SAFE_OFFSET({QUANTILES // 2})]",
        "percent_null": "100 * SAFE_DIVIDE(c.num_nulls, s.t_count)",
        "percent_zeros": "100 * SAFE_DIVIDE(c.num_zeros, c.count)",
        "percent_distinct": "100 * SAFE_DIVIDE(c.distinct_count, c.count)",
        "accuracy_score": f"IF({is_table}, s.q_accuracy, NULL)" if _classifier(m) else "CAST(NULL AS FLOAT64)",
    }
    regression = bool(il and il.label_col and _regression(m))
    for col, expr in (("mean_squared_error", "s.q_mse"), ("root_mean_squared_error", "SQRT(s.q_mse)"),
                      ("mean_average_error", "s.q_mae"), ("mean_absolute_percentage_error", "s.q_mape"),
                      ("r2_score", "1 - SAFE_DIVIDE(s.q_mse, s.q_label_var)")):
        out[col] = f"IF({is_table}, {expr}, NULL)" if regression else "CAST(NULL AS FLOAT64)"
    for cm in _aggregates(m):
        table_value = f"s.t_cm_{cm.name}" if TABLE in cm.input_columns else "NULL"
        out[cm.name] = f"IF({is_table}, {table_value}, c.cm_{cm.name})"
    out["_gkey"] = scan.gkey(start, "s.model_id", "s.slice_key", "s.slice_value")
    cols = ",\n    ".join(f"{e} AS {q(n)}" for n, e in out.items())
    return (f"SELECT\n    {cols}\nFROM (\n{scan_sql(m, scan, columns)}\n) AS s\n"
            f"CROSS JOIN UNNEST(ARRAY_CONCAT(s.cols, {table_row})) AS c")


def confusion_sql(m: QualityMonitor, scan: Scan) -> str:
    """A classification's counts per window, model_id and slice, keyed as its profile rows: a
    row per prediction and label, label NULL where it hasn't arrived (statistical parity reads
    every prediction; model quality only the labelled ones). _fkey and _slice_value pair a
    slice's true and false groups for fairness."""
    il = m.inference_log
    p = f"CAST({q(il.prediction_col)} AS STRING)"
    y = f"CAST({q(il.label_col)} AS STRING)" if il.label_col else "CAST(NULL AS STRING)"
    start = scan.start_sql(scan.window or "")
    where = [f"{p} IS NOT NULL", *([range_filter(*scan.ts)] if scan.ts else [])]
    return (f"SELECT\n    {scan.gkey(start, 'mdl', 's.slice_key', 's.slice_value')} AS _gkey,\n"
            f"    {scan.fkey(start, 'mdl', 's.slice_key')} AS _fkey,\n    s.slice_value AS _slice_value,\n"
            f"    {p} AS prediction,\n    {y} AS label,\n    COUNT(*) AS n\nFROM {q(scan.table)}\n"
            + "\n".join(_groups(m, scan.by_model, scan.slices)) +
            f"\nWHERE {' AND '.join(where)}\nGROUP BY _gkey, _fkey, _slice_value, prediction, label")


def proba_sql(m: QualityMonitor, scan: Scan) -> str:
    """A classifier's probabilities as counts per window, model_id and slice, keyed as its
    profile rows: how many rows of each label gave each class each probability, and (cls
    NULL) how many rows of each label there are. The rows log_loss and roc_auc_score read:
    prediction, label and probabilities all present."""
    il = m.inference_log
    p, y = f"CAST({q(il.prediction_col)} AS STRING)", f"CAST({q(il.label_col)} AS STRING)"
    proba = q(il.prediction_proba_col)
    start = scan.start_sql(scan.window or "")
    where = [f"{p} IS NOT NULL", f"{y} IS NOT NULL", f"ARRAY_LENGTH({proba}) > 0",
             *([range_filter(*scan.ts)] if scan.ts else [])]
    per_class = (f"ARRAY_CONCAT(ARRAY(SELECT AS STRUCT CAST(x.key AS STRING) AS key, CAST(x.value AS FLOAT64) "
                 f"AS value FROM UNNEST({proba}) AS x), [STRUCT(CAST(NULL AS STRING) AS key, "
                 f"CAST(NULL AS FLOAT64) AS value)])")
    return (f"SELECT\n    {scan.gkey(start, 'mdl', 's.slice_key', 's.slice_value')} AS _gkey,\n"
            f"    {y} AS label,\n    kv.key AS cls,\n    kv.value AS score,\n    COUNT(*) AS n\n"
            f"FROM {q(scan.table)}\n" + "\n".join(_groups(m, scan.by_model, scan.slices)) +
            f"\nCROSS JOIN UNNEST({per_class}) AS kv\nWHERE {' AND '.join(where)}\n"
            "GROUP BY _gkey, label, cls, score")


# --- drift --------------------------------------------------------------------------------

#: Two-sample Kolmogorov-Smirnov, Wasserstein distance and PSI from each side's quantiles
#: (current a, comparison b) and row counts. PSI bins are the comparison's deciles: below 0.1
#: no significant change, 0.1 to 0.2 moderate, 0.2 and above significant.
NUMERIC_DRIFT_JS = r"""
if (!a || !b || !a.length || !b.length) return null;
function cdf(qs, v) {
  let lo = 0, hi = qs.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (qs[mid] <= v) lo = mid + 1; else hi = mid; }
  return lo / qs.length;
}
function kolmogorov(lam) {
  if (lam < 1e-3) return 1;
  let total = 0, sign = 1;
  for (let j = 1; j <= 100; j++) {
    const term = sign * 2 * Math.exp(-2 * j * j * lam * lam);
    total += term;
    if (Math.abs(term) < 1e-12) break;
    sign = -sign;
  }
  return Math.min(Math.max(total, 0), 1);
}
let d = 0;
for (const v of a.concat(b)) d = Math.max(d, Math.abs(cdf(a, v) - cdf(b, v)));
let ks = null;
if (na > 0 && nb > 0) {
  const en = Math.sqrt(na * nb / (na + nb));
  ks = {statistic: d, pvalue: kolmogorov((en + 0.12 + 0.11 / en) * d)};
}
let w = null;
if (a.length === b.length) {
  w = 0;
  for (let i = 0; i < a.length; i++) w += Math.abs(a[i] - b[i]);
  w /= a.length;
}
const edges = [...new Set([1, 2, 3, 4, 5, 6, 7, 8, 9].map(i => b[Math.round(i * (b.length - 1) / 10)]))]
  .sort((x, y) => x - y);
function shares(qs) {
  const cuts = [0, ...edges.map(e => cdf(qs, e)), 1];
  const out = [];
  for (let i = 1; i < cuts.length; i++) out.push(Math.max(cuts[i] - cuts[i - 1], 1e-4));
  return out;
}
const sa = shares(a), sb = shares(b);
let psi = 0;
for (let i = 0; i < sa.length; i++) psi += (sa[i] - sb[i]) * Math.log(sa[i] / sb[i]);
return {ks_test: ks, wasserstein_distance: w, population_stability_index: psi};
"""

#: chi-squared test of homogeneity, total variation, L-infinity and Jensen-Shannon distance
#: (base 2) between two columns' frequent items (items, counts), the rest of each column's
#: non-null count pooled as one item.
CATEGORICAL_DRIFT_JS = r"""
function dist(items, counts, n) {
  const d = new Map();
  let seen = 0;
  for (let i = 0; i < (items || []).length; i++) { d.set(items[i], counts[i]); seen += counts[i]; }
  if ((n || 0) - seen > 0) d.set("\u0000other", n - seen);
  return d;
}
function gammaQ(s, x) {
  const lg = lgamma(s);
  if (x < s + 1) {
    let term = 1 / s, total = term, ap = s;
    for (let i = 0; i < 1000; i++) { ap += 1; term *= x / ap; total += term; if (Math.abs(term) < Math.abs(total) * 1e-15) break; }
    return Math.max(0, 1 - total * Math.exp(-x + s * Math.log(x) - lg));
  }
  const tiny = 1e-300;
  let bb = x + 1 - s, c = 1 / tiny, dd = 1 / bb, h = dd;
  for (let i = 1; i < 1000; i++) {
    const an = -i * (i - s);
    bb += 2;
    dd = an * dd + bb; if (Math.abs(dd) < tiny) dd = tiny;
    c = bb + an / c; if (Math.abs(c) < tiny) c = tiny;
    dd = 1 / dd;
    const delta = dd * c;
    h *= delta;
    if (Math.abs(delta - 1) < 1e-15) break;
  }
  return Math.min(1, Math.exp(-x + s * Math.log(x) - lg) * h);
}
function lgamma(z) {
  const g = 7, c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
    -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
  if (z < 0.5) return Math.log(Math.PI / Math.abs(Math.sin(Math.PI * z))) - lgamma(1 - z);
  z -= 1;
  let x = c[0];
  for (let i = 1; i < g + 2; i++) x += c[i] / (z + i);
  const t = z + g + 0.5;
  return 0.5 * Math.log(2 * Math.PI) + (z + 0.5) * Math.log(t) - t + Math.log(x);
}
const da = dist(ai, ac, na), db = dist(bi, bc, nb);
let ta = 0, tb = 0;
for (const v of da.values()) ta += v;
for (const v of db.values()) tb += v;
if (!ta || !tb) return null;
const keys = [...new Set([...da.keys(), ...db.keys()])];
let tv = 0, linf = 0, js = 0, stat = 0;
for (const k of keys) {
  const xa = da.get(k) || 0, xb = db.get(k) || 0, pa = xa / ta, pb = xb / tb, mid = (pa + pb) / 2;
  tv += Math.abs(pa - pb) / 2;
  linf = Math.max(linf, Math.abs(pa - pb));
  if (pa > 0) js += 0.5 * pa * Math.log2(pa / mid);
  if (pb > 0) js += 0.5 * pb * Math.log2(pb / mid);
  const col = xa + xb;
  for (const [obs, side] of [[xa, ta], [xb, tb]]) {
    const exp = col * side / (ta + tb);
    if (exp) stat += (obs - exp) * (obs - exp) / exp;
  }
}
const dof = keys.length - 1;
const pvalue = dof > 0 ? (stat <= 0 ? 1 : gammaQ(dof / 2, stat / 2)) : 1;
return {chi_squared_test: {statistic: stat, pvalue: pvalue}, tv_distance: tv, l_infinity_distance: linf,
        js_distance: Math.sqrt(Math.max(js, 0))};
"""

_TEST_TYPE = "STRUCT<statistic FLOAT64, pvalue FLOAT64>"
UDFS = (
    "CREATE TEMP FUNCTION numeric_drift(a ARRAY<FLOAT64>, b ARRAY<FLOAT64>, na FLOAT64, nb FLOAT64)\n"
    f"RETURNS STRUCT<ks_test {_TEST_TYPE}, wasserstein_distance FLOAT64, population_stability_index FLOAT64>\n"
    f'LANGUAGE js AS r"""{NUMERIC_DRIFT_JS}""";\n'
    "CREATE TEMP FUNCTION categorical_drift(ai ARRAY<STRING>, ac ARRAY<FLOAT64>, na FLOAT64,\n"
    "                                       bi ARRAY<STRING>, bc ARRAY<FLOAT64>, nb FLOAT64)\n"
    f"RETURNS STRUCT<chi_squared_test {_TEST_TYPE}, tv_distance FLOAT64, l_infinity_distance FLOAT64,"
    " js_distance FLOAT64>\n"
    f'LANGUAGE js AS r"""{CATEGORICAL_DRIFT_JS}""";\n'
)
#: What drift reads of a profile row, the same from staging and from the metric table.
_DRIFT_INPUT = ("`window`", "granularity", "model_id", "slice_key", "slice_value", "column_name",
                "data_type", "`count`", "`avg`", "percent_null", "percent_zeros", "percent_distinct",
                "quantiles", "frequent_items", "non_null_columns")


def _drift_input(source: str, where: str, custom: list[str]) -> str:
    return (f"SELECT {', '.join([*_DRIFT_INPUT, *map(q, custom)])},\n"
            "    FORMAT('%T', (granularity, model_id, slice_key, slice_value, column_name)) AS _skey,\n"
            "    FORMAT('%T', (model_id, slice_key, slice_value, column_name)) AS _bkey,\n"
            "    FORMAT('%T', (CAST(NULL AS STRING), slice_key, slice_value, column_name)) AS _bkey_all\n"
            f"FROM {source}\nWHERE {where}")


def render_drift(cm: MonitorMetric) -> str:
    """A custom drift metric's definition over a drift pair: {{current_df}} the window's
    profile row (c), {{base_df}} the row it is compared with (p), the previous window's or
    the baseline's."""
    return _DRIFT_PLACEHOLDER.sub(lambda x: "c" if x.group(1) == "current_df" else "p", cm.definition)


def drift_sql(source: str, *, previous: str | None, consecutive_after: dict[str, str],
              m: QualityMonitor | None = None) -> str:
    """The script's statements that build TEMP TABLE drift: each INPUT row against the window
    before it (CONSECUTIVE: a window starting after its granularity's consecutive_after
    parameter; a snapshot against `previous`, the metric table's last snapshot) and against
    the baseline's row of its model_id, slice and column (BASELINE; a model the baseline lacks
    compares with the baseline's all-models row). `source` holds this refresh's profile rows;
    m's custom drift metrics are computed on their input columns' rows."""
    custom = [cm.name for cm in (*_aggregates(m), *_derived(m))] if m else []
    drifts = _drifts(m) if m else []
    if previous is None:
        after = " ".join(f"WHEN {_string(g)} THEN {p}" for g, p in consecutive_after.items())
        prev, cond = "cur", f"p.`window`.`end` = c.`window`.start AND c.`window`.start > CASE c.granularity {after} END"
    else:
        prev, cond = "prev", "TRUE"
    cur = _drift_input(q(source), "log_type = 'INPUT'", custom)
    base = _drift_input(q(source), "log_type = 'BASELINE'", custom)
    stmts = [f"CREATE TEMP TABLE cur AS\n{cur};", f"CREATE TEMP TABLE base AS\n{base};"]
    if previous is not None:
        last = (f"log_type = 'INPUT' AND `window`.start = (SELECT MAX(`window`.start) FROM {q(previous)} "
                "WHERE log_type = 'INPUT' AND `window`.start < @now)")
        stmts.append(f"CREATE TEMP TABLE prev AS\n{_drift_input(q(previous), last, custom)};")
    stmts.append(
        "CREATE TEMP TABLE pairs AS\n"
        f"SELECT 'CONSECUTIVE' AS drift_type, c, p\nFROM cur AS c\nJOIN {prev} AS p ON p._skey = c._skey AND {cond}\n"
        "UNION ALL\n"
        "SELECT 'BASELINE' AS drift_type, c, IF(b1._bkey IS NOT NULL, b1, b0) AS p\nFROM cur AS c\n"
        "LEFT JOIN base AS b1 ON b1._bkey = c._bkey\nLEFT JOIN base AS b0 ON b0._bkey = c._bkey_all\n"
        "WHERE b1._bkey IS NOT NULL OR b0._bkey IS NOT NULL;")
    items = "ARRAY(SELECT x.item FROM UNNEST({0}.frequent_items) AS x ORDER BY x.item)"
    counts = "ARRAY(SELECT CAST(x.count AS FLOAT64) FROM UNNEST({0}.frequent_items) AS x ORDER BY x.item)"
    stmts.append(
        "CREATE TEMP TABLE drift AS\nSELECT\n"
        "    c.`window` AS `window`,\n    IF(drift_type = 'CONSECUTIVE', p.`window`, NULL) AS window_cmp,\n"
        "    drift_type,\n    c.granularity,\n    c.model_id,\n    c.slice_key,\n    c.slice_value,\n"
        "    c.column_name,\n    c.data_type,\n    @monitor_version AS monitor_version,\n"
        "    CAST(c.`count` - p.`count` AS FLOAT64) AS count_delta,\n    c.`avg` - p.`avg` AS avg_delta,\n"
        "    c.percent_null - p.percent_null AS percent_null_delta,\n"
        "    c.percent_zeros - p.percent_zeros AS percent_zeros_delta,\n"
        "    c.percent_distinct - p.percent_distinct AS percent_distinct_delta,\n"
        f"    IF(c.column_name = '{TABLE}', STRUCT(\n"
        "        (SELECT COUNT(*) FROM UNNEST(c.non_null_columns) AS x WHERE x NOT IN UNNEST(p.non_null_columns)) AS added,\n"
        "        (SELECT COUNT(*) FROM UNNEST(p.non_null_columns) AS x WHERE x NOT IN UNNEST(c.non_null_columns)) AS missing\n"
        "    ), NULL) AS non_null_columns_delta,\n"
        "    cat.chi_squared_test,\n    num.ks_test,\n    cat.tv_distance,\n    cat.l_infinity_distance,\n"
        "    cat.js_distance,\n    num.wasserstein_distance,\n    num.population_stability_index"
        + "".join(f",\n    IF(c.column_name IN UNNEST({_strings(cm.input_columns)}), ({render_drift(cm)}), NULL)"
                  f" AS {q(cm.name)}" for cm in drifts) + "\n"
        "FROM (\n    SELECT\n        drift_type,\n        c,\n        p,\n"
        "        IF(ARRAY_LENGTH(c.quantiles) > 0 AND ARRAY_LENGTH(p.quantiles) > 0,\n"
        "           numeric_drift(c.quantiles, p.quantiles, CAST(c.`count` AS FLOAT64), CAST(p.`count` AS FLOAT64)),\n"
        "           NULL) AS num,\n"
        "        IF(ARRAY_LENGTH(c.frequent_items) > 0 OR ARRAY_LENGTH(p.frequent_items) > 0,\n"
        f"           categorical_drift({items.format('c')}, {counts.format('c')}, CAST(c.`count` AS FLOAT64),\n"
        f"                             {items.format('p')}, {counts.format('p')}, CAST(p.`count` AS FLOAT64)),\n"
        "           NULL) AS cat\n    FROM pairs\n);")
    return "\n".join(stmts)


# --- writing the metric tables ------------------------------------------------------------

def classification_sql(confusion: str) -> str:
    """The confusion matrix and per-class precision, recall and f1 (one class against the rest,
    with their macro and support-weighted averages), per _gkey."""
    per_class = ("STRUCT(ARRAY_AGG(STRUCT(cls AS key, {0} AS value) ORDER BY cls) AS one_vs_all, "
                 "AVG({0}) AS macro, SAFE_DIVIDE(SUM({0} * true_n), SUM(true_n)) AS weighted) AS {1}")
    return (
        f"WITH conf AS (SELECT _gkey, prediction, label, n FROM {q(confusion)} WHERE label IS NOT NULL),\n"
        "classes AS (SELECT DISTINCT _gkey, cls FROM conf CROSS JOIN UNNEST([prediction, label]) AS cls),\n"
        "stats AS (\n    SELECT k._gkey, k.cls,\n"
        "        SUM(IF(c.prediction = k.cls AND c.label = k.cls, c.n, 0)) AS tp,\n"
        "        SUM(IF(c.prediction = k.cls, c.n, 0)) AS pred_n,\n"
        "        SUM(IF(c.label = k.cls, c.n, 0)) AS true_n\n"
        "    FROM classes AS k\n    JOIN conf AS c ON c._gkey = k._gkey\n    GROUP BY k._gkey, k.cls\n),\n"
        "pr AS (\n    SELECT _gkey, cls, true_n,\n"
        "        IFNULL(SAFE_DIVIDE(tp, pred_n), 0) AS p,\n        IFNULL(SAFE_DIVIDE(tp, true_n), 0) AS r\n"
        "    FROM stats\n),\n"
        "prf AS (SELECT *, IFNULL(SAFE_DIVIDE(2 * p * r, p + r), 0) AS f FROM pr),\n"
        "per_class AS (\n    SELECT _gkey,\n"
        f"        {per_class.format('p', 'precision')},\n        {per_class.format('r', 'recall')},\n"
        f"        {per_class.format('f', 'f1_score')}\n    FROM prf\n    GROUP BY _gkey\n),\n"
        "matrix AS (\n    SELECT _gkey, ARRAY_AGG(STRUCT(prediction, label, n AS `count`) ORDER BY prediction, label)"
        " AS confusion_matrix\n    FROM conf\n    GROUP BY _gkey\n)\n"
        "SELECT m._gkey, m.confusion_matrix, pc.precision, pc.recall, pc.f1_score\n"
        "FROM matrix AS m\nLEFT JOIN per_class AS pc ON pc._gkey = m._gkey")


#: Probabilities are clipped to [EPS, 1 - EPS] for log_loss, as sklearn's (float64's epsilon).
EPS = 2.220446049250313e-16


def probability_sql(proba: str) -> str:
    """log_loss and roc_auc_score per _gkey, from proba_sql's counts.

    log_loss: the mean of -ln p, p the row's probability of its label (clipped; a label its
    probabilities don't name is p = 0). roc_auc_score: sklearn's one-vs-one (Hand & Till) over
    the labels present: for each two labels a and b, the rows labelled either, the AUC of a's
    probability for a against b and of b's for b against a, averaged; macro the mean over the
    pairs, weighted by the pair's share of the rows. Each AUC is exact, ties counting half
    (Mann-Whitney), a class a row's probabilities don't name scoring 0."""
    clip = f"LEAST(GREATEST(score, {EPS!r}), {1 - EPS!r})"
    return (
        f"WITH pr AS (SELECT _gkey, label, cls, score, n FROM {q(proba)}),\n"
        "tot AS (SELECT _gkey, label, SUM(n) AS total FROM pr WHERE cls IS NULL GROUP BY _gkey, label),\n"
        "ll AS (\n    SELECT t._gkey,\n"
        f"        SAFE_DIVIDE(SUM(IFNULL(h.loss, 0) + (t.total - IFNULL(h.named, 0)) * {-math.log(EPS)!r}),"
        " SUM(t.total)) AS log_loss\n"
        "    FROM tot AS t\n    LEFT JOIN (\n"
        f"        SELECT _gkey, label, SUM(n * -LN({clip})) AS loss, SUM(n) AS named\n"
        "        FROM pr WHERE cls = label AND score IS NOT NULL GROUP BY _gkey, label\n"
        "    ) AS h ON h._gkey = t._gkey AND h.label = t.label\n    GROUP BY t._gkey\n),\n"
        "pairs AS (\n    SELECT a._gkey, a.label AS pos, b.label AS neg, a.total AS npos, b.total AS nneg\n"
        "    FROM tot AS a JOIN tot AS b ON b._gkey = a._gkey AND b.label != a.label\n),\n"
        "scored AS (\n    SELECT pa._gkey, pa.pos, pa.neg, pr.score,\n"
        "        IF(pr.label = pa.pos, pr.n, 0) AS np, IF(pr.label = pa.neg, pr.n, 0) AS nn\n"
        "    FROM pairs AS pa\n    JOIN pr ON pr._gkey = pa._gkey AND pr.cls = pa.pos AND pr.label IN (pa.pos, pa.neg)"
        " AND pr.score IS NOT NULL\n),\n"
        "filled AS (\n    SELECT _gkey, pos, neg, score, np, nn FROM scored\n    UNION ALL\n"
        "    SELECT pa._gkey, pa.pos, pa.neg, 0.0, pa.npos - IFNULL(SUM(s.np), 0), pa.nneg - IFNULL(SUM(s.nn), 0)\n"
        "    FROM pairs AS pa\n    LEFT JOIN scored AS s ON s._gkey = pa._gkey AND s.pos = pa.pos AND s.neg = pa.neg\n"
        "    GROUP BY pa._gkey, pa.pos, pa.neg, pa.npos, pa.nneg\n),\n"
        "by_score AS (SELECT _gkey, pos, neg, score, SUM(np) AS np, SUM(nn) AS nn FROM filled"
        " GROUP BY _gkey, pos, neg, score),\n"
        "ranked AS (\n    SELECT *,\n"
        "        IFNULL(SUM(nn) OVER (PARTITION BY _gkey, pos, neg ORDER BY score"
        " ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS below\n    FROM by_score\n),\n"
        "auc AS (\n    SELECT _gkey, pos, neg, SAFE_DIVIDE(SUM(np * (below + 0.5 * nn)), SUM(np) * SUM(nn)) AS auc\n"
        "    FROM ranked\n    GROUP BY _gkey, pos, neg\n),\n"
        "pair AS (\n    SELECT a._gkey, (a.auc + b.auc) / 2 AS score, pa.npos + pa.nneg AS w\n"
        "    FROM auc AS a\n    JOIN auc AS b ON b._gkey = a._gkey AND b.pos = a.neg AND b.neg = a.pos\n"
        "    JOIN pairs AS pa ON pa._gkey = a._gkey AND pa.pos = a.pos AND pa.neg = a.neg\n    WHERE a.pos < a.neg\n),\n"
        "roc AS (\n    SELECT _gkey, STRUCT(STRUCT(SAFE_DIVIDE(SUM(score * w), SUM(w)) AS weighted, AVG(score) AS macro)"
        " AS one_vs_one) AS roc_auc_score\n    FROM pair\n    GROUP BY _gkey\n)\n"
        "SELECT ll._gkey, ll.log_loss, roc.roc_auc_score\nFROM ll\nLEFT JOIN roc ON roc._gkey = ll._gkey")


def fairness_sql(confusion: str, labelled: bool) -> str:
    """Fairness and bias per _gkey of a slice's protected group (slice value true) against its
    unprotected group (false), one-vs-all per class (every class predicted or labelled in
    either group): protected minus unprotected precision (predictive_parity), false positive
    rate (predictive_equality), recall (equal_opportunity), over the labelled rows, and
    acceptance rate (statistical_parity), the share of every prediction that is the class. A
    rate one group can't have (no row predicted the class, say) leaves the class NULL."""
    diff = "STRUCT(t.cls AS key, t.{0} - f.{0} AS value)"
    labelled_cols = (", ".join(f"ARRAY_AGG({diff.format(c)} ORDER BY t.cls) AS {n}" for c, n in
                                (("prec", "predictive_parity"), ("fpr", "predictive_equality"),
                                 ("rec", "equal_opportunity"))) if labelled else
                     ", ".join(f"CAST(NULL AS ARRAY<STRUCT<key STRING, value FLOAT64>>) AS {n}"
                               for n in ("predictive_parity", "predictive_equality", "equal_opportunity")))
    return (
        f"WITH conf AS (\n    SELECT _gkey, _fkey, _slice_value AS sv, prediction, label, n FROM {q(confusion)}\n"
        "    WHERE _slice_value IN ('true', 'false')\n),\n"
        "both_groups AS (SELECT _fkey FROM conf GROUP BY _fkey HAVING COUNT(DISTINCT sv) = 2),\n"
        "classes AS (\n    SELECT DISTINCT c._fkey, cls\n    FROM conf AS c JOIN both_groups USING (_fkey)\n"
        "    CROSS JOIN UNNEST([c.prediction, c.label]) AS cls\n    WHERE cls IS NOT NULL\n),\n"
        "stats AS (\n    SELECT k._fkey, k.cls, c.sv,\n"
        "        SUM(IF(c.label IS NOT NULL AND c.prediction = k.cls AND c.label = k.cls, c.n, 0)) AS tp,\n"
        "        SUM(IF(c.label IS NOT NULL AND c.prediction = k.cls, c.n, 0)) AS pred_n,\n"
        "        SUM(IF(c.label = k.cls, c.n, 0)) AS true_n,\n"
        "        SUM(IF(c.label IS NOT NULL, c.n, 0)) AS labelled_n,\n"
        "        SUM(IF(c.prediction = k.cls, c.n, 0)) AS accepted_n,\n        SUM(c.n) AS all_n\n"
        "    FROM classes AS k\n    JOIN conf AS c ON c._fkey = k._fkey\n    GROUP BY k._fkey, k.cls, c.sv\n),\n"
        "rates AS (\n    SELECT _fkey, cls, sv,\n        SAFE_DIVIDE(tp, pred_n) AS prec,\n"
        "        SAFE_DIVIDE(pred_n - tp, labelled_n - true_n) AS fpr,\n        SAFE_DIVIDE(tp, true_n) AS rec,\n"
        "        SAFE_DIVIDE(accepted_n, all_n) AS acc\n    FROM stats\n),\n"
        "protected AS (SELECT DISTINCT _fkey, _gkey FROM conf WHERE sv = 'true')\n"
        f"SELECT p._gkey, {labelled_cols},\n    ARRAY_AGG({diff.format('acc')} ORDER BY t.cls) AS statistical_parity\n"
        "FROM rates AS t\nJOIN rates AS f ON f._fkey = t._fkey AND f.cls = t.cls AND f.sv = 'false'\n"
        "JOIN protected AS p ON p._fkey = t._fkey\nWHERE t.sv = 'true'\nGROUP BY p._gkey")


def profile_names(m: QualityMonitor) -> list[str]:
    """The profile metrics table's columns a refresh writes: Databricks' and the custom
    aggregate and derived metrics'."""
    return [*PROFILE_COLUMNS, *(cm.name for cm in (*_aggregates(m), *_derived(m)))]


def profile_temp_sql(m: QualityMonitor, staging: str, confusion: str | None, proba: str | None) -> str:
    """TEMP TABLE prof: the staged rows as the profile metrics table's, with a classifier's
    confusion matrix, per-class, probability and fairness metrics on its `:table` rows and the
    derived custom metrics computed. Drift reads it, then it is written."""
    staged = [n for n in PROFILE_COLUMNS if n not in _JOINED] + [cm.name for cm in _aggregates(m)]
    joins = []
    if confusion and _classifier(m):
        joins.append(("k", _CLASSIFICATION, classification_sql(confusion)))
    if proba:
        joins.append(("pb", _PROBABILITY, probability_sql(proba)))
    if confusion and m.slicing_exprs:
        joins.append(("fr", _FAIRNESS, fairness_sql(confusion, _classifier(m))))
    inner = [f"r.{q(n)}" for n in staged]
    source = f"{q(staging)} AS r"
    joined = {}
    for alias, cols, sql in joins:
        source += f"\n    LEFT JOIN (\n{sql}\n    ) AS {alias} ON {alias}._gkey = r._gkey"
        joined |= {c: alias for c in cols}
    inner += [f"IF(r.column_name = '{TABLE}', {joined[n]}.{n}, NULL) AS {n}" if n in joined
              else f"CAST(NULL AS {_bq_type(PROFILE_COLUMNS[n])}) AS {n}" for n in _JOINED]
    outer = [q(n) for n in (*staged, *_JOINED)] + [f"({cm.definition}) AS {q(cm.name)}" for cm in _derived(m)]
    return (f"CREATE TEMP TABLE prof AS\nSELECT {', '.join(outer)}\nFROM (\n    SELECT {', '.join(inner)}\n"
            f"    FROM {source}\n);")


def profile_insert_sql(m: QualityMonitor, table: str) -> str:
    cols = ", ".join(q(n) for n in profile_names(m))
    return f"INSERT INTO {q(table)} ({cols})\nSELECT {cols} FROM prof"


def _bq_type(spec) -> str:
    """A column spec as a BigQuery type, for a typed NULL."""
    if isinstance(spec, str):
        return spec
    if spec[0] == "REPEATED":
        return f"ARRAY<{_bq_type(spec[1])}>"
    inner = spec[1] if spec[0] == "RECORD" else spec
    return "STRUCT<" + ", ".join(f"{q(n)} {_bq_type(s)}" for n, s in inner) + ">"


def _strings(xs: list[str]) -> str:
    return "[" + ", ".join(_string(x) for x in xs) + "]"


def drift_insert_sql(table: str, m: QualityMonitor | None = None) -> str:
    cols = ", ".join(q(n) for n in [*DRIFT_COLUMNS, *(cm.name for cm in (_drifts(m) if m else []))])
    return f"INSERT INTO {q(table)} ({cols})\nSELECT {cols} FROM drift"


# --- anomalies ----------------------------------------------------------------------------

def anomaly_series_sql(m: QualityMonitor, profile_table: str, granularity: str | None) -> str:
    """The series anomaly detection judges, from the profile metrics table: one per model_id,
    slice, watched column and metric of the granularity's finished INPUT windows (series_id
    their JSON). A `count` series has a point at every window of the granularity since its
    first, 0 where its group had no rows."""
    a = m.anomaly_detection
    gran = "granularity IS NULL" if granularity is None else f"granularity = {_string(granularity)}"
    finished = f"log_type = 'INPUT' AND {gran} AND `window`.`end` <= @now"
    raw = "\n    UNION ALL\n".join(
        f"    SELECT TO_JSON_STRING(STRUCT(model_id, slice_key, slice_value, column_name, {_string(x)} AS metric))"
        f" AS series_id,\n        `window`.start AS ts, CAST({q(x)} AS FLOAT64) AS value\n"
        f"    FROM {q(profile_table)}\n    WHERE {finished} AND column_name IN UNNEST({_strings(a.columns)})"
        for x in a.metrics)
    if "count" not in a.metrics:
        return f"WITH raw AS (\n{raw}\n)\nSELECT series_id, ts, value FROM raw"
    return (f"WITH raw AS (\n{raw}\n),\n"
            f"cal AS (SELECT DISTINCT `window`.start AS ts FROM {q(profile_table)} WHERE {finished}),\n"
            "counts AS (SELECT series_id, MIN(ts) AS first FROM raw "
            "WHERE JSON_VALUE(series_id, '$.metric') = 'count' GROUP BY series_id)\n"
            "SELECT series_id, ts, value FROM raw WHERE JSON_VALUE(series_id, '$.metric') != 'count'\n"
            "UNION ALL\n"
            "SELECT k.series_id, c.ts, IFNULL(r.value, 0) AS value\nFROM counts AS k\n"
            "JOIN cal AS c ON c.ts >= k.first\nLEFT JOIN raw AS r ON r.series_id = k.series_id AND r.ts = c.ts")


def anomaly_insert_sql(m: QualityMonitor, profile_table: str, table: str, granularity: str | None,
                       cutoff: str) -> str:
    """The judged targets of one granularity into the anomalies table."""
    a = m.anomaly_detection
    det = detect_sql(anomaly_series_sql(m, profile_table, granularity), a, cutoff)
    end = "d.ts" if granularity is None else window_end_sql("d.ts", granularity)
    gran = "CAST(NULL AS STRING)" if granularity is None else _string(granularity)
    dims = ", ".join(f"JSON_VALUE(d.series_id, '$.{n}') AS {n}"
                     for n in ("model_id", "slice_key", "slice_value", "column_name", "metric"))
    cols = ", ".join(q(n) for n in ANOMALY_COLUMNS)
    return (f"INSERT INTO {q(table)} ({cols})\n"
            f"SELECT STRUCT(d.ts AS start, {end} AS `end`) AS `window`, {gran} AS granularity, {dims},\n"
            "    d.value, d.lower_bound, d.upper_bound, d.anomaly_probability, d.is_anomaly, d.status,\n"
            f"    {_string(a.model)} AS detector, @monitor_version AS monitor_version\nFROM (\n{det}\n) AS d")


def detect_anomalies(m: QualityMonitor, *, client, version: int, now: datetime, log=print) -> None:
    """Judge the latest detection_windows finished windows of each granularity (a snapshot:
    this one) and replace them in the anomalies table, in one transaction."""
    profile_table, _ = m.output_tables()
    table = m.anomalies_table()
    _ensure_table(client, table, ANOMALY_COLUMNS, {}, ["granularity", "metric", "column_name"])
    params: dict[str, object] = {"monitor_version": version, "now": now}
    stmts = ["BEGIN TRANSACTION;"]
    for i, g in enumerate(m.granularities or [None]):
        cut = f"@cut_{i}"
        params[f"cut_{i}"] = (now if g is None else
                              window_add(window_floor(now, g), g, -m.anomaly_detection.detection_windows))
        gran = "granularity IS NULL" if g is None else f"granularity = {_string(g)}"
        stmts += [f"DELETE FROM {q(table)} WHERE {gran} AND `window`.start >= {cut};",
                  anomaly_insert_sql(m, profile_table, table, g, cut) + ";"]
    stmts.append("COMMIT TRANSACTION;")
    _run(client, "\n".join(stmts), params)
    log(f"{table} judged ({m.anomaly_detection.model})")


# --- the refresh --------------------------------------------------------------------------

def config_hash(m: QualityMonitor) -> str:
    return hashlib.sha256(m.model_dump_json(exclude_none=True).encode()).hexdigest()[:16]


def next_version(state: dict | None, m: QualityMonitor) -> tuple[int, dict]:
    """monitor_version: 1 for a new monitor, one more each time its configuration changes."""
    h = config_hash(m)
    if state and state.get("config_hash") == h:
        return int(state["version"]), state
    version = int((state or {}).get("version") or 0) + 1
    return version, {"version": version, "config_hash": h}


def _param(name: str, value):
    from google.cloud import bigquery

    kind = ("TIMESTAMP" if isinstance(value, datetime) else "INT64" if isinstance(value, int)
            else "STRING")
    return bigquery.ScalarQueryParameter(name, kind, value)


def _run(client, sql: str, params: dict | None = None) -> None:
    from google.cloud import bigquery

    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[_param(k, v) for k, v in (params or {}).items()]))
    job.result()


def _fields(client, table: str) -> list[tuple[str, str, str]]:
    return [(f.name, f.field_type, f.mode) for f in client.get_table(table).schema]


def schema_fields(columns: dict[str, object]):
    """google.cloud.bigquery SchemaFields for a column spec."""
    from google.cloud.bigquery import SchemaField

    def field(name: str, spec) -> SchemaField:
        mode = "NULLABLE"
        if isinstance(spec, tuple) and spec[0] == "REPEATED":
            mode, spec = "REPEATED", spec[1]
        if isinstance(spec, tuple):  # ("RECORD", [...])
            spec = spec[1]
        if isinstance(spec, list):
            return SchemaField(name, "RECORD", mode=mode, fields=[field(n, s) for n, s in spec])
        return SchemaField(name, spec, mode=mode)
    return [field(n, s) for n, s in columns.items()]


def _ensure_table(client, table: str, columns: dict[str, object], extra: dict[str, str], cluster: list[str]):
    """The metric table, created on the first refresh; a custom metric's column added when new."""
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    wanted = schema_fields(columns) + [bigquery.SchemaField(n, t) for n, t in extra.items()]
    try:
        t = client.get_table(table)
    except NotFound:
        t = bigquery.Table(table, schema=wanted)
        t.clustering_fields = cluster
        client.create_table(t)
        return
    have = {f.name for f in t.schema}
    missing = [f for f in wanted if f.name not in have]
    if missing:
        t.schema = [*t.schema, *missing]
        client.update_table(t, ["schema"])


def _stage(client, staging: str, selects: list[tuple[str, dict]]) -> None:
    """Each SELECT's rows into staging, the first creating it (expiring in a day, should the
    refresh die before dropping it)."""
    for i, (sql, params) in enumerate(selects):
        head = (f"CREATE TABLE {q(staging)}\nOPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), "
                f"INTERVAL 1 DAY))\nAS\n" if i == 0 else f"INSERT INTO {q(staging)}\n")
        _run(client, head + sql, params)


def refresh(m: QualityMonitor, *, client, state: dict | None, now: datetime | None = None,
            log=print) -> dict:
    """One refresh, computed in BigQuery: profile the table over the lookback (or now, for a
    snapshot) and the baseline into a staging table, then in one script compute drift and
    replace what this refresh recomputes in both metric tables, in one transaction. Returns
    the new state (monitor_version and the configuration it is for)."""
    now = now or datetime.now(UTC)
    version, new_state = next_version(state, m)
    profile_table, drift_table = m.output_tables()
    il = m.inference_log
    fields = _fields(client, m.table_name)
    types = {n: t for n, t, _ in fields}
    for col in filter(None, [m.timestamp_col, il and il.prediction_col, il and il.model_id_col,
                             il and il.label_col, il and il.prediction_proba_col]):
        if col not in types:
            raise ValueError(f"{m.table_name} has no column {col!r}")
    if il and il.prediction_proba_col:
        _check_proba(client, m)
    _same_location(client, m)
    columns = profiled_columns(fields, {m.timestamp_col} if m.timestamp_col else set())
    for cm in (*_aggregates(m), *_derived(m)):
        render_definition(cm.definition, m, cm.input_columns[0] if cm.input_columns[0] != TABLE else None)
    _ensure_table(client, profile_table, PROFILE_COLUMNS,
                  {cm.name: cm.bigquery_type for cm in (*_aggregates(m), *_derived(m))},
                  ["log_type", "granularity", "column_name"])
    _ensure_table(client, drift_table, DRIFT_COLUMNS, {cm.name: cm.bigquery_type for cm in _drifts(m)},
                  ["drift_type", "granularity", "column_name"])

    stamp = f"{now:%Y%m%d%H%M%S}"
    staging = f"{profile_table}__staging_{stamp}"
    conf_staging = f"{profile_table}__confusion_{stamp}" if _classification(m) else None
    proba_staging = f"{profile_table}__proba_{stamp}" if _probabilities(m) else None
    base = {"monitor_version": version, "now": now}
    scans: list[tuple[Scan, list[Column], dict]] = []
    if m.baseline_table_name:
        bfields = _fields(client, m.baseline_table_name)
        bnames = {n for n, _, _ in bfields}
        scan = Scan(table=m.baseline_table_name, log_type="BASELINE", slices=m.slicing_exprs,
                    by_model=bool(il and il.model_id_col in bnames))
        scans.append((scan, [c for c in columns if c.name in bnames], {}))
    consecutive_after: dict[str, str] = {}
    script_params = dict(base)
    if m.snapshot is not None:
        scans.append((Scan(table=m.table_name, log_type="INPUT", slices=m.slicing_exprs, snapshot=True), columns, {}))
        profile_where, drift_where = "log_type = 'BASELINE'", "FALSE"
    else:
        ts_col = m.timestamp_col
        ts_expr = timestamp_sql(ts_col, types[ts_col])
        conds_p, conds_d = [], []
        for i, g in enumerate(m.granularities):
            start = scan_start(now, g, m.lookback_days or LOOKBACK_DAYS)
            scans.append((Scan(table=m.table_name, log_type="INPUT", granularity=g, window=window_sql(ts_expr, g),
                               ts=(ts_col, types[ts_col]), by_model=bool(il), slices=m.slicing_exprs),
                          columns, {"from_ts": start, "to_ts": now}))
            script_params[f"from_{i}"] = start
            consecutive_after[g] = f"@from_{i}"
            conds_p.append(f"(granularity = {_string(g)} AND `window`.start >= @from_{i})")
            conds_d.append(f"(granularity = {_string(g)} AND `window`.start >= @from_{i} AND "
                           f"(drift_type = 'BASELINE' OR `window`.start > @from_{i}))")
        profile_where = f"(log_type = 'INPUT' AND ({' OR '.join(conds_p)})) OR log_type = 'BASELINE'"
        drift_where = " OR ".join(conds_d)

    try:
        profile_selects, confusion_selects, proba_selects = [], [], []
        for scan, cols, params in scans:
            if scan.log_type == "BASELINE":
                scan = _baseline_slices(client, m, scan, cols, log)
            profile_selects.append((profile_rows_sql(m, scan, cols), base | params))
            if conf_staging:
                confusion_selects.append((confusion_sql(m, scan), base | params))
            if proba_staging and (scan.log_type == "INPUT" or il.prediction_proba_col in bnames):
                proba_selects.append((proba_sql(m, scan), base | params))
        _stage(client, staging, profile_selects)
        if conf_staging:
            _stage(client, conf_staging, confusion_selects)
        if proba_staging:
            _stage(client, proba_staging, proba_selects)
        log(f"staged {len(profile_selects)} scan(s) in {staging}")
        script = "\n".join([
            UDFS,
            profile_temp_sql(m, staging, conf_staging, proba_staging),
            drift_sql("prof", previous=profile_table if m.snapshot is not None else None,
                      consecutive_after=consecutive_after, m=m),
            "BEGIN TRANSACTION;",
            f"DELETE FROM {q(profile_table)} WHERE {profile_where};",
            profile_insert_sql(m, profile_table) + ";",
            f"DELETE FROM {q(drift_table)} WHERE {drift_where};",
            drift_insert_sql(drift_table, m) + ";",
            "COMMIT TRANSACTION;"])
        _run(client, script, script_params)
    finally:
        for t in filter(None, [staging, conf_staging, proba_staging]):
            client.delete_table(t, not_found_ok=True)
    log(f"{profile_table} and {drift_table} refreshed (monitor_version {version})")
    if m.anomaly_detection:
        detect_anomalies(m, client=client, version=version, now=now, log=log)
    return new_state


def _check_proba(client, m: QualityMonitor) -> None:
    """prediction_proba_col holds Databricks' map as BigQuery does: ARRAY<STRUCT<key, value>>."""
    col = m.inference_log.prediction_proba_col
    f = next(x for x in client.get_table(m.table_name).schema if x.name == col)
    sub = {x.name: x.field_type.upper() for x in (getattr(f, "fields", None) or [])}
    if (f.mode != "REPEATED" or f.field_type.upper() not in ("RECORD", "STRUCT") or set(sub) != {"key", "value"}
            or sub["value"] not in NUMERIC):
        raise ValueError(f"{m.table_name}.{col}: prediction_proba_col is each class label's probability, "
                         "ARRAY<STRUCT<key, value>> (Databricks' map) with a numeric value")


def _same_location(client, m: QualityMonitor) -> None:
    """BigQuery reads and writes one location per query: the monitored table, its baseline and
    the output schema must share it."""
    where = {m.table_name: client.get_table(m.table_name).location,
             m.output_schema_name: client.get_dataset(m.output_schema_name).location}
    if m.baseline_table_name:
        where[m.baseline_table_name] = client.get_table(m.baseline_table_name).location
    if len({str(v).lower() for v in where.values()}) > 1:
        raise ValueError("a monitor's tables and output schema must be in one BigQuery location: "
                         + ", ".join(f"{k} is in {v}" for k, v in where.items()))


def _baseline_slices(client, m: QualityMonitor, scan: Scan, cols: list[Column], log) -> Scan:
    """The baseline's scan, without the slicing expressions its columns can't evaluate (a dry
    run tells, billing nothing): Databricks profiles a baseline missing a column best-effort."""
    from google.cloud import bigquery

    if not scan.slices:
        return scan
    try:
        client.query(scan_sql(m, scan, cols), job_config=bigquery.QueryJobConfig(
            dry_run=True, query_parameters=[_param("now", datetime.now(UTC))]))
        return scan
    except Exception as e:  # noqa: BLE001 - BigQuery's 400 for an expression the table can't hold
        log(f"baseline {scan.table}: profiled without slices ({str(e).splitlines()[0]})")
        return scan.model_copy(update={"slices": []})


def main_refresh(key: str, config: str, *, project: str, bucket: str, state_object: str, log=print) -> int:
    """`mlops-task monitor`: one refresh, its monitor_version kept in GCS beside the bundle. Its
    queries run where the tables are (BigQuery infers the location), not in the bundle's region."""
    from google.cloud import storage

    from gcpbricks import job_labels

    m = QualityMonitor.model_validate(json.loads(config))
    blob = storage.Client(project=project).bucket(bucket).blob(state_object)
    state = json.loads(blob.download_as_text()) if blob.exists() else None
    log(f"quality monitor {key}: {m.profile_type} on {m.table_name}")
    new = refresh(m, client=job_labels.bigquery_client(project=project), state=state, log=log)
    if new != state:
        blob.upload_from_string(json.dumps(new), content_type="application/json")
    return 0

"""Quality monitors: Databricks' quality_monitors keys, the refresh job each becomes, the scan's
SQL, the metric rows and drift statistics, and a refresh against a fake BigQuery."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError
from scipy import stats

from gcpbricks import bundle as B
from gcpbricks import monitors as M

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.9.8"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
INFERENCE = {"problem_type": "PROBLEM_TYPE_REGRESSION", "prediction_col": "pred", "label_col": "y",
             "model_id_col": "model_id", "timestamp_col": "ts", "granularities": ["1 day"]}
CLASSIFIER = {**INFERENCE, "problem_type": "PROBLEM_TYPE_CLASSIFICATION"}
MONITOR = {"table_name": "p.ds.preds", "output_schema_name": "p.mon", "inference_log": INFERENCE}


def _load(tmp_path, monitors: dict, jobs: dict | None = None, target=None) -> B.Loaded:
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": {"quality_monitors": monitors, "jobs": jobs or {}}}))
    return B.load(path, target)


def _monitor(**over) -> M.QualityMonitor:
    return M.QualityMonitor.model_validate({**MONITOR, **over})


# --- the resource -------------------------------------------------------------------------

def test_a_monitor_takes_exactly_one_profile_type():
    with pytest.raises(ValidationError, match="one of inference_log, time_series or snapshot"):
        _monitor(snapshot={})
    with pytest.raises(ValidationError, match="one of inference_log"):
        M.QualityMonitor.model_validate({"table_name": "p.d.t", "output_schema_name": "p.o"})


def test_granularities_are_databricks_own():
    with pytest.raises(ValidationError, match="granularities"):
        _monitor(inference_log={**INFERENCE, "granularities": ["2 days"]})


def test_what_the_platform_cannot_compute_is_refused():
    with pytest.raises(ValidationError, match="warehouse_id"):
        _monitor(warehouse_id="abc")


def test_probabilities_are_a_classifiers():
    with pytest.raises(ValidationError, match="prediction_proba_col: a classifier's"):
        _monitor(inference_log={**INFERENCE, "prediction_proba_col": "proba"})
    _monitor(inference_log={**CLASSIFIER, "prediction_proba_col": "proba"})


def test_a_drift_metric_compares_current_with_base():
    with pytest.raises(ValidationError, match="compares {{current_df}} with {{base_df}}"):
        _monitor(custom_metrics=[{"type": "CUSTOM_METRIC_TYPE_DRIFT", "name": "d", "input_columns": [":table"],
                                  "definition": "1", "output_data_type": "double"}])


def test_a_custom_metric_cannot_shadow_a_metric_column():
    with pytest.raises(ValidationError, match="already has that column"):
        _monitor(custom_metrics=[{"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "avg",
                                  "input_columns": ["x"], "definition": "avg(x)", "output_data_type": "double"}])


def test_output_data_type_reads_sparks_struct_field_json():
    cm = M.MonitorMetric(type="CUSTOM_METRIC_TYPE_AGGREGATE", name="sq", input_columns=["x"],
                         definition="avg(x*x)",
                         output_data_type='{"name": "output", "type": "double", "nullable": true, "metadata": {}}')
    assert cm.bigquery_type == "FLOAT64"


def test_metric_tables_sit_in_the_output_schema():
    assert _monitor().output_tables() == ("p.mon.preds_profile_metrics", "p.mon.preds_drift_metrics")


# --- the refresh job ----------------------------------------------------------------------

def test_a_monitor_becomes_a_refresh_job_on_the_platform_runtime(tmp_path):
    lb = _load(tmp_path, {"forecast": {**MONITOR, "schedule": {"quartz_cron_expression": "0 0 6 * * ?",
                                                            "timezone_id": "UTC"},
                                   "notifications": {"on_failure": {"email_addresses": ["a@b.c"]}}}})
    job = lb.bundle.resources.jobs["forecast-monitor"]
    assert job.schedule.quartz_cron_expression == "0 0 6 * * ?"
    assert job.email_notifications.on_failure == ["a@b.c"]
    assert job.tags == {"mlops_monitor": "forecast"}
    nc = job.job_clusters[0].new_cluster
    assert nc.docker_image.url.endswith("/runtime:0.9.8")
    w = job.tasks[0].python_wheel_task
    assert (w.package_name, w.entry_point, w.parameters[0]) == ("gcpbricks", "mlops-task", "monitor")
    params = dict(zip(w.parameters[1::2], w.parameters[2::2]))
    assert params["--state-object"] == "dev/.bundle/demo/monitors/forecast.json"
    assert M.QualityMonitor.model_validate(json.loads(params["--config"])).table_name == "p.ds.preds"


def test_the_config_keeps_custom_metric_placeholders_out_of_run_values(tmp_path):
    cm = {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "bias", "input_columns": [":table"],
          "definition": "avg({{prediction_col}} - {{label_col}})", "output_data_type": "double"}
    lb = _load(tmp_path, {"forecast": {**MONITOR, "custom_metrics": [cm]}})
    params = lb.bundle.resources.jobs["forecast-monitor"].tasks[0].python_wheel_task.parameters
    config = params[params.index("--config") + 1]
    assert "{{" not in config
    assert json.loads(config)["custom_metrics"][0]["definition"] == cm["definition"]


def test_resource_references_resolve_inside_the_config(tmp_path):
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": {
        "schemas": {"mon": {"name": "ml_monitoring"}},
        "quality_monitors": {"forecast": {**MONITOR, "output_schema_name": "p.${resources.schemas.mon.name}"}}}}))
    lb = B.load(path, "dev")
    assert lb.bundle.resources.quality_monitors["forecast"].output_schema_name == "p.ml_monitoring_dev"
    params = lb.bundle.resources.jobs["forecast-monitor"].tasks[0].python_wheel_task.parameters
    assert json.loads(params[params.index("--config") + 1])["output_schema_name"] == "p.ml_monitoring_dev"


def test_a_job_named_like_a_refresh_job_is_refused(tmp_path):
    job = {"job_clusters": [{"job_cluster_key": "main", "new_cluster": {"docker_image": {"url": "i"}}}],
           "tasks": [{"task_key": "t", "job_cluster_key": "main",
                      "python_wheel_task": {"package_name": "x", "entry_point": "y"}}]}
    with pytest.raises(ValueError, match="its refresh job"):
        _load(tmp_path, {"forecast": MONITOR}, jobs={"forecast-monitor": job})


def test_a_monitor_key_leaves_room_for_its_job_key(tmp_path):
    with pytest.raises(ValidationError, match="names match"):
        _load(tmp_path, {"m" * 34: MONITOR})


def test_another_job_can_refresh_a_monitor_after_writing_its_table(tmp_path):
    job = {"tasks": [{"task_key": "refresh", "run_job_task": {"job_id": "${resources.jobs.forecast-monitor.id}"}}]}
    lb = _load(tmp_path, {"forecast": MONITOR}, jobs={"serve": job})
    desc = B.job_definition(lb, "serve")["tasks"]["refresh"]
    assert desc["workflow"] == B.workflow_name(lb.bundle, "forecast-monitor", "dev")


def test_deployment_records_each_monitor_for_the_console(tmp_path):
    lb = _load(tmp_path, {"forecast": MONITOR})
    d = B.deployment(lb, "v", "0.9.8", "me", "", "2026-11-16T00:00:00Z")
    qm = d["resources"]["quality_monitors"]["forecast"]
    assert qm["profile_metrics_table"] == "p.mon.preds_profile_metrics"
    assert qm["job"] == "forecast-monitor" and qm["profile_type"] == "InferenceLog"
    assert d["resources"]["jobs"]["forecast-monitor"] == "demo-forecast-monitor-dev"
    drift = {"type": "CUSTOM_METRIC_TYPE_DRIFT", "name": "d", "input_columns": [":table"],
             "definition": "{{current_df}}.c - {{base_df}}.c", "output_data_type": "double"}
    agg = {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "c", "input_columns": [":table"],
           "definition": "count(*)", "output_data_type": "long"}
    lb = _load(tmp_path, {"forecast": {**MONITOR, "custom_metrics": [agg, drift]}})
    qm = B.deployment(lb, "v", "0.9.8", "me", "", "2026-11-16T00:00:00Z")["resources"]["quality_monitors"]["forecast"]
    assert (qm["custom_metrics"], qm["drift_custom_metrics"], qm["prediction_proba_col"]) == (["c"], ["d"], None)
    assert qm["anomalies_table"] is None


# --- windows ------------------------------------------------------------------------------

def test_windows_start_on_monday_midnight_utc():
    ts = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)  # a Wednesday
    assert M.window_floor(ts, "1 week") == datetime(2026, 11, 16, tzinfo=UTC)
    assert M.window_floor(ts, "1 day") == datetime(2026, 11, 18, tzinfo=UTC)
    assert M.window_floor(ts, "30 minutes") == datetime(2026, 11, 18, 15, 30, tzinfo=UTC)
    assert M.window_floor(ts, "1 month") == datetime(2026, 11, 1, tzinfo=UTC)
    assert M.window_add(datetime(2026, 12, 1, tzinfo=UTC), "1 month") == datetime(2027, 1, 1, tzinfo=UTC)
    assert M.window_add(datetime(2026, 1, 1, tzinfo=UTC), "1 month", -1) == datetime(2025, 12, 1, tzinfo=UTC)


def test_a_longer_lookback_reaches_late_labels():
    m = _monitor(lookback_days=45)
    bq = FakeBigQuery({"p.ds.preds": FIELDS})
    M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    staged = next(params for sql, params, dry in bq.queries if "CREATE TABLE" in sql)
    assert staged["from_ts"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert M.config_hash(_monitor()) != M.config_hash(m)
    assert M.config_hash(_monitor()) == M.config_hash(_monitor(lookback_days=None))
    with pytest.raises(ValidationError, match="no lookback"):
        M.QualityMonitor.model_validate({"table_name": "p.ds.t", "output_schema_name": "p.o", "snapshot": {},
                                         "lookback_days": 45})
    with pytest.raises(ValidationError, match="lookback_days"):
        _monitor(lookback_days=0)


def test_a_scan_starts_a_window_before_the_lookback():
    now = datetime(2026, 11, 16, 6, tzinfo=UTC)
    assert M.scan_start(now, "1 day") == datetime(2026, 10, 16, tzinfo=UTC)


def test_window_sql_buckets_from_the_same_origin():
    assert "TIMESTAMP_BUCKET(`ts`, INTERVAL 7 DAY, TIMESTAMP '1970-01-05 00:00:00+00')" == \
        M.window_sql("`ts`", "1 week")
    assert M.window_sql("`ts`", "1 month") == "TIMESTAMP_TRUNC(`ts`, MONTH)"


# --- the scan -----------------------------------------------------------------------------

COLUMNS = [M.Column(name="spend", type="FLOAT64"), M.Column(name="country", type="STRING"),
           M.Column(name="pred", type="FLOAT64"), M.Column(name="y", type="FLOAT64"),
           M.Column(name="model_id", type="STRING")]
DAY = M.Scan(table="p.ds.preds", log_type="INPUT", granularity="1 day", window=M.window_sql("`ts`", "1 day"),
             ts=("ts", "TIMESTAMP"), by_model=True)


def test_profiled_columns_skip_records_arrays_and_the_timestamp():
    fields = [("ts", "TIMESTAMP", "NULLABLE"), ("x", "INT64", "NULLABLE"), ("r", "RECORD", "NULLABLE"),
              ("a", "STRING", "REPEATED"), ("d", "DATE", "NULLABLE")]
    cols = M.profiled_columns(fields, {"ts"})
    assert [(c.name, c.kind) for c in cols] == [("x", "numeric"), ("d", "other")]


def test_the_scan_reads_the_table_once_for_every_slice_and_model():
    m = _monitor(slicing_exprs=["country", "spend > 1000"])
    sql = M.profile_rows_sql(m, DAY.model_copy(update={"slices": m.slicing_exprs}), COLUMNS)
    assert sql.count("FROM `p.ds.preds`") == 1 and "UNION" not in sql
    assert "UNNEST([CAST(`model_id` AS STRING), CAST(NULL AS STRING)]) AS mdl" in sql
    assert "STRUCT('spend > 1000', CAST((spend > 1000) AS STRING))" in sql
    assert "WHERE `ts` >= @from_ts AND `ts` < @to_ts" in sql
    assert f"APPROX_QUANTILES(IF(IS_NAN(CAST(`spend` AS FLOAT64)), NULL, CAST(`spend` AS FLOAT64)), {M.QUANTILES})" in sql
    assert f"APPROX_TOP_COUNT(CAST(`country` AS STRING), {M.TOP_ITEMS})" in sql
    assert "CROSS JOIN UNNEST(ARRAY_CONCAT(s.cols, " in sql


def test_every_profile_row_names_each_staged_column_once():
    sql = M.profile_rows_sql(_monitor(), DAY, COLUMNS)
    staged = [n for n in M.PROFILE_COLUMNS if n not in M._JOINED]
    for n in staged:
        assert sql.count(f" AS `{n}`,") + sql.count(f" AS `{n}`\n") == 1, n
    assert "IF(c.column_name = ':table', 1 - SAFE_DIVIDE(s.q_mse, s.q_label_var), NULL) AS `r2_score`" in sql
    assert "STRUCT(s.window_start AS start, TIMESTAMP_ADD(s.window_start, INTERVAL 1 DAY) AS `end`) AS `window`" in sql


def test_a_baseline_has_no_window_and_a_snapshot_is_now():
    base = M.profile_rows_sql(_monitor(), M.Scan(table="t", log_type="BASELINE"), COLUMNS)
    assert "CAST(NULL AS STRUCT<start TIMESTAMP, `end` TIMESTAMP>) AS `window`" in base
    assert "'BASELINE' AS `log_type`" in base and "WHERE" not in base.split("FROM `t`")[1]
    snap = M.profile_rows_sql(_monitor(), M.Scan(table="t", log_type="INPUT", snapshot=True), COLUMNS)
    assert "STRUCT(@now AS start, @now AS `end`) AS `window`" in snap


def test_a_date_column_filters_on_itself_so_partitions_prune():
    assert M.range_filter("d", "DATE") == "`d` >= DATE(@from_ts) AND `d` < DATE(@to_ts)"
    assert M.timestamp_sql("d", "DATE") == "TIMESTAMP(`d`)"
    assert M.timestamp_sql("e", "INT64") == "TIMESTAMP_SECONDS(`e`)"


def test_window_ends_follow_the_granularity():
    assert M.window_end_sql("x", "2 weeks") == "TIMESTAMP_ADD(x, INTERVAL 14 DAY)"
    assert M.window_end_sql("x", "1 month") == "TIMESTAMP(DATE_ADD(DATE(x), INTERVAL 1 MONTH))"


def test_custom_aggregates_fill_databricks_placeholders():
    m = _monitor(custom_metrics=[
        {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "sq", "input_columns": ["spend", "pred"],
         "definition": "avg(`{{input_column}}` * `{{input_column}}`)", "output_data_type": "double"},
        {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "bias", "input_columns": [":table"],
         "definition": "avg({{prediction_col}} - {{ label_col }})", "output_data_type": "double"}])
    sql = M.profile_rows_sql(m, DAY, COLUMNS)
    assert "(avg(`spend` * `spend`))" in sql and "(avg(`pred` * `pred`))" in sql
    assert "(avg(pred - y)) AS t_cm_bias" in sql
    assert "IF(c.column_name = ':table', s.t_cm_bias, c.cm_bias) AS `bias`" in sql
    assert "IF(c.column_name = ':table', NULL, c.cm_sq) AS `sq`" in sql


def test_a_classifier_counts_its_confusion_keyed_as_its_profile_rows():
    m = _monitor(inference_log={**INFERENCE, "problem_type": "PROBLEM_TYPE_CLASSIFICATION"})
    conf = M.confusion_sql(m, DAY)
    rows = M.profile_rows_sql(m, DAY, COLUMNS)
    assert "GROUP BY _gkey, _fkey, _slice_value, prediction, label" in conf
    window = M.window_sql("`ts`", "1 day")
    assert f"FORMAT('%T', ('INPUT', '1 day', {window}, mdl, s.slice_key, s.slice_value)) AS _gkey" in conf
    assert "FORMAT('%T', ('INPUT', '1 day', s.window_start, s.model_id, s.slice_key, s.slice_value)) AS `_gkey`" in rows
    prof = M.profile_temp_sql(m, "p.mon.stage", "p.mon.conf", None)
    assert "LEFT JOIN (" in prof and "IF(r.column_name = ':table', k.precision, NULL) AS precision" in prof
    assert "CAST(NULL AS FLOAT64) AS log_loss" in prof and "AS fr ON" not in prof


def test_a_regression_writes_typed_nulls_for_classifier_metrics():
    prof = M.profile_temp_sql(_monitor(), "p.mon.stage", None, None)
    assert ("CAST(NULL AS ARRAY<STRUCT<`prediction` STRING, `label` STRING, `count` INT64>>) AS confusion_matrix"
            in prof)
    assert "CAST(NULL AS ARRAY<STRUCT<`key` STRING, `value` FLOAT64>>) AS predictive_parity" in prof
    assert "`_gkey`" not in prof.split("SELECT")[1]
    insert = M.profile_insert_sql(_monitor(), "p.mon.t")
    assert insert.startswith("INSERT INTO `p.mon.t` (`window`") and insert.endswith("FROM prof")


def test_unlabelled_predictions_are_counted_for_statistical_parity():
    conf = M.confusion_sql(_monitor(inference_log={**CLASSIFIER, "label_col": None}), DAY)
    assert "CAST(NULL AS STRING) AS label" in conf and "label IS NOT NULL" not in conf
    labelled = M.confusion_sql(_monitor(inference_log=CLASSIFIER), DAY)
    assert "CAST(`y` AS STRING) IS NOT NULL" not in labelled
    assert "FORMAT('%T', ('INPUT', '1 day'" in conf and "s.slice_value AS _slice_value" in conf
    assert "WHERE label IS NOT NULL" in M.classification_sql("p.mon.conf")


def test_fairness_compares_a_slices_true_group_with_its_false_group():
    m = _monitor(inference_log=CLASSIFIER, slicing_exprs=["spend > 10"])
    prof = M.profile_temp_sql(m, "p.mon.stage", "p.mon.conf", None)
    assert "AS fr ON fr._gkey = r._gkey" in prof
    assert "IF(r.column_name = ':table', fr.statistical_parity, NULL) AS statistical_parity" in prof
    fair = M.fairness_sql("p.mon.conf", labelled=True)
    assert "WHERE _slice_value IN ('true', 'false')" in fair and "HAVING COUNT(DISTINCT sv) = 2" in fair
    assert "ARRAY_AGG(STRUCT(t.cls AS key, t.prec - f.prec AS value) ORDER BY t.cls) AS predictive_parity" in fair
    unlabelled = M.fairness_sql("p.mon.conf", labelled=False)
    assert "CAST(NULL AS ARRAY<STRUCT<key STRING, value FLOAT64>>) AS equal_opportunity" in unlabelled
    assert "AS statistical_parity" in unlabelled


def test_probabilities_are_staged_per_label_class_and_score():
    m = _monitor(inference_log={**CLASSIFIER, "prediction_proba_col": "proba"})
    sql = M.proba_sql(m, DAY)
    assert "CROSS JOIN UNNEST(ARRAY_CONCAT(ARRAY(SELECT AS STRUCT CAST(x.key AS STRING) AS key" in sql
    assert "ARRAY_LENGTH(`proba`) > 0" in sql and sql.endswith("GROUP BY _gkey, label, cls, score")
    prof = M.profile_temp_sql(m, "p.mon.stage", "p.mon.conf", "p.mon.proba")
    assert "IF(r.column_name = ':table', pb.roc_auc_score, NULL) AS roc_auc_score" in prof
    auc = M.probability_sql("p.mon.proba")
    assert "SAFE_DIVIDE(SUM(np * (below + 0.5 * nn)), SUM(np) * SUM(nn)) AS auc" in auc
    assert "LEAST(GREATEST(score, 2.220446049250313e-16), 0.9999999999999998)" in auc


def test_derived_metrics_are_computed_as_rows_are_written():
    m = _monitor(custom_metrics=[
        {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "sq", "input_columns": ["spend"],
         "definition": "avg(`{{input_column}}` * `{{input_column}}`)", "output_data_type": "double"},
        {"type": "CUSTOM_METRIC_TYPE_DERIVED", "name": "rms", "input_columns": ["spend"],
         "definition": "sqrt(sq)", "output_data_type": "double"}])
    prof = M.profile_temp_sql(m, "p.mon.stage", None, None)
    assert "`sq`, `confusion_matrix`" in prof and "(sqrt(sq)) AS `rms`" in prof
    assert "`statistical_parity`, `sq`, `rms`)" in M.profile_insert_sql(m, "p.mon.t")


def test_custom_drift_metrics_compare_current_with_base_on_their_columns():
    m = _monitor(custom_metrics=[
        {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "weighted_error", "input_columns": [":table"],
         "definition": "avg(abs(`{{prediction_col}}` - `{{label_col}}`))", "output_data_type": "double"},
        {"type": "CUSTOM_METRIC_TYPE_DRIFT", "name": "error_rate_delta", "input_columns": [":table"],
         "definition": "{{current_df}}.weighted_error - {{ base_df }}.weighted_error",
         "output_data_type": '{"name": "output", "type": "double", "nullable": true, "metadata": {}}'}])
    sql = M.drift_sql("prof", previous=None, consecutive_after={"1 day": "@from_0"}, m=m)
    assert "percent_distinct, quantiles, frequent_items, non_null_columns, `weighted_error`," in sql
    assert ("IF(c.column_name IN UNNEST([':table']), (c.weighted_error - p.weighted_error), NULL) "
            "AS `error_rate_delta`") in sql
    assert M.drift_insert_sql("p.mon.d", m).endswith("`population_stability_index`, `error_rate_delta` FROM drift")
    assert "error_rate_delta" not in M.profile_insert_sql(m, "p.mon.t")


def test_drift_pairs_consecutive_windows_and_the_baseline():
    sql = M.drift_sql("p.mon.stage", previous=None, consecutive_after={"1 day": "@from_0"})
    assert ("JOIN cur AS p ON p._skey = c._skey AND p.`window`.`end` = c.`window`.start AND "
            "c.`window`.start > CASE c.granularity WHEN '1 day' THEN @from_0 END") in sql
    assert "IF(b1._bkey IS NOT NULL, b1, b0) AS p" in sql
    assert "numeric_drift(c.quantiles, p.quantiles" in sql and "categorical_drift(" in sql


def test_a_snapshot_drifts_from_the_last_snapshot_written():
    sql = M.drift_sql("p.mon.stage", previous="p.mon.t_profile_metrics", consecutive_after={})
    assert "CREATE TEMP TABLE prev AS" in sql and "JOIN prev AS p ON p._skey = c._skey AND TRUE" in sql
    assert "`window`.start < @now" in sql


# --- drift statistics, as BigQuery runs them ----------------------------------------------

node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the JavaScript UDFs")


def _js(body: str, names: list[str], *args):
    src = (f"function f({', '.join(names)}) {{{body}}}\n"
           f"console.log(JSON.stringify(f(...{json.dumps(list(args))})));")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)


def _numeric(qa, qb, na, nb):
    return _js(M.NUMERIC_DRIFT_JS, ["a", "b", "na", "nb"], qa, qb, na, nb)


def _categorical(a: dict, na, b: dict, nb):
    return _js(M.CATEGORICAL_DRIFT_JS, ["ai", "ac", "na", "bi", "bc", "nb"],
               list(a), list(a.values()), na, list(b), list(b.values()), nb)


def _quantiles(xs: list[float]) -> list[float]:
    xs = sorted(xs)
    return [xs[round(i * (len(xs) - 1) / M.QUANTILES)] for i in range(M.QUANTILES + 1)]


@node
def test_ks_and_wasserstein_from_quantiles_match_scipy_on_the_samples():
    rng = random.Random(7)
    a = [rng.gauss(0, 1) for _ in range(5000)]
    b = [rng.gauss(0.3, 1.5) for _ in range(4000)]
    d = _numeric(_quantiles(a), _quantiles(b), len(a), len(b))
    ref = stats.ks_2samp(a, b)
    assert d["ks_test"]["statistic"] == pytest.approx(ref.statistic, abs=0.02)
    assert d["ks_test"]["pvalue"] < 1e-6 and ref.pvalue < 1e-6
    assert d["wasserstein_distance"] == pytest.approx(stats.wasserstein_distance(a, b), rel=0.1)
    assert d["population_stability_index"] >= 0.2


@node
def test_the_same_distribution_does_not_drift():
    rng = random.Random(3)
    a = [rng.gauss(0, 1) for _ in range(3000)]
    b = [rng.gauss(0, 1) for _ in range(3000)]
    d = _numeric(_quantiles(a), _quantiles(b), 3000, 3000)
    assert d["ks_test"]["pvalue"] > 0.05 and d["population_stability_index"] < 0.1
    same = _numeric(_quantiles(a), _quantiles(a), 3000, 3000)
    assert same["population_stability_index"] == pytest.approx(0, abs=1e-12)
    assert same["ks_test"]["statistic"] == 0 and same["wasserstein_distance"] == 0


@node
def test_an_empty_side_has_no_numeric_drift():
    assert _numeric([], [1.0, 2.0], 0, 2) is None


@node
def test_categorical_drift_matches_scipy():
    d = _categorical({"VN": 60, "US": 40}, 100, {"VN": 30, "US": 50, "JP": 20}, 100)
    ref = stats.chi2_contingency([[60, 40, 0], [30, 50, 20]], correction=False)
    assert d["chi_squared_test"]["statistic"] == pytest.approx(ref.statistic)
    assert d["chi_squared_test"]["pvalue"] == pytest.approx(ref.pvalue, rel=1e-6)
    assert d["tv_distance"] == pytest.approx(0.3) and d["l_infinity_distance"] == pytest.approx(0.3)
    from scipy.spatial.distance import jensenshannon
    assert d["js_distance"] == pytest.approx(jensenshannon([0.6, 0.4, 0], [0.3, 0.5, 0.2], base=2))


@node
def test_the_chi_squared_p_value_matches_scipy_across_its_range():
    for k in (2, 5, 30, 150):
        rng = random.Random(k)
        a = {str(i): rng.randint(1, 200) for i in range(k)}
        b = {str(i): rng.randint(1, 200) for i in range(k)}
        d = _categorical(a, sum(a.values()), b, sum(b.values()))
        ref = stats.chi2_contingency([list(a.values()), list(b.values())], correction=False)
        assert d["chi_squared_test"]["pvalue"] == pytest.approx(ref.pvalue, rel=1e-5, abs=1e-12), k


@node
def test_items_past_the_top_are_pooled():
    d = _categorical({"a": 5}, 10, {"a": 5}, 10)
    assert d["tv_distance"] == 0
    assert _categorical({"a": 5}, 10, {"a": 9}, 10)["tv_distance"] == pytest.approx(0.4)


# --- a refresh ----------------------------------------------------------------------------

class _Field:
    def __init__(self, name, field_type, mode="NULLABLE"):
        self.name, self.field_type, self.mode = name, field_type, mode


class FakeBigQuery:
    """What refresh() calls of a google.cloud.bigquery Client: tables' schemas, and every
    statement it runs, with its parameters."""

    def __init__(self, tables: dict[str, list[_Field]], fail_dry_run: bool = False,
                 locations: dict[str, str] | None = None):
        self.tables, self.fail_dry_run, self.locations = dict(tables), fail_dry_run, locations or {}
        self.queries, self.created, self.deleted = [], [], []

    def get_table(self, name):
        from google.api_core.exceptions import NotFound
        if name not in self.tables:
            raise NotFound(name)
        return type("T", (), {"schema": self.tables[name], "location": self.locations.get(name, "us-central1")})()

    def get_dataset(self, name):
        return type("D", (), {"location": self.locations.get(name, "us-central1")})()

    def create_table(self, t):
        self.created.append(t.table_id)
        self.tables[f"{t.project}.{t.dataset_id}.{t.table_id}"] = list(t.schema)

    def update_table(self, t, fields):
        pass

    def query(self, sql, job_config=None):
        if job_config.dry_run and self.fail_dry_run:
            raise ValueError("400 Unrecognized name: country")
        self.queries.append((sql, {p.name: p.value for p in job_config.query_parameters}, job_config.dry_run))
        return type("J", (), {"result": lambda self_: []})()

    def delete_table(self, name, not_found_ok=False):
        self.deleted.append(name)


FIELDS = [_Field("ts", "TIMESTAMP"), _Field("spend", "FLOAT64"), _Field("country", "STRING"),
          _Field("pred", "FLOAT64"), _Field("y", "FLOAT64"), _Field("model_id", "STRING")]
NOW = datetime(2026, 11, 16, 6, tzinfo=UTC)


def test_a_refresh_stages_each_scan_then_replaces_the_lookback_in_one_transaction():
    m = _monitor(baseline_table_name="p.ds.valid", slicing_exprs=["country"],
                 inference_log={**INFERENCE, "granularities": ["1 day", "1 week"]})
    bq = FakeBigQuery({"p.ds.preds": FIELDS, "p.ds.valid": FIELDS[1:]})
    state = M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    assert state["version"] == 1
    assert sorted(bq.created) == ["preds_drift_metrics", "preds_profile_metrics"]
    runs = [(sql, params) for sql, params, dry in bq.queries if not dry]
    staging = "p.mon.preds_profile_metrics__staging_20261116060000"
    assert runs[0][0].startswith(f"CREATE TABLE `{staging}`") and "FROM `p.ds.valid`" in runs[0][0]
    assert [r[0].startswith(f"INSERT INTO `{staging}`") for r in runs[1:3]] == [True, True]
    assert runs[1][1]["from_ts"] == datetime(2026, 10, 16, tzinfo=UTC) and runs[1][1]["to_ts"] == NOW
    assert runs[2][1]["from_ts"] == datetime(2026, 10, 5, tzinfo=UTC)
    script, params = runs[-1]
    assert params == {"monitor_version": 1, "now": NOW, "from_0": datetime(2026, 10, 16, tzinfo=UTC),
                      "from_1": datetime(2026, 10, 5, tzinfo=UTC)}
    assert script.index("CREATE TEMP FUNCTION numeric_drift") < script.index("BEGIN TRANSACTION;")
    assert ("DELETE FROM `p.mon.preds_profile_metrics` WHERE (log_type = 'INPUT' AND ((granularity = '1 day' "
            "AND `window`.start >= @from_0) OR (granularity = '1 week' AND `window`.start >= @from_1))) "
            "OR log_type = 'BASELINE';") in script
    assert script.rstrip().endswith("COMMIT TRANSACTION;")
    assert bq.deleted == [staging]


def test_a_classifier_stages_its_confusion_counts_too():
    m = _monitor(inference_log={**INFERENCE, "problem_type": "PROBLEM_TYPE_CLASSIFICATION"})
    bq = FakeBigQuery({"p.ds.preds": FIELDS})
    M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    conf = "p.mon.preds_profile_metrics__confusion_20261116060000"
    assert any(sql.startswith(f"CREATE TABLE `{conf}`") for sql, _, _ in bq.queries)
    assert f"FROM `{conf}`" in bq.queries[-1][0]
    assert sorted(bq.deleted) == sorted([conf, "p.mon.preds_profile_metrics__staging_20261116060000"])


def test_a_classifier_with_probabilities_stages_them_too():
    proba = _Field("proba", "RECORD", "REPEATED")
    proba.fields = [_Field("key", "STRING"), _Field("value", "FLOAT64")]
    m = _monitor(inference_log={**CLASSIFIER, "prediction_proba_col": "proba"})
    bq = FakeBigQuery({"p.ds.preds": [*FIELDS, proba]})
    M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    staged = "p.mon.preds_profile_metrics__proba_20261116060000"
    assert any(sql.startswith(f"CREATE TABLE `{staged}`") for sql, _, _ in bq.queries)
    assert f"FROM `{staged}`" in bq.queries[-1][0] and staged in bq.deleted
    proba.fields = [_Field("key", "STRING"), _Field("value", "STRING")]
    with pytest.raises(ValueError, match="ARRAY<STRUCT<key, value>>"):
        M.refresh(m, client=FakeBigQuery({"p.ds.preds": [*FIELDS, proba]}), state=None, now=NOW, log=lambda *_: None)


def test_a_snapshot_keeps_earlier_snapshots():
    m = M.QualityMonitor.model_validate({"table_name": "p.ds.preds", "output_schema_name": "p.mon", "snapshot": {}})
    bq = FakeBigQuery({"p.ds.preds": FIELDS})
    M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    script = bq.queries[-1][0]
    assert "DELETE FROM `p.mon.preds_profile_metrics` WHERE log_type = 'BASELINE';" in script
    assert "DELETE FROM `p.mon.preds_drift_metrics` WHERE FALSE;" in script


def test_a_baseline_without_a_slices_columns_is_profiled_whole():
    m = _monitor(baseline_table_name="p.ds.valid", slicing_exprs=["country"])
    bq = FakeBigQuery({"p.ds.preds": FIELDS, "p.ds.valid": FIELDS[1:]}, fail_dry_run=True)
    said = []
    M.refresh(m, client=bq, state=None, now=NOW, log=said.append)
    baseline = next(sql for sql, _, dry in bq.queries if "FROM `p.ds.valid`" in sql and not dry)
    assert "STRUCT('country'" not in baseline
    assert any("profiled without slices" in s for s in said)


def test_a_refresh_refuses_tables_in_two_locations():
    bq = FakeBigQuery({"p.ds.preds": FIELDS}, locations={"p.mon": "asia-southeast1"})
    with pytest.raises(ValueError, match="one BigQuery location: p.ds.preds is in us-central1, p.mon is in asia-southeast1"):
        M.refresh(_monitor(), client=bq, state=None, now=NOW, log=lambda *_: None)
    assert bq.queries == [] and bq.created == []


def test_a_refresh_names_a_missing_column():
    bq = FakeBigQuery({"p.ds.preds": [_Field("ts", "TIMESTAMP"), _Field("pred", "FLOAT64")]})
    with pytest.raises(ValueError, match="no column 'model_id'"):
        M.refresh(_monitor(), client=bq, state=None, log=lambda *_: None)


def test_monitor_version_moves_only_when_the_configuration_does():
    m = _monitor()
    v, state = M.next_version(None, m)
    assert v == 1
    assert M.next_version(state, m) == (1, state)
    assert M.next_version(state, _monitor(slicing_exprs=["country"]))[0] == 2


def test_mlops_task_monitor_runs_a_refresh(monkeypatch):
    from gcpbricks import task_runner

    seen = {}
    monkeypatch.setattr(M, "main_refresh", lambda key, config, **kw: seen.update(key=key, config=config, **kw) or 0)
    assert task_runner.main(["monitor", "--key", "forecast", "--config", "{}", "--project", "p", "--bucket", "b",
                             "--state-object", "o"]) == 0
    assert seen == {"key": "forecast", "config": "{}", "project": "p", "bucket": "b", "state_object": "o"}


# --- anomalies ----------------------------------------------------------------------------

COST_SUM = {"type": "CUSTOM_METRIC_TYPE_AGGREGATE", "name": "cost_sum", "input_columns": [":table"],
            "definition": "SUM(spend)", "output_data_type": "double"}


def test_anomaly_detection_watches_numeric_profile_and_custom_metrics():
    m = _monitor(custom_metrics=[COST_SUM], anomaly_detection={"metrics": ["count", "cost_sum", "percent_null"]})
    assert m.anomalies_table() == "p.mon.preds_anomalies" and _monitor().anomalies_table() is None
    assert m.anomaly_detection.model == "TimesFM 2.5" and m.anomaly_detection.columns == [":table"]
    with pytest.raises(ValidationError, match="each a numeric profile metric"):
        _monitor(anomaly_detection={"metrics": ["frequent_items"]})
    with pytest.raises(ValidationError, match="each a numeric profile metric"):
        _monitor(anomaly_detection={"metrics": ["nope"]})


def test_anomaly_series_are_the_finished_windows_and_counts_fill_zero():
    m = _monitor(custom_metrics=[COST_SUM], slicing_exprs=["country"],
                 anomaly_detection={"metrics": ["count", "cost_sum"], "columns": [":table", "spend"]})
    sql = M.anomaly_series_sql(m, "p.mon.preds_profile_metrics", "1 day")
    assert "log_type = 'INPUT' AND granularity = '1 day' AND `window`.`end` <= @now" in sql
    assert "TO_JSON_STRING(STRUCT(model_id, slice_key, slice_value, column_name, 'cost_sum' AS metric))" in sql
    assert "column_name IN UNNEST([':table', 'spend'])" in sql
    assert "JOIN cal AS c ON c.ts >= k.first" in sql and "IFNULL(r.value, 0) AS value" in sql
    no_count = M.anomaly_series_sql(_monitor(anomaly_detection={"metrics": ["avg"]}), "p.mon.t", None)
    assert "granularity IS NULL" in no_count and "cal AS" not in no_count


def test_a_refresh_judges_the_latest_finished_windows_after_writing_the_metrics():
    m = _monitor(custom_metrics=[COST_SUM], anomaly_detection={"metrics": ["count"], "detection_windows": 3},
                 inference_log={**INFERENCE, "granularities": ["1 day", "1 week"]})
    bq = FakeBigQuery({"p.ds.preds": FIELDS})
    M.refresh(m, client=bq, state=None, now=NOW, log=lambda *_: None)
    assert "preds_anomalies" in bq.created
    script, params = bq.queries[-1][0], bq.queries[-1][1]
    assert "AI.DETECT_ANOMALIES" in script and script.rstrip().endswith("COMMIT TRANSACTION;")
    assert params["cut_0"] == datetime(2026, 11, 13, tzinfo=UTC) and params["cut_1"] == datetime(2026, 10, 26, tzinfo=UTC)
    assert ("DELETE FROM `p.mon.preds_anomalies` WHERE granularity = '1 day' AND `window`.start >= @cut_0;"
            in script)
    assert "INSERT INTO `p.mon.preds_anomalies` (`window`, `granularity`" in script
    assert "'TimesFM 2.5' AS detector" in script

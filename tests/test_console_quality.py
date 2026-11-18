"""The console's Quality tab over a fake GCP: finding a table's monitors in deployment
records, and reading their metric tables for one granularity, model and slice."""
from __future__ import annotations

import pytest
import requests

from gcpbricks.console import bundles as BN
from gcpbricks.console import quality as Q

MONITOR = {"table_name": "p.ds.preds", "profile_type": "InferenceLog",
           "profile_metrics_table": "p.mon.preds_profile_metrics", "drift_metrics_table": "p.mon.preds_drift_metrics",
           "baseline_table_name": None, "slicing_exprs": ["geo"], "granularities": ["1 day", "1 week"],
           "timestamp_col": "ts", "problem_type": "PROBLEM_TYPE_REGRESSION", "prediction_col": "pred",
           "label_col": "y", "model_id_col": "model_id", "custom_metrics": ["bias"],
           "drift_custom_metrics": ["bias_delta"], "anomalies_table": None, "schedule": None,
           "job": "forecast-monitor", "workflow": "demo-forecast-monitor-dev"}
DEPLOYMENTS = [{"bundle": "demo", "target": "dev", "resources": {"quality_monitors": {"forecast": MONITOR}}},
               {"bundle": "other", "target": "prod", "resources": {"jobs": {}}}]


class FakeGcp:
    project = "p"

    def __init__(self, rows: dict[str, list[list]] | None = None, missing: tuple = (), sources: dict | None = None):
        self.queries, self.rows, self.missing, self.sources = [], rows or {}, set(missing), sources or {}

    def bq_get(self, path, project=None):
        _, ds, _, tb = path.split("/")
        if f"{project}.{ds}.{tb}" in self.missing:
            r = requests.Response()
            r.status_code = 404
            raise requests.HTTPError(response=r)
        return {}

    def bq_referenced_tables(self, fqn):
        return self.sources.get(fqn, [fqn])

    def bq_query(self, body):
        self.queries.append(body)
        name = next((k for k in self.rows if k in body["query"]), None)
        fields = [{"name": "model", "type": "STRING"}, {"name": "slice_key", "type": "STRING"},
                  {"name": "slice_value", "type": "STRING"}, {"name": "granularity", "type": "STRING"}]
        rows = self.rows.get(name, [])
        return {"schema": {"fields": fields}, "rows": [{"f": [{"v": v} for v in r]} for r in rows],
                "totalBytesBilled": "1024"}


@pytest.fixture(autouse=True)
def _deployments(monkeypatch):
    Q._SOURCES.clear()
    monkeypatch.setattr(BN, "deployments", lambda gcp: DEPLOYMENTS)
    monkeypatch.setattr(BN, "deployment", lambda gcp, b, t: next(
        (d for d in DEPLOYMENTS if (d["bundle"], d["target"]) == (b, t)), None))


def test_a_tables_monitors_come_from_deployment_records():
    found = Q.monitors(FakeGcp(), "ds", "preds")
    assert [(m["bundle"], m["target"], m["key"]) for m in found] == [("demo", "dev", "forecast")]
    assert Q.monitors(FakeGcp(), "mon", "preds_drift_metrics")[0]["key"] == "forecast"
    assert Q.monitors(FakeGcp(), "ds", "other") == []


def test_metrics_read_one_group_under_a_billing_cap():
    gcp = FakeGcp({"SELECT DISTINCT": [["", "", "", "1 day"], ["v3", "", "", "1 day"], ["", "geo", "VN", "1 day"]]})
    out = Q.metrics(gcp, "demo", "dev", "forecast", model="v3", slice_key="geo", slice_value="VN")
    assert out["granularity"] == "1 day"
    assert out["options"] == {"granularities": ["1 day", "1 week"], "models": ["v3"],
                              "slices": [{"key": "geo", "value": "VN"}]}
    assert out["billed_bytes"] == 3 * 1024
    profile = next(q for q in gcp.queries if "preds_profile_metrics" in q["query"] and "DISTINCT" not in q["query"])
    assert "IFNULL(model_id, '') = @model" in profile["query"] and ", bias FROM" in profile["query"]
    assert "roc_auc_score.one_vs_one.weighted AS roc_auc_weighted" in profile["query"]
    drift = next(q for q in gcp.queries if "preds_drift_metrics" in q["query"])
    assert ", bias_delta FROM" in drift["query"]
    params = {p["name"]: p["parameterValue"]["value"] for p in profile["queryParameters"]}
    assert (params["model"], params["slice_key"], params["slice_value"], params["granularity"]) == \
        ("v3", "geo", "VN", "1 day")
    assert all(q["maximumBytesBilled"] == str(Q.MAX_QUERY_BYTES) for q in gcp.queries)


def test_a_granularity_the_monitor_lacks_is_refused():
    with pytest.raises(ValueError, match="not one of"):
        Q.metrics(FakeGcp(), "demo", "dev", "forecast", granularity="1 hour")


def test_an_unknown_monitor_is_not_found():
    with pytest.raises(KeyError):
        Q.metrics(FakeGcp(), "demo", "dev", "nope")


def test_a_monitors_anomalies_are_read_for_the_same_group():
    DEPLOYMENTS[0]["resources"]["quality_monitors"]["forecast"] = {**MONITOR, "anomalies_table": "p.mon.preds_anomalies"}
    try:
        gcp = FakeGcp()
        out = Q.metrics(gcp, "demo", "dev", "forecast", slice_key="geo", slice_value="VN")
        q = next(x for x in gcp.queries if "preds_anomalies" in x["query"])
        assert "IFNULL(slice_key, '') = @slice_key" in q["query"] and "lower_bound, upper_bound" in q["query"]
        assert out["anomalies"] == [] and out["billed_bytes"] == 4 * 1024
    finally:
        DEPLOYMENTS[0]["resources"]["quality_monitors"]["forecast"] = MONITOR


def test_slices_read_every_slices_table_row_for_one_model():
    gcp = FakeGcp()
    out = Q.slices(gcp, "demo", "dev", "forecast", model="v3")
    q = gcp.queries[0]
    assert "column_name = ':table'" in q["query"] and "slice_key = @" not in q["query"]
    params = {p["name"]: p["parameterValue"]["value"] for p in q["queryParameters"]}
    assert (params["model"], params["granularity"]) == ("v3", "1 day")
    assert out["granularity"] == "1 day" and q["maximumBytesBilled"] == str(Q.MAX_QUERY_BYTES)


def test_a_columns_distribution_reads_its_quantiles_and_the_baselines():
    gcp = FakeGcp()
    Q.column(gcp, "demo", "dev", "forecast", "pred", slice_key="geo", slice_value="VN")
    q = gcp.queries[0]
    assert "quantiles" in q["query"] and "log_type = 'BASELINE' OR" in q["query"]
    params = {p["name"]: p["parameterValue"]["value"] for p in q["queryParameters"]}
    assert (params["column"], params["slice_key"], params["slice_value"]) == ("pred", "geo", "VN")


def test_the_create_dialogs_monitor_becomes_the_bundles_yaml():
    import yaml

    spec = {"table_name": "p.ds.preds", "output_schema_name": "p.mon",
            "inference_log": {"problem_type": "PROBLEM_TYPE_REGRESSION", "prediction_col": "pred",
                              "timestamp_col": "ts", "model_id_col": "model_id", "granularities": ["1 day"]}}
    out = yaml.safe_load(Q.monitor_yaml("forecast", spec)["yaml"])
    assert out == {"resources": {"quality_monitors": {"forecast": spec}}}
    snap = yaml.safe_load(Q.monitor_yaml("s", {"table_name": "p.ds.t", "output_schema_name": "p.mon",
                                               "snapshot": {}})["yaml"])
    assert snap["resources"]["quality_monitors"]["s"]["snapshot"] == {}
    with pytest.raises(ValueError, match="one of inference_log"):
        Q.monitor_yaml("forecast", {"table_name": "p.ds.preds", "output_schema_name": "p.mon"})
    with pytest.raises(ValueError, match="monitor key"):
        Q.monitor_yaml("Bad Key", spec)


def test_a_monitor_on_a_view_shows_on_the_tables_the_view_reads():
    gcp = FakeGcp(sources={"p.ds.preds": ["p.raw.predictions", "p.raw.labels"]})
    found = Q.monitors(gcp, "raw", "predictions")
    assert [(m["key"], m["via"]) for m in found] == [("forecast", "p.ds.preds")]
    assert "via" not in Q.monitors(gcp, "ds", "preds")[0]


def test_a_monitor_never_refreshed_reads_as_set_up_not_as_an_error():
    gcp = FakeGcp(missing=("p.mon.preds_profile_metrics",))
    out = Q.metrics(gcp, "demo", "dev", "forecast")
    assert out["refreshed"] is False and out["profile"] == [] and gcp.queries == []
    assert Q.metrics(FakeGcp(), "demo", "dev", "forecast")["refreshed"] is True


def test_a_deleted_monitors_metric_tables_left_behind_are_named(monkeypatch):
    import json

    config = {"table_name": "p.ds.preds", "output_schema_name": "p.mon", "snapshot": {}}

    class Gcp(FakeGcp):
        def workflows(self):
            return [{"name": "x/demo-forecast-monitor-dev", "labels": {"mlops_bundle": "demo", "mlops_retired": "2026-11-01"},
                     "userEnvVars": {"MLOPS_JOB": "forecast-monitor", "MLOPS_JOB_NAME": "Quality monitor: p.ds.preds",
                                     "MLOPS_TARGET": "dev", "MLOPS_BUCKET": "b", "MLOPS_DEFINITION": "d.json"}}]

        def read_json(self, bucket, name):
            return {"tasks": {"refresh": {"type": "cloud_run", "args": [
                "mlops-task", "monitor", "--key", "forecast", "--config", json.dumps(config)]}}}
    left = Q.leftovers(Gcp(missing=("p.mon.preds_drift_metrics",)), "ds", "preds")
    assert left == [{"key": "forecast", "bundle": "demo", "target": "dev", "retired": "2026-11-01",
                     "tables": ["p.mon.preds_profile_metrics"]}]
    assert Q.leftovers(Gcp(), "ds", "other") == []

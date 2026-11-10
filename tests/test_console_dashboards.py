"""The console's Dashboards over a fake GCP: finding a bundle's dashboards, and each widget's
data from the bundle's runs, a BigQuery dataset, its logs and its alerts."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from gcpbricks.console import dashboards as D

P = "projects/p/locations/r"
NOW = datetime.now(UTC)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


RECORD = {"bundle": "demo", "target": "dev", "version": "abc", "mlops_version": "0.5.2", "mode": "development",
          "deployed_at": _iso(0.5), "deployed_by": "ci@x", "resources": {"jobs": {"j": "demo-j-dev"},
          "experiments": {"m": "m-dev"}, "registered_models": {}, "model_serving_endpoints": {},
          "schemas": {}, "dashboards": {"ops": "dev/.bundle/demo/dashboards/ops.json"}}}
SPEC = {"key": "ops", "title": "[dev] demo ops", "bundle": "demo", "target": "dev", "window": "30d",
        "datasets": {"acc": {"bigquery": "SELECT model, mape, day FROM t WHERE day >= @start", "time": "day"}},
        "widgets": [
            {"title": "Failed", "type": "counter", "source": "runs", "measure": "count", "filter": {"state": "FAILED"}},
            {"title": "Minutes by task", "type": "bar", "source": "tasks", "measure": "avg(duration_min)", "by": ["task"]},
            {"title": "MAPE", "type": "table", "dataset": "acc", "measure": "avg(mape)", "by": ["model"]},
            {"title": "Warnings", "type": "logs", "query": "severity>=WARNING"},
            {"title": "Alerts", "type": "alerts"},
            {"title": "About", "type": "text", "text": "hello"}]}


def _ex(i, state, days_ago, minutes, tasks):
    start = NOW - timedelta(days=days_ago)
    body = json.dumps({"status": state, "tasks": {k: {"status": v} for k, v in tasks.items()}})
    ex = {"name": f"{P}/workflows/demo-j-dev/executions/e{i}", "state": state, "startTime": start.isoformat(),
          "endTime": (start + timedelta(minutes=minutes)).isoformat(), "argument": "{}"}
    ex["result" if state == "SUCCEEDED" else "error"] = body if state == "SUCCEEDED" else {"payload": body}
    return ex


class FakeGcp:
    project = "p"

    def __init__(self):
        self.queries, self.log_filters = [], []

    def workflows(self):
        return [{"name": f"{P}/workflows/demo-j-dev", "labels": {"mlops_bundle": "demo"},
                 "userEnvVars": {"MLOPS_JOB": "j", "MLOPS_TARGET": "dev", "MLOPS_BUCKET": "b",
                                 "MLOPS_DEFINITION": "dev/.bundle/demo/jobs/j.json"}},
                {"name": f"{P}/workflows/other-j", "labels": {"mlops_bundle": "other"},
                 "userEnvVars": {"MLOPS_JOB": "j", "MLOPS_TARGET": "dev", "MLOPS_BUCKET": "b",
                                 "MLOPS_DEFINITION": "dev/.bundle/other/jobs/j.json"}}]

    def executions(self, workflow, n):
        assert workflow == "demo-j-dev"
        return [_ex(1, "FAILED", 1, 5, {"a": "SUCCESS", "b": "FAILED"}),
                _ex(2, "SUCCEEDED", 2, 30, {"a": "SUCCESS", "b": "SUCCESS"}),
                _ex(3, "FAILED", 60, 5, {"a": "FAILED"})]

    def list_objects(self, bucket, prefix, match_glob=None):
        # The bundle's own folder, never the whole bucket.
        assert prefix.startswith(("dev/.bundle/demo/", "dev/.bundle/other/")), prefix
        return [{"name": "dev/.bundle/demo/dashboards/ops.json"}] if prefix == "dev/.bundle/demo/dashboards/" else []

    def read_json(self, bucket, obj):
        return self.read_json_or_none(bucket, obj)

    def read_json_or_none(self, bucket, obj):
        return {"dev/.bundle/demo/deployment.json": RECORD,
                "dev/.bundle/demo/dashboards/ops.json": SPEC}.get(obj)

    def run_jobs(self):
        return [{"name": f"{P}/jobs/demo-j-main-dev", "labels": {"mlops_bundle": "demo", "mlops_target": "dev"}},
                {"name": f"{P}/jobs/other-main-dev", "labels": {"mlops_bundle": "other", "mlops_target": "dev"}}]

    def run_executions(self, job, n):
        assert job == "demo-j-main-dev"
        return [{"name": f"{P}/jobs/{job}/executions/x1", "startTime": _iso(1), "completionTime": _iso(0.9),
                 "succeededCount": 1, "taskCount": 1,
                 "template": {"containers": [{"resources": {"limits": {"memory": "2Gi"}}}]}}]

    def log_entries(self, flt, n=500, newest_first=False):
        self.log_filters.append((flt, newest_first))
        if "mlops_task" in flt:
            ex1, ex2 = (NOW - timedelta(days=1)), (NOW - timedelta(days=2))
            return [{"timestamp": t.isoformat(), "labels": {"execution_id": ex},
                     "jsonPayload": {"task": task, "status": st}}
                    for ex, base in (("e1", ex1), ("e2", ex2))
                    for task, off in (("a", 0), ("b", 2))
                    for t, st in ((base + timedelta(minutes=off), "STARTED"),
                                  (base + timedelta(minutes=off + (1 if task == "a" else 3)), "SUCCESS"))]
        return [{"timestamp": _iso(0.1), "severity": "WARNING", "jsonPayload": {"message": "slow"}}]

    def bq_query(self, body):
        self.queries.append(body)
        return {"jobComplete": True,
                "schema": {"fields": [{"name": "model", "type": "STRING"}, {"name": "mape", "type": "FLOAT"},
                                      {"name": "day", "type": "DATE"}]},
                "rows": [{"f": [{"v": "m1"}, {"v": "0.2"}, {"v": NOW.date().isoformat()}]},
                         {"f": [{"v": "m1"}, {"v": "0.4"}, {"v": NOW.date().isoformat()}]}]}

    def open_alerts(self):
        return [{"policy": {"displayName": "demo j:on_failure (dev)"}, "openTime": _iso(0.2)},
                {"policy": {"displayName": "demo j:on_failure (prod)"}, "openTime": _iso(0.2)},
                {"policy": {"displayName": "other j:on_failure (dev)"}, "openTime": _iso(0.2)}]


@pytest.fixture()
def gcp():
    D._CACHE.clear()
    return FakeGcp()


def test_dashboards_are_found_beside_the_job_definitions(gcp):
    assert [(d["bundle"], d["target"], d["key"], d["widgets"]) for d in D.list_dashboards(gcp)] == [
        ("demo", "dev", "ops", 6)]
    with pytest.raises(KeyError):
        D.dashboard(gcp, "demo", "prod", "ops")


def test_a_runs_counter_reads_the_bundles_runs_in_the_window(gcp):
    assert D.widget(gcp, "demo", "dev", "ops", 0)["rows"] == [[1]]
    assert D.widget(gcp, "demo", "dev", "ops", 0, "90d")["rows"] == [[2]]


def test_a_tasks_widget_reads_each_tasks_duration(gcp):
    out = D.widget(gcp, "demo", "dev", "ops", 1)
    assert out["columns"] == ["task", "avg(duration_min)"]
    assert dict(map(tuple, out["rows"])) == {"a": 1.0, "b": 3.0}


def test_a_dataset_runs_its_sql_with_the_window_and_a_bytes_cap(gcp):
    out = D.widget(gcp, "demo", "dev", "ops", 2)
    assert out["rows"] == [["m1", pytest.approx(0.3)]]
    q = gcp.queries[0]
    assert [p["name"] for p in q["queryParameters"]] == ["start"]
    assert q["maximumBytesBilled"] == str(D.MAX_DATASET_BYTES)


def test_logs_alerts_and_text(gcp):
    logs = D.widget(gcp, "demo", "dev", "ops", 3)
    assert logs["lines"][0]["text"] == "slow"
    assert gcp.log_filters[-1][0].startswith("(severity>=WARNING) AND timestamp>=") and gcp.log_filters[-1][1]
    assert [a["policy"] for a in D.widget(gcp, "demo", "dev", "ops", 4)["alerts"]] == ["demo j:on_failure (dev)"]
    assert D.widget(gcp, "demo", "dev", "ops", 5)["text"] == "hello"


def test_a_widget_is_cached_and_a_bad_one_refused(gcp):
    D.widget(gcp, "demo", "dev", "ops", 2)
    D.widget(gcp, "demo", "dev", "ops", 2)
    assert len(gcp.queries) == 1
    with pytest.raises(KeyError):
        D.widget(gcp, "demo", "dev", "ops", 9)
    with pytest.raises(ValueError, match="window"):
        D.widget(gcp, "demo", "dev", "ops", 0, "3 days")


def test_memory_reads_the_bundles_cloud_run_jobs(gcp, monkeypatch):
    from gcpbricks.console import service as S
    monkeypatch.setattr(S, "memory", lambda g, name, ex: {"limit": "2Gi", "peak_bytes": 1024 ** 3, "peak_fraction": 0.5})
    rows = D._memory_rows(gcp, "demo", "dev")
    assert rows == [{"run_job": "demo-j-main-dev", "state": "SUCCEEDED", "start": rows[0]["start"],
                     "peak_mb": 1024.0, "limit_mb": 2048.0, "peak_fraction": 0.5}]


def test_model_metrics_read_the_experiments_the_deploy_recorded(gcp, monkeypatch):
    from gcpbricks.console import ml as M
    monkeypatch.setattr(M, "experiments", lambda g: [{"id": "1", "name": "m-dev"}, {"id": "2", "name": "other"}])
    monkeypatch.setattr(M, "experiment", lambda g, i: {"runs": [
        {"name": "r1", "state": "COMPLETE", "created": _iso(2), "metrics": {"accuracy": 0.8, "mape": 0.2}}]})
    rows = D._metric_rows(gcp, "demo", "dev")
    assert sorted((r["experiment"], r["metric"], r["value"]) for r in rows) == [
        ("m-dev", "accuracy", 0.8), ("m-dev", "mape", 0.2)]


def test_a_picked_filter_narrows_widgets_and_reaches_the_sql(gcp):
    SPEC["filters"] = {"model": {"dataset": "acc", "field": "model"}}
    SPEC["datasets"]["acc"]["bigquery"] = "SELECT model, mape, day FROM t WHERE day >= @start AND (@model = '' OR model = @model)"
    try:
        assert D.filter_values(gcp, "demo", "dev", "ops", "model") == ["m1"]
        out = D.widget(gcp, "demo", "dev", "ops", 2, None, {"model": "m2"})
        assert out["rows"] == []
        assert {p["name"]: p["parameterValue"]["value"] for p in gcp.queries[-1]["queryParameters"]} == {
            "start": gcp.queries[-1]["queryParameters"][0]["parameterValue"]["value"], "model": "m2"}
        with pytest.raises(ValueError, match="no filter"):
            D.widget(gcp, "demo", "dev", "ops", 2, None, {"nope": "x"})
    finally:
        del SPEC["filters"]
        SPEC["datasets"]["acc"]["bigquery"] = "SELECT model, mape, day FROM t WHERE day >= @start"


def test_bundles_list_their_deploy_record():
    from gcpbricks.console import bundles as BN
    rows = BN.list_bundles(FakeGcp())
    assert [(r["bundle"], r["target"], r["recorded"], r["version"]) for r in rows] == [
        ("demo", "dev", True, "abc"), ("other", "dev", False, None)]


def test_a_bundle_shows_each_target_with_its_record_and_jobs(monkeypatch):
    from gcpbricks.console import bundles as BN
    from gcpbricks.console import service as S
    monkeypatch.setattr(S, "list_jobs", lambda g, target, n: [
        {"workflow": "demo-j-dev", "job": "j", "target": "dev", "bundle": "demo", "description": "",
         "schedule": None, "trigger": None, "runs": []},
        {"workflow": "other-j", "job": "j", "target": "dev", "bundle": "other", "description": "",
         "schedule": None, "trigger": None, "runs": []}])
    b = BN.bundle(FakeGcp(), "demo")
    assert [(t["target"], t["deployment"]["version"], [j["job"] for j in t["jobs"]]) for t in b["targets"]] == [
        ("dev", "abc", ["j"])]
    with pytest.raises(KeyError):
        BN.bundle(FakeGcp(), "nope")

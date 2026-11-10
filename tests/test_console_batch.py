"""The console's Batch serving: batch_serve_tasks from job definitions, runs from their job."""
from __future__ import annotations

import json

import pytest

from gcpbricks.console import batch as B
from gcpbricks.console import service as S

ARG = {"job": "nightly-score", "target": "dev", "region": "r", "models": [{"model": "roas_d90", "job": "d90-dev"},
       {"model": "roas_d30", "job": "d30-dev", "window_days": 7}], "window_days": 30, "utc_offset_hours": 7,
       "max_hours": 2, "service_account": "sa"}
DEFINITION = {"tasks": {
    "features": {"type": "cloud_run", "run_job": "x", "args": ["mlops-task", "dbt", "build"]},
    "score": {"type": "workflow", "workflow": "b-batch-serve", "argument": ARG}}}
JOB = {"workflow": "b-nightly-dev", "job": "nightly", "bundle": "b", "target": "dev", "bucket": "bk", "definition": "d.json"}


def _ex(i, state, error=None):
    ex = {"name": f"projects/p/locations/r/workflows/b-nightly-dev/executions/e{i}", "state": state,
          "startTime": f"2026-10-1{i}T00:00:00Z", "argument": "{}"}
    if error:
        ex["error"] = {"payload": json.dumps({"message": error})}
    return ex


class FakeGcp:
    def __init__(self):
        self.asked = []

    def read_json(self, bucket, obj):
        assert (bucket, obj) == ("bk", "d.json")
        return DEFINITION

    def executions(self, workflow, n):
        self.asked.append((workflow, n))
        return [_ex(2, "FAILED", 'serving failed for: ["roas_d90"]'), _ex(1, "SUCCEEDED")]


@pytest.fixture(autouse=True)
def jobs(monkeypatch):
    monkeypatch.setattr(S, "list_jobs", lambda gcp, target, n: [{**JOB, "schedule": None, "trigger": None, "runs": []}])
    monkeypatch.setattr(S, "_find", lambda gcp, wf: JOB)


def test_only_batch_serve_tasks_are_listed():
    [t] = B.tasks(FakeGcp())
    assert (t["task"], t["window_days"], t["last_run"]) == ("score", 30, None)
    assert [m["model"] for m in t["models"]] == ["roas_d90", "roas_d30"]


def test_a_task_shows_its_models_settings_and_its_jobs_runs():
    gcp = FakeGcp()
    t = B.task(gcp, "b-nightly-dev", "score")
    assert gcp.asked == [("b-nightly-dev", B.RUNS_LIMIT)]
    assert [(r["id"], r["state"]) for r in t["runs"]] == [("e2", "FAILED"), ("e1", "SUCCEEDED")]
    assert "roas_d90" in t["runs"][0]["error_message"]
    assert t["settings"] == {"window_days": 30, "max_hours": 2, "utc_offset_hours": 7, "region": "r"}


def test_a_job_deployed_before_the_slim_task_still_counts_as_batch_serving():
    old = {**ARG, "feature_table": "fs.features", "runs_table": "p.d.runs"}
    assert S.task_kind({"type": "workflow", "argument": old}) == "batch_serve_task"
    assert S.task_kind({"type": "workflow", "argument": ARG}) == "batch_serve_task"


def test_a_task_that_isnt_batch_serving_is_not_found():
    with pytest.raises(KeyError):
        B.task(FakeGcp(), "b-nightly-dev", "features")

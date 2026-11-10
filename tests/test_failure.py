"""failure.why over the console's fakes: a failed task followed down to the line that matters."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from test_console import FakeGcp, P, _ex

from gcpbricks import notify as N
from gcpbricks.console import failure as F
from gcpbricks.console import service as S

RUNNER = f"{P}/workflows/demo-vertex-pipeline/executions/c1"
PIPELINE = "projects/1/locations/r/pipelineJobs/retrain-d7-1"
CUSTOM = "projects/1/locations/r/customJobs/42"


class Gcp(FakeGcp):
    def __init__(self):
        super().__init__()
        self.execs["e1"] = _ex(1, "FAILED", {"b": {"status": "FAILED", "outcome": ""}})
        self.execs["c1"] = {"name": RUNNER, "state": "FAILED", "startTime": "2026-10-14T01:00:11Z",
                            "error": {"payload": json.dumps(f"retrain: {PIPELINE} ended PIPELINE_STATE_FAILED")}}
        self.objects[".bundle/demo/deployment.json"] = (json.dumps({
            "version": "cf8c", "commit": "dc40c32aa", "branch": "main", "deployed_at": "2026-10-20T06:29:08+00:00",
            "resources": {}}).encode(), "1")
        self.queries = []

    def read_json_or_none(self, bucket, obj):
        got = self.objects.get(obj)
        return json.loads(got[0]) if got else None

    def ai_get(self, path, **params):
        if path == PIPELINE:
            return {"displayName": "retrain-d7-1", "state": "PIPELINE_STATE_FAILED",
                    "error": {"message": "The DAG failed because some tasks failed."},
                    "jobDetail": {"taskDetails": [
                        {"taskName": "condition-1", "state": "FAILED", "endTime": "2026-10-14T01:20:12Z",
                         "error": {"message": "failed tasks: [custom-training-job]"}},
                        {"taskName": "custom-training-job", "state": "FAILED", "endTime": "2026-10-14T01:20:11Z",
                         "error": {"message": "The replica workerpool0-0 exited with a non-zero status of 42."},
                         "executorDetail": {"customJobDetail": {"job": CUSTOM}}}]}}
        if path == CUSTOM:
            return {"state": "JOB_STATE_FAILED", "createTime": "2026-10-14T01:07:43Z", "endTime": "2026-10-14T01:19:52Z"}
        return super().ai_get(path, **params)

    def log_entries(self, flt, n=500, newest_first=False):
        self.queries.append(flt)
        if 'resource.type="ml_job"' in flt:
            lines = ["Vertex AI is provisioning job running framework."] * 3 + [
                "fit start", "[LightGBM] [Fatal] [tweedie]: at least one target label is negative", "fit failed"]
            entries = [{"timestamp": f"2026-10-14T01:19:{i:02d}Z", "textPayload": t} for i, t in enumerate(lines)]
            return entries[::-1] if newest_first else entries
        return [
            {"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}},
            {"timestamp": "2026-10-14T01:20:30Z", "jsonPayload": {
                "event": "mlops_task", "task": "b", "status": "FAILED",
                "error": {"message": "Execution failed or cancelled.", "operation": {"name": RUNNER}}}}]


@pytest.fixture()
def gcp():
    S._FINISHED.clear()
    S._LIVE.clear()
    F._DONE.clear()
    return Gcp()


def test_a_failed_task_is_followed_to_its_custom_jobs_log(gcp):
    w = F.why(gcp, "demo-j", "e1")
    assert [p["kind"] for p in w["path"]] == ["run", "task", "runner", "pipeline", "step", "custom_job"]
    assert w["error"].endswith("non-zero status of 42.")
    texts = [line["text"] for line in w["logs"]]
    assert texts.count("Vertex AI is provisioning job running framework.") == 1
    assert texts[-2] == "[LightGBM] [Fatal] [tweedie]: at least one target label is negative"
    assert w["code"] == {"version": "cf8c", "commit": "dc40c32aa", "branch": "main",
                         "deployed_at": "2026-10-20T06:29:08+00:00"}
    assert all("timestamp<=" in q for q in gcp.queries if "ml_job" in q)


def test_a_finished_failure_is_traced_once(gcp):
    F.why(gcp, "demo-j", "e1")
    n = len(gcp.queries)
    F.why(gcp, "demo-j", "e1")
    assert len(gcp.queries) == n


def test_the_email_quotes_the_root_cause(gcp):
    run = S.run_detail(gcp, "demo-j", "e1")
    _, text, body = N.render(run, job="j", target="prod", url="u", why=F.why(gcp, "demo-j", "e1"))
    assert "at least one target label is negative" in text and "Code: main@dc40c32a" in text
    assert "Why it failed" in body and "custom-training-job" in body


def test_a_failed_container_shows_its_own_last_lines():
    S._FINISHED.clear()
    F._DONE.clear()
    g = FakeGcp()
    g.read_json_or_none = lambda bucket, obj: None
    real = g.log_entries
    g.log_entries = lambda flt, n=500, newest_first=False: (
        [{"timestamp": "t", "textPayload": "KeyError: 'cost'"}] if "cloud_run_job" in flt else real(flt, n))
    w = F.why(g, "demo-j", "e1")
    assert [p["kind"] for p in w["path"]] == ["run", "task", "container"] and w["path"][-1]["label"] == "x2"
    assert [line["text"] for line in w["logs"]] == ["KeyError: 'cost'"] and w["code"] == {}


def test_a_run_that_succeeded_names_the_condition_it_skipped_and_the_verdict_behind_it(monkeypatch):
    S._FINISHED.clear()
    F._DONE.clear()
    g = FakeGcp()
    g.execs["e3"] = _ex(3, "SUCCEEDED", {"b": {"status": "SUCCESS", "outcome": ""}})
    g.read_json_or_none = lambda bucket, obj: None
    nodes = [{"key": "c8", "label": "condition-8", "kind": "condition", "condition": "promote == true",
              "state": "SKIPPED", "parent": None},
             {"key": "c4", "label": "condition-4", "kind": "condition", "condition": "intervals == true",
              "state": "SKIPPED", "parent": "c8"},
             {"key": "gate-op", "label": "gate", "kind": "step", "condition": "", "state": "SUCCESS", "parent": None}]
    monkeypatch.setattr(S, "pipeline_run", lambda gcp, wf, ex, key: {"run": {"name": PIPELINE, "link": "L"}, "nodes": nodes})
    g.ai_get = lambda path, **params: {"jobDetail": {"taskDetails": [{"taskName": "gate-op", "outputs": {
        "verdict": {"artifacts": [{"uri": "gs://b/verdict.md", "schemaTitle": "system.Markdown"}]},
        "report": {"artifacts": [{"uri": "gs://b/report.html", "schemaTitle": "system.HTML"}]}}}]}}
    g.objects["verdict.md"] = (b"FAIL: n 0.00 fails >= 1", "1")
    w = F.why(g, "demo-j", "e3")
    assert [p["kind"] for p in w["path"]] == ["run"]
    assert w["skipped"] == [{"task": "b", "pipeline": "L",
                             "skipped": [{"step": "condition-8", "condition": "promote == true"}],
                             "verdicts": [{"step": "gate-op", "name": "verdict", "text": "FAIL: n 0.00 fails >= 1"}]}]


def test_a_failed_run_is_not_searched_for_skipped_steps(gcp, monkeypatch):
    monkeypatch.setattr(S, "pipeline_run", lambda *a: pytest.fail("a failure is traced, not its skipped steps"))
    assert "skipped" not in F.why(gcp, "demo-j", "e1")

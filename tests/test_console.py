"""The console's service logic and API over a fake GCP."""
from __future__ import annotations

import json
import time

import pytest
import requests

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from gcpbricks.console import app as A
from gcpbricks.console import service as S
from gcpbricks.console.gcp import Gcp

P = "projects/p/locations/r"
DEFINITION = {"region": "r", "parameters": {"day": "x"}, "levels": [["a", "fan"], ["b"]],
              "max_concurrent_runs": 1, "queue": False, "timeout_seconds": 0,
              "duration_warning_seconds": 0, "watchdog": "",
              "tasks": {"a": {"type": "condition", "depends_on": [], "run_if": "ALL_SUCCESS",
                              "op": "GREATER_THAN", "left": "{{job.parameters.day}}", "right": "2"},
                        "fan": {"type": "for_each", "depends_on": [], "run_if": "ALL_SUCCESS",
                                "inputs": "[1, 2]", "task": {"type": "cloud_run", "run_job": "demo-j-py"}},
                        "b": {"type": "cloud_run", "run_job": "demo-j-py", "run_if": "ALL_SUCCESS",
                              "depends_on": [{"task_key": "a", "outcome": "true"}]}}}


def _ex(i, state, tasks=None, params=None):
    body = {"tasks": tasks or {}}
    ex = {"name": f"{P}/workflows/demo-j/executions/e{i}", "state": state,
          "startTime": "2026-10-14T01:00:00Z", "argument": json.dumps({"params": params or {}})}
    if state == "SUCCEEDED":
        ex["result"] = json.dumps({"status": "SUCCEEDED", **body})
    elif state == "FAILED":
        ex["error"] = {"payload": json.dumps({"message": "x", **body})}
    return ex


class FakeGcp:
    project, region = "p", "r"

    def __init__(self):
        self.calls, self.audits, self.objects = [], [], {}
        self.execs = {"e1": _ex(1, "FAILED", {"a": {"status": "SUCCESS", "outcome": "true"},
                                              "fan": {"status": "SUCCESS", "outcome": ""},
                                              "b": {"status": "FAILED", "outcome": ""}}, {"day": "1"}),
                      "e2": _ex(2, "ACTIVE")}
        self.run_execs = {f"{P}/jobs/demo-j-py/executions/x1": {
            "startTime": "2026-10-14T01:00:20Z", "completionTime": "2026-10-14T01:05:00Z",
            "template": {"containers": [{"resources": {"limits": {"memory": "512Mi"}}}]}},
                          f"{P}/jobs/demo-j-py/executions/x2": {}}
        self.series = []
        self.definition, self.labels = DEFINITION, {"mlops_bundle": "demo"}
        self.source = '- only: ${map.get(given, "only")}'

    def workflows(self):
        return [{"name": f"{P}/workflows/demo-j", "labels": self.labels,
                 "userEnvVars": {"MLOPS_JOB": "j", "MLOPS_TARGET": "prod", "MLOPS_BUCKET": "b",
                                 "MLOPS_DEFINITION": ".bundle/demo/jobs/j.json"},
                 "sourceContents": self.source},
                {"name": f"{P}/workflows/demo-vertex-pipeline", "labels": {}}]

    def workflow(self, name):
        return next(w for w in self.workflows() if w["name"].endswith("/" + name))

    def schedulers(self):
        return [{"name": f"{P}/jobs/demo-j-prod", "schedule": "0 2 * * *", "timeZone": "UTC",
                 "state": "ENABLED"}]

    def executions(self, workflow, n):
        return list(self.execs.values())[:n]

    def execution(self, workflow, execution_id):
        return self.execs[execution_id]

    def read_json(self, bucket, obj):
        return self.definition

    def read_object(self, bucket, obj):
        return self.objects.get(obj)

    def write_object(self, bucket, obj, data, content_type, if_generation=None):
        current = self.objects.get(obj, (b"", "0"))[1]
        if if_generation is not None and if_generation != current:
            r = requests.Response()
            r.status_code = 412
            raise requests.HTTPError(response=r)
        self.objects[obj] = (data, str(int(current) + 1))
        return self.objects[obj][1]

    def log_entries(self, flt, n=500):
        if "cloud_run_job" in flt:
            return [{"timestamp": "t", "textPayload": "hello", "labels": {
                "run.googleapis.com/execution_name": "x2"}}]
        return [
            {"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}},
            {"timestamp": "2026-10-14T01:00:20Z", "jsonPayload": {"event": "mlops_task_execution", "task": "b",
                                                "execution": f"{P}/jobs/demo-j-py/executions/x1"}},
            {"timestamp": "2026-10-14T01:00:30Z", "jsonPayload": {"event": "mlops_task_execution", "task": "b",
                                                "execution": f"{P}/jobs/demo-j-py/executions/x2"}},
            {"timestamp": "2026-10-14T01:00:40Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "FAILED"}},
        ]

    def start(self, workflow, argument):
        self.calls.append(("start", workflow, argument))
        return {"name": f"{P}/workflows/{workflow}/executions/new"}

    def cancel(self, workflow, execution_id):
        self.calls.append(("cancel", execution_id))

    def time_series(self, flt, start, end, aligner, period="60s"):
        self.series.append((flt, start, end))
        peak = 0.47 if "utilizations" in flt else 251278421.0
        return [{"points": [{"value": {"doubleValue": peak / 2}}, {"value": {"doubleValue": peak}}]}]

    def run_execution(self, name):
        return self.run_execs[name]

    def cancel_run_execution(self, name):
        self.calls.append(("cancel_container", name))

    def set_scheduler(self, name, paused):
        self.calls.append(("pause" if paused else "resume", name))

    def delete_workflow(self, name):
        self.calls.append(("delete_workflow", name))

    def delete_object(self, bucket, obj):
        self.calls.append(("delete_object", obj))

    def audit(self, payload):
        self.audits.append(payload)


@pytest.fixture()
def gcp():
    S._FINISHED.clear()
    S._TASK_SECS.clear()
    S._MEMORY.clear()
    S._RUN_OF.clear()
    S._LIVE.clear()
    S._EXCLUDED.clear()
    return FakeGcp()


def test_jobs_are_the_workflows_with_a_job_env(gcp):
    jobs = S.list_jobs(gcp)
    assert [(j["bundle"], j["job"], j["target"]) for j in jobs] == [("demo", "j", "prod")]
    assert jobs[0]["schedule"] == {"name": "demo-j-prod", "cron": "0 2 * * *", "time_zone": "UTC",
                                   "paused": False}
    assert jobs[0]["runs"][0]["tasks"] == {"SUCCESS": 2, "FAILED": 1}


def test_the_graph_follows_levels_and_dependencies(gcp):
    g = S.job_detail(gcp, "demo-j")["graph"]
    assert {n["key"]: n["level"] for n in g["nodes"]} == {"a": 0, "fan": 0, "b": 1}
    assert g["edges"] == [{"from": "a", "to": "b", "outcome": "true"}]
    assert next(n for n in g["nodes"] if n["key"] == "fan")["for_each"] is True


def test_a_run_shows_each_tasks_timeline_and_containers(gcp):
    d = S.run_detail(gcp, "demo-j", "e1")
    b = d["task_runs"]["b"]
    assert (b["status"], b["start"], b["end"]) == ("FAILED", "2026-10-14T01:00:10Z", "2026-10-14T01:00:40Z")
    assert [x["name"].rsplit("/", 1)[-1] for x in b["executions"]] == ["x1", "x2"]
    assert d["task_runs"]["a"]["status"] == "SUCCESS" and d["params"] == {"day": "1"}


def test_task_logs_read_its_containers(gcp):
    assert S.task_logs(gcp, "demo-j", "e1", "b")[0]["text"] == "hello"
    assert S.task_logs(gcp, "demo-j", "e1", "a") == []


def test_actions_are_audited_with_the_user(gcp):
    S.run_now(gcp, "u@x.com", "demo-j", {"day": "2"})
    assert gcp.calls[-1] == ("start", "demo-j", {"params": {"day": "2"}})
    r = S.repair(gcp, "u@x.com", "demo-j", "e1")
    assert r["reused"] == ["a", "fan"] and gcp.calls[-1][2]["params"] == {"day": "1"}
    assert [a["action"] for a in gcp.audits] == ["run", "repair"]
    assert all(a["user"] == "u@x.com" for a in gcp.audits)


def test_a_repair_shows_and_takes_a_batch_serve_tasks_models(gcp):
    child = f"{P}/workflows/demo-batch-serve/executions/c1"
    served = {"run_id": "r1", "started_ms": 1, "models": [
        {"model": "d30", "from_date": "a", "to_date": "b", "run_id": "r1", "status": "SUCCEEDED"},
        {"model": "d90", "from_date": "a", "to_date": "b", "run_id": "r1", "status": "FAILED"}]}
    gcp.execs["e3"] = _ex(3, "FAILED", {"serve": {"status": "FAILED", "outcome": "", "execution": child}})
    gcp.execs["c1"] = {"state": "FAILED", "error": {"payload": json.dumps({"message": "x", **served})}}
    assert S.repair_plan(gcp, "demo-j", "e3") == {"models": {"serve": [
        {"model": "d30", "status": "SUCCEEDED"}, {"model": "d90", "status": "FAILED"}]},
        "pipelines": {}, "parameters": {"day": "x"}, "original": {}, "overrides": {}, "latest_repair_id": 0,
        "only": [], "execution": "e3"}
    r = S.repair(gcp, "u", "demo-j", "e3", ["d30"])
    assert r["models"] == {"serve": {"resume": ["d90"], "fresh": ["d30"]}} and r["reused"] == []
    assert gcp.calls[-1][2]["repair"]["serve"]["argument"]["repair"]["run_id"] == "r1"


def test_a_run_of_some_tasks_names_them_only(gcp):
    S.run_now(gcp, "u", "demo-j", {}, ["b"])
    assert gcp.calls[-1] == ("start", "demo-j", {"only": {"b": True}})
    with pytest.raises(ValueError, match="no task"):
        S.run_now(gcp, "u", "demo-j", {}, ["nope"])
    gcp.source = ""
    with pytest.raises(ValueError, match="redeploy its bundle"):
        S.run_now(gcp, "u", "demo-j", {}, ["b"])


def test_a_repair_of_a_run_of_some_tasks_names_them_only(gcp):
    gcp.execs["e5"] = {**_ex(5, "FAILED", {"a": {"status": "FAILED", "outcome": ""},
                                          "b": {"status": "EXCLUDED", "outcome": ""}}),
                       "argument": json.dumps({"only": {"a": True}})}
    assert S.repair_plan(gcp, "demo-j", "e5")["only"] == ["a"]
    S.repair(gcp, "u", "demo-j", "e5")
    assert gcp.calls[-1][2]["only"] == {"a": True}


def _cancelled_run(gcp, monkeypatch):
    gcp.execs["e6"] = {**_ex(6, "CANCELLED"), "endTime": "2026-10-14T01:09:00Z"}
    entries = [
        {"timestamp": "2026-10-14T01:00:05Z", "jsonPayload": {"event": "mlops_task", "task": "a", "status": "STARTED"}},
        {"timestamp": "2026-10-14T01:00:06Z", "jsonPayload": {"event": "mlops_task", "task": "a", "status": "SUCCESS",
                                                              "result": {"status": "SUCCESS", "outcome": "true"}}},
        {"timestamp": "2026-10-14T01:00:05Z", "jsonPayload": {"event": "mlops_task", "task": "fan", "status": "STARTED"}},
        {"timestamp": "2026-10-14T01:00:09Z", "jsonPayload": {"event": "mlops_task", "task": "fan", "status": "SUCCESS"}},
        {"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}}]
    monkeypatch.setattr(gcp, "log_entries", lambda flt, n=500: entries)


def test_a_task_a_cancel_stopped_reads_cancelled_not_running(gcp, monkeypatch):
    _cancelled_run(gcp, monkeypatch)
    b = S.execution_detail(gcp, "demo-j", "e6")["task_runs"]["b"]
    assert (b["status"], b["end"]) == ("CANCELLED", "2026-10-14T01:09:00Z")


def test_a_cancelled_run_is_repaired_reusing_the_successes_it_logged(gcp, monkeypatch):
    _cancelled_run(gcp, monkeypatch)
    r = S.repair(gcp, "u", "demo-j", "e6")
    # fan succeeded under a runner that logged no result: it runs again, as b does.
    assert r["reused"] == ["a"]
    assert gcp.calls[-1][2]["repair"] == {"a": {"status": "SUCCESS", "outcome": "true"}}


def test_a_repair_keeps_the_failed_tasks_dropped_and_reads_them_kept(gcp, monkeypatch):
    S.repair(gcp, "u", "demo-j", "e1", keep=["b"])
    assert gcp.calls[-1][2]["repair"]["b"] == {"status": "FAILED", "outcome": ""}
    assert gcp.audits[-1]["kept"] == ["b"]
    entries = [{"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {
        "event": "mlops_task", "task": "b", "status": "KEPT", "result": {"status": "FAILED", "outcome": ""}}}]
    monkeypatch.setattr(gcp, "log_entries", lambda flt, n=500: entries)
    gcp.execs["e7"] = _ex(7, "ACTIVE")
    b = S.execution_detail(gcp, "demo-j", "e7")["task_runs"]["b"]
    assert (b["status"], b["kept"]) == ("FAILED", True)


def test_a_run_whose_leaf_tasks_all_succeeded_reads_succeeded_with_failures(gcp):
    ok = S._run_summary(_ex(8, "SUCCEEDED", {"a": {"status": "FAILED", "outcome": ""},
                                             "b": {"status": "SUCCESS", "outcome": ""}}))
    assert ok["with_failures"] is True
    assert S._run_summary(_ex(9, "SUCCEEDED", {"b": {"status": "SUCCESS", "outcome": ""}}))["with_failures"] is False


def test_cancel_stops_the_run_and_its_unfinished_containers(gcp):
    S.cancel(gcp, "u", "demo-j", "e1")
    assert ("cancel_container", f"{P}/jobs/demo-j-py/executions/x2") in gcp.calls
    assert ("cancel_container", f"{P}/jobs/demo-j-py/executions/x1") not in gcp.calls
    assert ("cancel", "e1") not in gcp.calls  # a FAILED run has no execution left to cancel


def test_pause_reaches_the_jobs_scheduler(gcp):
    S.set_paused(gcp, "u", "demo-j", True)
    assert gcp.calls == [("pause", "demo-j-prod")]


@pytest.mark.parametrize("call", [
    lambda g: S.run_now(g, "u", "demo-j", {"nope": "1"}),
    lambda g: S.repair(g, "u", "demo-j", "e2"),
])
def test_a_bad_action_is_refused(gcp, call):
    with pytest.raises(ValueError):
        call(gcp)


def test_a_job_shows_its_declared_settings_and_tags(gcp):
    d = S.job_detail(gcp, "demo-j")
    assert d["declared"] is None and d["tags"] == {}
    declared = {"trigger": None, "notifications": [], "job_clusters": [], "environments": []}
    gcp.definition = {**DEFINITION, "settings": declared}
    gcp.labels = {"mlops_bundle": "demo", "mlops_target": "prod", "goog-terraform-provisioned": "true",
                  "team": "ua"}
    d = S.job_detail(gcp, "demo-j")
    assert d["declared"] == declared and d["tags"] == {"team": "ua"}


def test_a_runner_workflow_is_not_a_job(gcp):
    with pytest.raises(KeyError):
        S.job_detail(gcp, "demo-vertex-pipeline")


# --- the API ------------------------------------------------------------------------------

@pytest.fixture()
def client(gcp, monkeypatch):
    monkeypatch.setattr(A, "gcp", lambda: gcp)
    return TestClient(A.app)


def test_the_api_needs_iap(client, monkeypatch):
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    monkeypatch.setenv("MLOPS_IAP_AUDIENCE", "/projects/1/locations/r/services/console")
    assert client.get("/api/jobs").status_code == 401
    r = client.get("/api/jobs", headers={"x-goog-iap-jwt-assertion": "not-a-jwt"})
    assert r.status_code == 401


def test_without_an_audience_only_dev_mode_serves(client, monkeypatch):
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    assert client.get("/api/jobs").status_code == 500
    monkeypatch.setenv("MLOPS_CONSOLE_DEV", "1")
    assert client.get("/api/jobs").json()[0]["job"] == "j"


def test_api_errors_map_to_http(client, monkeypatch):
    monkeypatch.setenv("MLOPS_CONSOLE_DEV", "1")
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    assert client.get("/api/jobs/demo-vertex-pipeline").status_code == 404
    assert client.post("/api/jobs/demo-j/runs/e2/repair").status_code == 400
    assert client.post("/api/jobs/demo-j/run", json={"params": {"day": "3"}}).status_code == 200


def test_a_finished_run_is_read_once(gcp):
    S.run_detail(gcp, "demo-j", "e1")
    gcp.execs["e1"] = None  # a second read would fail
    assert S.run_detail(gcp, "demo-j", "e1")["state"] == "FAILED"


def test_every_log_query_is_time_bounded(gcp):
    seen = []
    real = gcp.log_entries
    gcp.log_entries = lambda flt, n=500: (seen.append(flt), real(flt, n))[1]
    S.task_logs(gcp, "demo-j", "e1", "b")
    assert len(seen) == 2 and all('timestamp>="2026-10-14T00:59:' in f for f in seen)


def test_the_graph_says_what_each_task_runs(gcp):
    nodes = {n["key"]: n for n in S.job_detail(gcp, "demo-j")["graph"]["nodes"]}
    assert nodes["a"]["detail"] == "{{job.parameters.day}} > 2"
    assert nodes["fan"]["detail"] == "[1, 2]: demo-j-py" and nodes["b"]["depends_on"] == ["a"]


def test_a_failed_run_carries_its_error_message(gcp):
    assert S.list_jobs(gcp)[0]["runs"][0]["error_message"] == "x"


# --- a task's page: its attempts, and the logs of every task type ---------------------------

def _parent_run(gcp, child: str, child_state: str = "SUCCEEDED"):
    """Run e3 whose task b started the Workflow execution `child` (workflows/<w>/executions/<id>)."""
    wf, child_id = child.split("/")[1], child.split("/")[3]
    gcp.execs["e3"] = _ex(3, "ACTIVE")
    gcp.execs[child_id] = {**_ex(9, child_state), "name": f"{P}/{child}"}
    real = gcp.log_entries

    def entries(flt, n=500):
        if 'execution_id="e3"' in flt:
            return [
                {"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}},
                {"timestamp": "2026-10-14T01:00:20Z", "jsonPayload": {"event": "mlops_task_execution", "task": "b",
                                                                     "execution": f"{P}/{child}"}},
            ]
        if f'workflow_id="{wf}" AND labels.execution_id="{child_id}"' in flt and "mlops_task" not in flt:
            return [{"timestamp": "2026-10-14T01:00:30Z", "severity": "INFO",
                     "jsonPayload": {"event": "pipeline_started", "job": "pj-1"}}]
        return real(flt, n)
    gcp.log_entries = entries


def test_a_task_run_has_each_attempts_own_state(gcp):
    gcp.run_execs[f"{P}/jobs/demo-j-py/executions/x1"] = {"completionTime": "t", "taskCount": 1, "failedCount": 1}
    t = S.task_run(gcp, "demo-j", "e1", "b")
    assert [(a["id"], a["state"]) for a in t["attempts"]] == [("x1", "FAILED"), ("x2", "RUNNING")]
    assert t["spec"]["run_job"] == "demo-j-py" and t["node"]["depends_on"] == ["a"]
    assert t["run"]["params"] == {"day": "1"}
    with pytest.raises(KeyError):
        S.task_run(gcp, "demo-j", "e1", "nope")


def test_a_failed_task_keeps_its_error(gcp):
    real = gcp.log_entries
    gcp.log_entries = lambda flt, n=500: [
        {**e, "jsonPayload": {**e["jsonPayload"], "error": {"message": "exit 1", "tags": []}}}
        if e["jsonPayload"].get("status") == "FAILED" else e for e in real(flt, n)]
    assert S.run_detail(gcp, "demo-j", "e1")["task_runs"]["b"]["error"] == "exit 1"


def test_a_task_that_ran_a_job_links_its_run_and_shows_its_tasks_output(gcp):
    _parent_run(gcp, "workflows/demo-j/executions/e1")
    t = S.task_run(gcp, "demo-j", "e3", "b")
    assert [(a["kind"], a["workflow"], a["id"], a["job"]) for a in t["attempts"]] == [("workflow", "demo-j", "e1", "j")]
    logs = S.task_logs(gcp, "demo-j", "e3", "b")
    assert [(line["task"], line["text"]) for line in logs] == [("b", "hello")]


def test_a_task_that_ran_a_runner_shows_what_the_runner_logged(gcp):
    _parent_run(gcp, "workflows/demo-vertex-pipeline/executions/v1")
    assert S.task_run(gcp, "demo-j", "e3", "b")["attempts"][0]["job"] is None
    logs = S.task_logs(gcp, "demo-j", "e3", "b")
    assert [json.loads(line["text"]) for line in logs] == [{"event": "pipeline_started", "job": "pj-1"}]


def test_cancel_stops_a_child_run_too(gcp):
    _parent_run(gcp, "workflows/demo-j/executions/e2", child_state="ACTIVE")
    gcp.execs["e2"] = _ex(2, "ACTIVE")
    S.cancel(gcp, "u", "demo-j", "e3")
    assert ("cancel", "e2") in gcp.calls and ("cancel", "e3") in gcp.calls


def test_a_containers_memory_is_its_jobs_peak_within_its_window(gcp):
    attempts = S.task_run(gcp, "demo-j", "e1", "b")["attempts"]
    x1 = next(a for a in attempts if a["id"] == "x1")
    assert x1["memory"] == {"limit": "512Mi", "peak_bytes": 251278421.0, "peak_fraction": 0.47}
    flt, start, end = gcp.series[0]
    assert 'resource.labels.job_name="demo-j-py"' in flt
    assert (start, end) == ("2026-10-14T01:00:20Z", "2026-10-14T01:05:00Z")
    x2 = next(a for a in attempts if a["id"] == "x2")
    assert x2["memory"] == {"limit": None, "peak_bytes": None, "peak_fraction": None}


def test_a_jobs_runs_carry_their_tasks_durations(gcp, monkeypatch):
    entries = [{"timestamp": t, "labels": {"execution_id": "e1"}, "jsonPayload": {"event": "mlops_task", "task": "b", "status": s}}
               for t, s in (("2026-10-14T01:00:10Z", "STARTED"), ("2026-10-14T01:01:40Z", "FAILED"))]
    monkeypatch.setattr(gcp, "log_entries", lambda flt, n=500: entries)
    runs = {r["id"]: r for r in S.job_detail(gcp, "demo-j")["runs"]}
    assert runs["e1"]["task_seconds"] == {"b": 90.0}
    assert runs["e2"]["task_seconds"] == {}
    assert ("demo-j", "e1") in S._TASK_SECS and ("demo-j", "e2") not in S._TASK_SECS


def test_only_admins_delete(client, monkeypatch):
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    A.app.dependency_overrides[A.user] = lambda: "user@example.com"
    try:
        assert client.delete("/api/serving/7").status_code == 403
        assert client.get("/api/me").json()["admin"] is False
        monkeypatch.setattr(A, "ADMINS", {"user@example.com"})
        assert client.get("/api/me").json()["admin"] is True
    finally:
        A.app.dependency_overrides.clear()


def test_a_change_goes_to_gcp_as_its_user(client, gcp, monkeypatch):
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    A.app.dependency_overrides[A.user] = lambda: "user@example.com"
    tokens = []
    monkeypatch.setattr(gcp, "as_user", lambda t: tokens.append(t) or gcp, raising=False)
    try:
        r = client.post("/api/jobs/demo-j/run", json={})
        assert r.status_code == 401 and r.json()["detail"].startswith(A.NEEDS_ACCESS)
        # A token granted to someone else isn't theirs.
        client.cookies.set(A.TOKEN_COOKIE, f"other@example.com|{int(time.time()) + 3600}|tok")
        assert client.post("/api/jobs/demo-j/run", json={}).status_code == 401
        client.cookies.set(A.TOKEN_COOKIE, f"user@example.com|{int(time.time()) + 3600}|tok")
        assert client.post("/api/jobs/demo-j/run", json={}).status_code == 200
        assert tokens == ["tok"]
        assert client.get("/api/jobs").status_code == 200 and tokens == ["tok"]  # reads stay the console's
    finally:
        A.app.dependency_overrides.clear()
        client.cookies.clear()


def test_an_admin_changes_things_as_the_console_without_signing_in(client, gcp, monkeypatch):
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    monkeypatch.setattr(A, "ADMINS", {"user@example.com"})
    monkeypatch.setattr(gcp, "as_user", lambda t: pytest.fail("an admin writes as the console"), raising=False)
    A.app.dependency_overrides[A.user] = lambda: "user@example.com"
    try:
        assert client.post("/api/jobs/demo-j/run", json={}).status_code == 200
    finally:
        A.app.dependency_overrides.clear()


def test_gcp_denying_a_user_is_a_403_with_its_reason(client, gcp, monkeypatch):
    monkeypatch.delenv("MLOPS_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("MLOPS_CONSOLE_DEV", raising=False)
    A.app.dependency_overrides[A.user] = lambda: "user@example.com"
    denied = requests.Response()
    denied.status_code = 403
    denied._content = b'{"error": {"message": "Permission \'workflows.executions.create\' denied"}}'

    def start(*_):
        raise requests.HTTPError(response=denied)
    monkeypatch.setattr(gcp, "as_user", lambda t: gcp, raising=False)
    monkeypatch.setattr(gcp, "start", start)
    client.cookies.set(A.TOKEN_COOKIE, f"user@example.com|{int(time.time()) + 3600}|tok")
    try:
        r = client.post("/api/jobs/demo-j/run", json={})
        assert r.status_code == 403 and "workflows.executions.create" in r.json()["detail"]
    finally:
        A.app.dependency_overrides.clear()
        client.cookies.clear()


def test_as_user_writes_with_the_users_token_and_reads_with_the_consoles():
    class Session:
        def __init__(self):
            self.calls = []

        def _ok(self, method, url):
            self.calls.append(method)
            r = requests.Response()
            r.status_code, r._content = 200, b"{}"
            return r

        def get(self, url, params=None):
            return self._ok("GET", url)

        def post(self, url, json=None):
            return self._ok("POST", url)
    console, user = Session(), Session()
    base = Gcp("p", "r", session=console)
    g = base.as_user("tok")
    assert g.w.headers["Authorization"] == "Bearer tok" and base.w is console
    g.w = user
    g.execution("wf", "e1")
    g.start("wf", {})
    g.set_scheduler("s", True)
    assert console.calls == ["GET"] and user.calls == ["POST", "POST"]


def test_a_pipeline_runs_steps_follow_its_spec_and_a_condition_that_didnt_hold_skips_its_own():
    job = {"pipelineSpec": {
        "root": {"dag": {"tasks": {
            "prepare-op": {"componentRef": {"name": "comp-prepare"}, "taskInfo": {"name": "prepare"}},
            "condition-1": {"componentRef": {"name": "comp-condition-1"}, "dependentTasks": ["prepare-op"],
                            "triggerPolicy": {"condition": "inputs.parameter_values['pipelinechannel--gate-op-promote'] == true"}}}}},
        "components": {"comp-prepare": {}, "comp-promote": {},
                       "comp-condition-1": {"dag": {"tasks": {
                           "promote-op": {"componentRef": {"name": "comp-promote"}, "taskInfo": {"name": "promote"}}}}}}},
        "jobDetail": {"taskDetails": [{"taskName": "prepare-op", "state": "SUCCEEDED", "startTime": "t0"},
                                      {"taskName": "condition-1", "state": "NOT_TRIGGERED"}]}}
    g = S.pipeline_graph(job)
    by = {n["key"]: n for n in g["nodes"]}
    assert (by["prepare-op"]["label"], by["prepare-op"]["state"], by["prepare-op"]["level"]) == ("prepare", "SUCCESS", 0)
    assert (by["condition-1"]["condition"], by["condition-1"]["state"], by["condition-1"]["level"]) == ("promote == true", "SKIPPED", 1)
    assert (by["promote-op"]["state"], by["promote-op"]["level"]) == ("SKIPPED", 2)
    assert {"from": "condition-1", "to": "promote-op"} in g["edges"]


def _started_as(gcp, ex_id, state, tasks):
    """The execution the fake's last start made, as the runner would end it."""
    argument = gcp.calls[-1][2]
    ex = _ex(0, state, tasks)
    gcp.execs[ex_id] = {**ex, "name": f"{P}/workflows/demo-j/executions/{ex_id}", "argument": json.dumps(argument),
                        "startTime": "2026-10-14T02:00:00Z"}
    record = json.loads(gcp.objects[".bundle/demo/jobs/j.runs/e1.json"][0])
    record["repairs"][-1]["execution"] = ex_id
    gcp.objects[".bundle/demo/jobs/j.runs/e1.json"] = (json.dumps(record).encode(), gcp.objects[".bundle/demo/jobs/j.runs/e1.json"][1])


def test_repairs_are_attempts_of_one_run_with_their_own_overrides(gcp):
    gcp.execs.pop("e2")
    r = S.repair(gcp, "u", "demo-j", "e1", params={"day": "5"}, latest_repair_id=0)
    assert (r["run_id"], r["repair_id"]) == ("e1", 1)
    arg = gcp.calls[-1][2]
    assert (arg["repair_of"], arg["repair_id"], arg["params"]) == ("e1", 1, {"day": "5"})
    assert sorted(arg["repair"]) == ["a", "fan"]
    _started_as(gcp, "r1", "FAILED", {"a": {"status": "SUCCESS", "outcome": "true"},
                                      "fan": {"status": "SUCCESS", "outcome": ""}, "b": {"status": "FAILED", "outcome": ""}})

    # A second repair builds on the first: its overrides stay unless dropped.
    with pytest.raises(ValueError, match="latest repair is 1, not 0"):
        S.repair(gcp, "u", "demo-j", "e1", latest_repair_id=0)
    assert S.repair_plan(gcp, "demo-j", "e1")["overrides"] == {"day": "5"}
    S.repair(gcp, "u", "demo-j", "r1", latest_repair_id=1)
    assert gcp.calls[-1][2]["params"] == {"day": "5"} and gcp.calls[-1][2]["repair_id"] == 2
    _started_as(gcp, "r2", "SUCCEEDED", {"a": {"status": "SUCCESS", "outcome": "true"},
                                         "fan": {"status": "SUCCESS", "outcome": ""}, "b": {"status": "SUCCESS", "outcome": ""}})
    with pytest.raises(ValueError, match="only a FAILED or CANCELLED run, or one that succeeded with failures"):
        S.repair(gcp, "u", "demo-j", "e1", params={"day": None})

    # The run lists once, as its latest attempt; its page picks any attempt.
    rows = S.job_detail(gcp, "demo-j")["runs"]
    assert [(x["id"], x["execution"], x["repairs"], x["state"]) for x in rows] == [("e1", "r2", 2, "SUCCEEDED")]
    d = S.run_detail(gcp, "demo-j", "e1")
    assert (d["id"], d["repair_id"], d["latest_repair_id"]) == ("r2", 2, 2)
    assert [(h["type"], h["repair_id"], h["state"]) for h in d["repair_history"]] == [
        ("ORIGINAL", 0, "FAILED"), ("REPAIR", 1, "FAILED"), ("REPAIR", 2, "SUCCEEDED")]
    assert S.run_detail(gcp, "demo-j", "e1", 0)["id"] == "e1"
    assert S.run_detail(gcp, "demo-j", "r1")["repair_id"] == 1  # an attempt's own id opens that attempt
    with pytest.raises(KeyError, match="no repair 7"):
        S.run_detail(gcp, "demo-j", "e1", 7)


def test_a_reused_task_says_which_attempt_ran_it(gcp, monkeypatch):
    gcp.execs.pop("e2")
    S.repair(gcp, "u", "demo-j", "e1")
    _started_as(gcp, "r1", "SUCCEEDED", {"a": {"status": "SUCCESS", "outcome": "true"},
                                         "fan": {"status": "SUCCESS", "outcome": ""}, "b": {"status": "SUCCESS", "outcome": ""}})
    real = gcp.log_entries

    def logs(flt, n=500):
        if 'execution_id="r1"' in flt:
            return [{"timestamp": "2026-10-14T02:00:01Z", "jsonPayload": {"event": "mlops_task", "task": "a", "status": "REUSED"}},
                    {"timestamp": "2026-10-14T02:00:02Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}},
                    {"timestamp": "2026-10-14T02:00:09Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "SUCCESS"}}]
        return real(flt, n)
    monkeypatch.setattr(gcp, "log_entries", logs)
    tasks = S.run_detail(gcp, "demo-j", "e1")["task_runs"]
    assert (tasks["a"]["status"], tasks["a"]["ran_in"]) == ("REUSED", 0)
    assert (tasks["b"]["status"], tasks["b"]["ran_in"]) == ("SUCCESS", 1)
    t = S.task_run(gcp, "demo-j", "r1", "b")
    assert [(h["repair_id"], h["status"]) for h in t["history"]] == [(0, "FAILED"), (1, "SUCCESS")]


def test_a_repair_that_fails_to_start_leaves_no_attempt(gcp, monkeypatch):
    gcp.execs.pop("e2")
    monkeypatch.setattr(gcp, "start", lambda wf, arg: (_ for _ in ()).throw(RuntimeError("quota")))
    with pytest.raises(RuntimeError):
        S.repair(gcp, "u", "demo-j", "e1")
    assert json.loads(gcp.objects[".bundle/demo/jobs/j.runs/e1.json"][0])["repairs"] == []
    with pytest.raises(ValueError, match="parameters"):
        S.repair(gcp, "u", "demo-j", "e1", params={"nope": "1"})


def test_a_running_runs_logs_are_read_once_between_polls(gcp, monkeypatch):
    seen = []
    real = gcp.log_entries
    gcp.log_entries = lambda flt, n=500: (seen.append(flt), real(flt, n))[1]
    gcp.execs["e2"]["startTime"] = "2026-10-14T01:00:00Z"
    S.run_detail(gcp, "demo-j", "e2")
    S.task_run(gcp, "demo-j", "e2", "b")
    assert len([f for f in seen if 'execution_id="e2"' in f]) == 1
    monkeypatch.setattr(S, "LIVE_SECONDS", 0)
    S.run_detail(gcp, "demo-j", "e2")
    assert len([f for f in seen if 'execution_id="e2"' in f]) == 2


def test_gcps_rate_limit_reads_as_a_retry_later(gcp, client, monkeypatch):
    monkeypatch.setenv("MLOPS_CONSOLE_DEV", "1")
    r = requests.Response()
    r.status_code = 429

    def limited(flt, n=500):
        raise requests.HTTPError(response=r)
    gcp.log_entries = limited
    got = client.get("/api/jobs/demo-j/runs/e1")
    assert got.status_code == 503 and "rate-limiting" in got.json()["detail"]


def test_a_child_run_named_only_in_its_failure_is_linked(gcp, monkeypatch):
    child = f"{P}/workflows/demo-vertex-pipeline/executions/c9"
    entries = [
        {"timestamp": "2026-10-14T01:00:10Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "STARTED"}},
        {"timestamp": "2026-10-14T01:20:00Z", "jsonPayload": {"event": "mlops_task", "task": "b", "status": "FAILED", "error": {
            "message": "Execution failed or cancelled.", "operation": {"name": child, "startTime": "2026-10-14T01:00:11Z"}}}}]
    monkeypatch.setattr(gcp, "log_entries", lambda flt, n=500: entries)
    b = S.execution_detail(gcp, "demo-j", "e1")["task_runs"]["b"]
    assert b["executions"] == [{"name": child, "kind": "workflow", "input": "", "start": "2026-10-14T01:00:11Z"}]



PIPELINE_JOB = "projects/1/locations/r/pipelineJobs/retrain-train-prod-1"


def _failed_pipeline_run(gcp):
    runner = f"{P}/workflows/demo-vertex-pipeline/executions/v1"
    gcp.execs["e4"] = _ex(4, "FAILED", {"a": {"status": "SUCCESS", "outcome": "true"},
                                        "train": {"status": "FAILED", "outcome": "", "execution": runner}})
    gcp.execs["v1"] = {"name": runner, "state": "FAILED",
                       "error": {"payload": json.dumps(f"retrain-train: {PIPELINE_JOB} ended PIPELINE_STATE_FAILED")}}
    gcp.ai_get = lambda path, **kw: {"jobDetail": {"taskDetails": [
        {"taskName": "retrain-train-prod-1", "state": "FAILED"}, {"taskName": "prepare-op", "state": "SUCCEEDED"},
        {"taskName": "custom-training-job", "state": "FAILED"}]}} if path == PIPELINE_JOB else {}


def test_a_failed_pipeline_task_is_repaired_as_a_vertex_rerun(gcp):
    _failed_pipeline_run(gcp)
    plan = S.repair_plan(gcp, "demo-j", "e4")["pipelines"]
    assert plan == {"train": {"pipeline_job": PIPELINE_JOB, "steps": [
        {"name": "prepare-op", "state": "SUCCEEDED"}, {"name": "custom-training-job", "state": "FAILED"}]}}
    r = S.repair(gcp, "u", "demo-j", "e4", steps={"train": ["prepare-op"]})
    assert r["pipelines"] == {"train": {"pipeline_job": PIPELINE_JOB, "rerun": ["prepare-op"]}}
    assert gcp.calls[-1][2]["repair"]["train"]["argument"] == {"repair": {"pipeline_job": PIPELINE_JOB, "rerun": ["prepare-op"]}}


def test_a_pipeline_task_can_start_a_new_pipeline_instead(gcp):
    _failed_pipeline_run(gcp)
    r = S.repair(gcp, "u", "demo-j", "e4", fresh=["train"])
    assert r["pipelines"] == {} and "train" not in gcp.calls[-1][2]["repair"]
    with pytest.raises(ValueError, match="ran a pipeline for \\['nope'\\]"):
        S.repair(gcp, "u", "demo-j", "e1", steps={"nope": ["x"]})


def test_a_pipeline_that_succeeded_without_its_gated_steps_says_which_it_skipped(gcp):
    runner = f"{P}/workflows/demo-vertex-pipeline/executions/v2"
    gcp.execs["e5"] = _ex(5, "SUCCEEDED", {"train": {"status": "SUCCESS", "outcome": "", "execution": runner}})
    gcp.execs["v2"] = {"name": runner, "state": "SUCCEEDED", "result": json.dumps({"pipeline_job": PIPELINE_JOB})}
    gcp.ai_get = lambda path, **kw: {"pipelineSpec": {
        "root": {"dag": {"tasks": {
            "gate-op": {"componentRef": {"name": "comp-gate"}, "taskInfo": {"name": "gate"}},
            "condition-1": {"componentRef": {"name": "comp-condition-1"}, "dependentTasks": ["gate-op"]}}}},
        "components": {"comp-gate": {}, "comp-promote": {}, "comp-condition-1": {"dag": {"tasks": {
            "promote-op": {"componentRef": {"name": "comp-promote"}, "taskInfo": {"name": "promote"}}}}}}},
        "jobDetail": {"taskDetails": [{"taskName": "gate-op", "state": "SUCCEEDED"},
                                      {"taskName": "condition-1", "state": "NOT_TRIGGERED"}]}} if path == PIPELINE_JOB else {}
    assert S.run_detail(gcp, "demo-j", "e5")["task_runs"]["train"]["excluded"] == ["promote"]
    # A run still going isn't asked.
    assert "excluded" not in S.run_detail(gcp, "demo-j", "e2")["task_runs"].get("b", {})


# --- a job's name, and a retired job ------------------------------------------------------

def test_a_job_shows_its_display_name_and_whether_it_is_retired(gcp):
    assert {(j["job"], j["name"], j["retired"]) for j in S.list_jobs(gcp)} == {("j", "j", None)}
    gcp.workflows = lambda: [{"name": f"{P}/workflows/demo-j", "labels": {"mlops_bundle": "demo", "mlops_retired": "2026-10-21"},
                              "userEnvVars": {"MLOPS_JOB": "j", "MLOPS_JOB_NAME": "UA data", "MLOPS_TARGET": "prod",
                                              "MLOPS_BUCKET": "b", "MLOPS_DEFINITION": ".bundle/demo/jobs/j.json"}}]
    job = S.list_jobs(gcp)[0]
    assert (job["name"], job["retired"]) == ("UA data", "2026-10-21")
    with pytest.raises(ValueError, match="retired"):
        S.run_now(gcp, "u", "demo-j", {})


def test_only_a_retired_job_with_no_run_going_is_deleted_and_its_deploy_record_forgets_it(gcp):
    with pytest.raises(ValueError, match="not retired"):
        S.delete_job(gcp, "u", "demo-j")
    gcp.labels = {"mlops_bundle": "demo", "mlops_retired": "2026-10-21"}
    with pytest.raises(ValueError, match="still going"):  # e2 is ACTIVE
        S.delete_job(gcp, "u", "demo-j")
    del gcp.execs["e2"]
    gcp.objects[".bundle/demo/deployment.json"] = (json.dumps(
        {"retired_jobs": {"j": {"since": "2026-10-21", "plan": {}}}, "job_plans": {}}).encode(), "3")
    assert S.delete_job(gcp, "u", "demo-j") == {"deleted": "demo-j"}
    assert json.loads(gcp.objects[".bundle/demo/deployment.json"][0])["retired_jobs"] == {}
    assert ("delete_workflow", "demo-j") in gcp.calls and ("delete_object", ".bundle/demo/jobs/j.json") in gcp.calls

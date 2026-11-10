"""A task run's metrics: compute use and cost, BigQuery jobs by label, a dbt command's models."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
import requests

from gcpbricks import job_labels, task_runner
from gcpbricks.console import stats as ST

RUN = "projects/p/locations/r/jobs/dbt-x/executions/dbt-x-abc"


def _job(job_id: str, labels: dict, billed: int = 0, slot_ms: int = 0, spilled: int = 0, sql: str = "") -> dict:
    return {"jobReference": {"projectId": "p", "jobId": job_id, "location": "US"},
            "configuration": {"jobType": "QUERY", "labels": labels, "query": {"query": sql}},
            "status": {"state": "DONE"},
            "statistics": {"creationTime": "1", "totalSlotMs": str(slot_ms),
                           "query": {"totalBytesProcessed": str(billed), "totalBytesBilled": str(billed),
                                     "queryPlan": [{"shuffleOutputBytesSpilled": str(spilled)}]}}}


class FakeGcp:
    project, region = "p", "r"

    def __init__(self, jobs=(), series=None, deny_all_users=False):
        self.jobs, self.series, self.deny = list(jobs), series or {}, deny_all_users
        self.listed = []

    def bq_get(self, path, project=None, **params):
        self.listed.append(params.get("allUsers"))
        if self.deny and params.get("allUsers"):
            r = requests.Response()
            r.status_code = 403
            raise requests.HTTPError(response=r)
        return {"jobs": self.jobs}

    def time_series(self, flt, start, end, aligner, period="60s"):
        metric = flt.rsplit("/", 1)[-1].strip('"') if "allocation_time" not in flt else flt.split("container/")[1].strip('"')
        pts = self.series.get(metric, [])
        return [{"points": [{"interval": {"endTime": t}, "value": {"doubleValue": v}} for t, v in pts]}] if pts else []


def test_quantities():
    assert ST.cpus("1000m") == 1 and ST.cpus("2") == 2 and ST.cpus(None) is None
    assert ST.memory_bytes("512Mi") == 512 * 2 ** 20 and ST.memory_bytes("2Gi") == 2 * 2 ** 30
    assert ST.memory_bytes("1G") == 1e9


def test_a_platform_bigquery_client_labels_its_jobs_with_the_execution(monkeypatch):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    made = {}
    monkeypatch.setattr(bigquery, "Client", lambda **kw: made.update(kw) or "client")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "Serve-ROAS-x7k2")
    assert job_labels.bigquery_client(project="p") == "client"
    assert made["default_query_job_config"].labels == {"mlops_exec": "serve-roas-x7k2"}
    assert made["default_load_job_config"].labels == {"mlops_exec": "serve-roas-x7k2"}
    made.clear()
    monkeypatch.delenv("CLOUD_RUN_EXECUTION")
    monkeypatch.delenv("CLOUD_ML_JOB_ID", raising=False)
    job_labels.bigquery_client(project="p")
    assert made == {"project": "p"}


def test_mlops_task_keeps_each_commands_run_results(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    rr = {"metadata": {"invocation_id": "inv-1"}, "elapsed_time": 4.4, "results": [
        {"unique_id": "model.p.big", "status": "success", "execution_time": 3.0, "relation_name": "`p`.`d`.`big`",
         "timing": [{"name": "execute", "started_at": "s", "completed_at": "e"}],
         "adapter_response": {"job_id": "j1", "location": "US", "project_id": "p", "bytes_processed": 10,
                              "rows_affected": 5, "_message": "x"}}]}

    def runner(argv, **_):
        (project / "target" / "run_results.json").write_text(json.dumps(rr))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "dbt-x-abc")
    files_uri = "gs://b/.bundle/demo/files"
    code = task_runner.run_dbt(project, tmp_path, ["dbt build"], runner,
                               after=lambda i, c: task_runner.save_run_results(project, files_uri, i, c, tmp_path))
    assert code == 0
    kept = json.loads((tmp_path / "b/.bundle/demo/runs/dbt-x-abc/dbt-0.json").read_text())
    assert kept["invocation_id"] == "inv-1" and kept["command"] == "dbt build"
    assert kept["results"][0]["adapter_response"] == {"job_id": "j1", "location": "US", "project_id": "p",
                                                      "bytes_processed": 10, "rows_affected": 5}
    assert not (project / "target" / "run_results.json").exists()


def test_bigquery_jobs_are_found_by_label_and_fall_back_to_the_consoles_own():
    jobs = [_job("a", {"dbt_invocation_id": "inv-1"}, billed=2 ** 40), _job("b", {"mlops_exec": "x"}),
            _job("c", {"other": "y"})]
    g = FakeGcp(jobs, deny_all_users=True)
    got = ST.bigquery_jobs(g, {"dbt_invocation_id": {"inv-1"}, "mlops_exec": {"x"}}, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert [j["job_id"] for j in got] == ["a", "b"]
    assert got[0]["cost_usd"] == ST.BQ_USD_PER_TIB
    assert g.listed == ["true", None]
    assert ST.bigquery_jobs(g, {"mlops_exec": set()}, "2026-01-01T00:00:00Z", None) == []


def test_a_dbt_commands_models_carry_their_jobs_bytes_and_cost():
    results = [{"command": "dbt build", "invocation_id": "inv-1", "results": [
        {"unique_id": "model.p.big", "status": "success", "execution_time": 3.0,
         "adapter_response": {"job_id": "a", "bytes_processed": 9}},
        {"unique_id": "seed.p.s", "status": "success", "execution_time": 1.0, "adapter_response": {}}]}]
    jobs = [ST._bq_row(_job("a", {}, billed=2 ** 30, slot_ms=500))]
    big, seed = ST.dbt_models(results, jobs)
    assert big["name"] == "big" and big["bytes_billed"] == 2 ** 30 and big["slot_ms"] == 500
    assert big["cost_usd"] == pytest.approx(ST.BQ_USD_PER_TIB / 1024)
    assert seed["resource"] == "seed" and seed["cost_usd"] is None


def _dbt_sql(node: str, body: str) -> str:
    return f'/* {{"app": "dbt", "dbt_version": "2.0.0", "node_id": "{node}", "profile_name": "p"}} */\n{body}'


def test_a_models_cost_is_every_job_it_ran_not_only_the_last_one_run_results_names():
    inv = {"dbt_invocation_id": "inv-1"}
    jobs = [ST._bq_row(j) for j in [
        _job("create", inv, billed=2 ** 30, slot_ms=900, sql=_dbt_sql("model.p.tbl", "create or replace table t as ...")),
        _job("drop", inv, sql=_dbt_sql("model.p.tbl", "drop table if exists t__tmp")),
        _job("view", inv, sql=_dbt_sql("model.p.v", "create or replace view v as ...")),
        _job("other_cmd", {"dbt_invocation_id": "inv-2"}, billed=2 ** 40, sql=_dbt_sql("model.p.tbl", "select 1")),
        _job("check", inv, billed=2 ** 20, sql=_dbt_sql("test.p.t1", "select ..."))]]
    results = [{"command": "dbt build", "invocation_id": "inv-1", "results": [
        {"unique_id": "model.p.tbl", "status": "success", "adapter_response": {"job_id": "drop"}},
        {"unique_id": "model.p.v", "status": "success", "adapter_response": {"job_id": "view"}},
        {"unique_id": "test.p.t1", "status": "pass", "adapter_response": {"job_id": "check"}}]}]
    tbl, view, test = ST.dbt_models(results, jobs)
    assert (tbl["jobs"], tbl["bytes_billed"], tbl["slot_ms"], tbl["job_id"]) == (2, 2 ** 30, 900, "create")
    assert tbl["cost_usd"] == pytest.approx(ST.BQ_USD_PER_TIB / 1024)
    assert (view["jobs"], view["cost_usd"]) == (1, 0) and test["bytes_billed"] == 2 ** 20


def test_a_cloud_run_executions_cost_is_its_allocation_or_else_its_limits_for_its_duration():
    ex = {"template": {"containers": [{"resources": {"limits": {"cpu": "2", "memory": "4Gi"}}}]},
          "startTime": "2026-01-01T00:00:00Z", "completionTime": "2026-01-01T00:10:00Z", "taskCount": 1}
    g = FakeGcp(series={"cpu/allocation_time": [("2026-01-01T00:01:00Z", 100.0), ("2026-01-01T00:02:00Z", 20.0)],
                        "memory/allocation_time": [("2026-01-01T00:01:00Z", 240.0)],
                        "utilizations": [("2026-01-01T00:01:00Z", 0.95)]})
    u = ST.cloud_run(g, RUN, ex, "dbt")
    assert u["usage"]["vcpu_seconds"] == 120 and not u["usage"]["estimated"]
    assert u["cost_usd"] == pytest.approx(120 * ST.RUN_USD_PER_VCPU_S + 240 * ST.RUN_USD_PER_GIB_S)
    est = ST.cloud_run(FakeGcp(), RUN, ex, "dbt")
    assert est["usage"]["estimated"] and est["usage"]["vcpu_seconds"] == 2 * 600 and est["usage"]["gib_seconds"] == 4 * 600
    assert ST.cloud_run(FakeGcp(), RUN, {}, "dbt")["cost_usd"] is None


def test_insights_name_memory_pressure_idle_cpus_a_costly_model_and_spill():
    units = [{"label": "train", "machine": {"cpu": 4, "memory_bytes": 8 * 2 ** 30},
              "usage": {"memory_peak": 0.95, "cpu_mean": 0.1}}]
    jobs = [ST._bq_row(_job("a", {}, billed=2 ** 40, spilled=5))]
    models = [{"name": "big", "cost_usd": jobs[0]["cost_usd"], "bytes_billed": 2 ** 40}]
    text = " ".join(i["text"] for i in ST.insights(units, jobs, models))
    assert "95% of its memory" in text and "10% busy" in text and "big is 100%" in text and "spilled" in text


def test_a_skewed_or_spilling_stage_stands_out():
    stage = {"name": "S01: Join", "spilled_bytes": 2 ** 30, "compute_avg": 100, "read_avg": 0,
             "compute_max": 9000, "read_max": 0}
    text = " ".join(i["text"] for i in ST.stage_insights([stage]))
    assert "spilled 1.00 GiB" in text and "skewed" in text
    assert ST.stage_insights([stage | {"spilled_bytes": 0, "compute_max": 150}]) == []


def test_a_served_models_executions_are_those_its_run_started():
    g = SimpleNamespace(run_executions=lambda job, n: [
        {"name": f"{job}/e1", "createTime": "2026-01-01T00:05:00Z"},
        {"name": f"{job}/e0", "createTime": "2025-12-31T00:00:00Z"}])
    spec = {"argument": {"models": [{"model": "roas", "job": "serve-roas"}, {"model": "v", "vertex": {}}]}}
    got = ST._served_executions(g, spec, {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"})
    assert [(m, x["name"]) for m, x in got] == [("roas", "serve-roas/e1")]

"""The Costs page's BigQuery breakdown: each job put on what ran it and the tables it wrote and
read, and each model's upstream data cost."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from gcpbricks.console import cost_attribution as CA

P = "proj"
TIB = 2 ** 40
FILES = "gs://b/.bundle/forecast/prod/files"


def row(day="2026-11-10", user="sa@x", exec_=None, inv=None, node=None, profile=None, target=None,
        dest=None, refs=(), jobs=1, billed=TIB, slot_ms=1000):
    vals = [day, user, exec_, inv, node, profile, target, dest, ",".join(refs), str(jobs), str(billed), str(slot_ms)]
    return {"f": [{"v": v} for v in vals]}


ROWS = [
    # the platform's dbt_task: builds stg from raw, then mart from stg
    row(inv="inv-1", node="model.forecast.stg", dest=f"{P}.ua.stg", refs=[f"{P}.raw.events"], billed=2 * TIB),
    # dbt's incremental temp table and a loader's staging copy are the tables they stand for, or none
    row(inv="inv-1", node="model.forecast.mart", dest=f"{P}.ua.mart__dbt_tmp0123", refs=[f"{P}.ua.stg", f"{P}.ua._load_ab12"],
        billed=TIB),
    # a platform Python task's own query, labelled with its Cloud Run execution
    row(exec_="forecast-retrain-main-ab12c", dest=None, refs=[f"{P}.ua.mart"], billed=TIB // 2),
    # dbt run from someone's laptop
    row(inv="inv-9", node="model.other.x", profile="other", target="dev", user="me@x", dest=f"{P}.dev.x",
        refs=[f"{P}.raw.events"], billed=TIB // 4),
    # an ad-hoc query
    row(user="analyst@x", refs=[f"{P}.ua.mart"], billed=TIB // 4),
]


class FakeGcp:
    project, region = P, "us-central1"

    def __init__(self, deny_project=False):
        self.deny_project = deny_project
        self.sql = []

    def bq_query(self, body):
        self.sql.append(body["query"])
        if self.deny_project and "JOBS_BY_PROJECT" in body["query"]:
            r = requests.Response()
            r.status_code = 403
            raise requests.HTTPError(response=r)
        return {"jobReference": {"jobId": "j"}, "rows": ROWS}

    def workflows(self):
        return [{"name": "w/forecast-retrain-prod", "labels": {"mlops_bundle": "forecast"},
                 "userEnvVars": {"MLOPS_JOB": "retrain", "MLOPS_BUCKET": "b", "MLOPS_DEFINITION": "jobs/retrain.json"}},
                # another job of the bundle on the same Cloud Run job, with another dbt command
                {"name": "w/forecast-report-prod", "labels": {"mlops_bundle": "forecast"},
                 "userEnvVars": {"MLOPS_JOB": "report", "MLOPS_BUCKET": "b", "MLOPS_DEFINITION": "jobs/report.json"}}]

    def read_json(self, bucket, name):
        if name == "jobs/retrain.json":
            return {"tasks": {
                "build": {"type": "cloud_run", "run_job": "forecast-retrain-main",
                          "args": ["mlops-task", "dbt", "--files", FILES, "--",
                                   "dbt build --select forecast {{job.parameters.dbt_args}}"]},
                "train": {"type": "cloud_run", "run_job": "forecast-retrain-main",
                          "args": ["mlops-task", "python", "--files", FILES, "--", "train.py"]}}}
        if name == "jobs/report.json":
            return {"tasks": {"report": {"type": "cloud_run", "run_job": "forecast-retrain-main",
                                         "args": ["mlops-task", "dbt", "--files", FILES, "--", "dbt run -s report"]}}}
        if name.endswith("dbt-0.json"):
            return {"invocation_id": "inv-1", "command": "dbt build --select forecast "}
        raise AssertionError(name)

    def list_objects(self, bucket, prefix, glob=None):
        assert prefix == ".bundle/forecast/prod/runs/"
        return [{"name": ".bundle/forecast/prod/runs/forecast-retrain-main-zz9yx/dbt-0.json", "timeCreated": "2026-11-10T01:00:00Z"},
                {"name": ".bundle/forecast/prod/runs/forecast-retrain-main-old00/dbt-0.json", "timeCreated": "2025-01-01T00:00:00Z"}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    CA._INVOCATIONS.clear()
    CA._CACHE.clear()
    monkeypatch.setattr(CA.C, "model_flows", lambda gcp: [
        {"model": "1", "name": "forecast_90d", "version": "3", "features": f"{P}.ua.mart", "labels": None},
        {"model": "2", "name": "view_fed", "version": "1", "features": f"{P}.fs.view", "labels": None}])


def hit(gcp=None):
    return CA.cached(gcp or FakeGcp(), 7, "us-central1", 6.25, now=datetime(2026, 11, 12, tzinfo=UTC))


def test_each_job_goes_to_the_platform_task_dbt_elsewhere_or_its_user():
    r = CA.report(FakeGcp(), hit())
    by = {tuple(o["path"]): o["usd"] for o in r["owners"]}
    assert by == {("Platform", "forecast", "retrain", "build", "model.forecast.stg"): 12.5,
                  ("Platform", "forecast", "retrain", "build", "model.forecast.mart"): 6.25,
                  # the execution's Cloud Run job runs both jobs: which one ran it is not guessed
                  ("Platform", "forecast", "(job unknown)", "(other queries)"): 3.125,
                  ("dbt outside the platform", "other.dev", "me@x", "model.other.x"): 1.5625,
                  ("Other queries", "analyst@x"): 1.5625}
    assert r["total_usd"] == 25.0 and r["scope"]["kind"] == "project" and r["invocations_found"] == 1
    assert next(o for o in r["owners"] if o["path"][-1] == "model.forecast.stg")["workflow"] == "forecast-retrain-prod"


def test_a_tables_build_cost_adds_up_and_its_read_cost_is_the_readers_whole_jobs():
    r = CA.report(FakeGcp(), hit())
    built = {b["table"]: b["usd"] for b in r["built"]}
    assert built == {f"{P}.ua.stg": 12.5, f"{P}.ua.mart": 6.25, f"{P}.dev.x": 1.5625}
    assert r["no_table_usd"] == round(3.125 + 1.5625, 2)
    read = {x["table"]: (x["jobs"], x["jobs_usd"]) for x in r["read"]}
    assert read[f"{P}.raw.events"] == (2, 12.5 + 1.5625) and read[f"{P}.ua.mart"] == (2, 3.125 + 1.5625)


def test_a_models_data_cost_is_its_tables_and_everything_upstream_each_once():
    r = CA.report(FakeGcp(), hit())
    m = {x["name"]: x for x in r["models"]}
    assert m["forecast_90d"]["usd"] == 18.75
    assert {t["table"]: t["depth"] for t in m["forecast_90d"]["tables"]} == {
        f"{P}.ua.mart": 0, f"{P}.ua.stg": 1, f"{P}.raw.events": 2}
    assert m["forecast_90d"]["unresolved"] == []
    # nothing built the view and no job reaches back from it: said so, not guessed
    assert m["view_fed"]["usd"] == 0 and m["view_fed"]["unresolved"] == [f"{P}.fs.view"]


def test_one_tables_page_has_its_builds_by_day_readers_and_upstream():
    t = CA.table(hit(), f"{P}.ua.mart")
    assert t["build"]["usd"] == 6.25 and t["build"]["by"][0]["name"] == "forecast / retrain / build / model.forecast.mart"
    assert {d["day"]: d["usd"] for d in t["build"]["by_day"]}["2026-11-10"] == 6.25 and len(t["build"]["by_day"]) == 7
    assert t["upstream_usd"] == 12.5 and t["built_from"] == [f"{P}.ua.stg"]
    assert CA.table(hit(), f"{P}.ua.stg")["feeds"] == [f"{P}.ua.mart"]


def test_without_listAll_only_the_consoles_own_jobs_and_it_says_so():
    g = FakeGcp(deny_project=True)
    r = CA.report(g, hit(g))
    assert r["scope"]["kind"] == "user" and "JOBS_BY_USER" in g.sql[-1]


def test_upstream_stops_on_a_cycle():
    assert CA.upstream({"a": {"b"}, "b": {"a"}}, ["a"]) == {"a": 0, "b": 1}


def test_a_declared_command_matches_what_ran_with_its_parameters_filled_in():
    assert CA._runs("dbt build -s tag:x {{job.parameters.dbt_args}}", "dbt build -s tag:x --full-refresh")
    assert CA._runs("dbt build -s tag:x {{job.parameters.dbt_args}}", "dbt build -s tag:x ")
    assert not CA._runs("dbt build -s tag:x {{job.parameters.dbt_args}}", "dbt build -s tag:y")


def test_temporary_tables_are_the_tables_they_stand_for_or_none():
    assert CA.canonical("p.d.t__dbt_tmp015746650111") == "p.d.t"
    assert CA.canonical("p.m.t_profile_metrics__staging_20261117134851") == "p.m.t_profile_metrics"
    assert CA.canonical("p.d._stg_t_cdf669a21dce") is None and CA.canonical("p.d.t") == "p.d.t"

"""The workspace from anywhere: links resolved to what they name, the CLI's Databricks-style
groups calling the console's backend, and the MCP server's workspace tools."""
from __future__ import annotations

import asyncio
import json

import pytest

from gcpbricks import cli
from gcpbricks import workspace as W
from gcpbricks.console import service as S

RUN = "c9590ada-6973-42ad-be21-fc06f3e2bcab"


@pytest.mark.parametrize("link, want", [
    (f"https://console.example/jobs/wf-dev/runs/{RUN}/tasks/train", {"kind": "task", "workflow": "wf-dev", "run": RUN, "task": "train"}),
    (f"https://console.example/jobs/wf-dev/runs/{RUN}", {"kind": "run", "workflow": "wf-dev", "run": RUN}),
    ("https://console.example/jobs/wf-dev", {"kind": "job", "workflow": "wf-dev"}),
    ("/catalog/ds/t?project=p", {"kind": "table", "dataset": "ds", "table": "t", "project": "p"}),
    ("/catalog/ds", {"kind": "dataset", "dataset": "ds"}),
    ("/models/123/versions/4", {"kind": "model_version", "model": "123", "version": "4"}),
    ("/experiments/e1/runs/r1", {"kind": "experiment", "experiment": "e1", "run": "r1"}),
    ("/quality/demo-model/dev/forecast?granularity=1%20day", {"kind": "monitor", "bundle": "demo-model", "target": "dev", "key": "forecast"}),
    (f"https://console.cloud.google.com/workflows/workflow/us-central1/wf-dev/execution/{RUN}?project=p",
     {"kind": "run", "workflow": "wf-dev", "run": RUN}),
    (f"projects/1/locations/us-central1/workflows/wf-dev/executions/{RUN}", {"kind": "run", "workflow": "wf-dev", "run": RUN}),
    ("https://console.cloud.google.com/workflows/workflow/us-central1/wf-dev?project=p", {"kind": "job", "workflow": "wf-dev"}),
    ("https://console.cloud.google.com/vertex-ai/locations/us-central1/pipelines/runs/retrain-x-dev-17?project=p",
     {"kind": "pipeline", "pipeline": "retrain-x-dev-17"}),
    ("p:us-central1.bquxjob_1a2b", {"kind": "bigquery_job", "project": "p", "location": "us-central1", "job": "bquxjob_1a2b"}),
    ("p.ds.t", {"kind": "table", "project": "p", "dataset": "ds", "table": "t"}),
])
def test_a_link_resolves_to_what_it_names(link, want):
    assert W.resolve(link) == want


def test_an_unknown_link_is_refused():
    with pytest.raises(ValueError, match="not a link"):
        W.resolve("https://example.com/whatever")


def test_a_run_is_its_link_or_a_job_and_an_id():
    assert W.run_of(f"/jobs/wf-dev/runs/{RUN}", None) == ("wf-dev", RUN)
    assert W.run_of("wf-dev", RUN) == ("wf-dev", RUN)
    with pytest.raises(ValueError, match="not a run"):
        W.run_of("/jobs/wf-dev", None)


@pytest.fixture
def backend(monkeypatch):
    calls = []
    monkeypatch.setattr(W, "client", lambda: "gcp")
    monkeypatch.setattr(W, "whoami", lambda: "me@x")
    monkeypatch.setattr(S, "cancel", lambda g, u, wf, rid: calls.append(("cancel", g, u, wf, rid)) or
                        {"cancelled": rid, "containers": []})
    monkeypatch.setattr(S, "run_now", lambda g, u, wf, params, only: calls.append(("run", wf, params, only)) or
                        {"execution": "e1"})
    monkeypatch.setattr(S, "set_paused", lambda g, u, wf, paused: calls.append(("pause", wf, paused)) or {"paused": paused})
    return calls


def test_jobs_cancel_run_takes_a_run_link(backend, capsys):
    assert cli.main(["jobs", "cancel-run", f"https://c/jobs/wf-dev/runs/{RUN}"]) == 0
    assert backend == [("cancel", "gcp", "me@x", "wf-dev", RUN)]
    assert json.loads(capsys.readouterr().out)["cancelled"] == RUN


def test_jobs_run_now_and_pause_name_the_workflow(backend):
    cli.main(["jobs", "run-now", "wf-dev", "--params", "a=1,b=2", "--only", "t1"])
    cli.main(["jobs", "pause", "https://c/jobs/wf-dev"])
    assert backend == [("run", "wf-dev", {"a": "1", "b": "2"}, ["t1"]), ("pause", "wf-dev", True)]


def test_a_backend_refusal_exits_with_its_message(monkeypatch):
    monkeypatch.setattr(W, "client", lambda: "gcp")

    def gone(*a):
        raise KeyError("wf-x is not a bundle job")
    monkeypatch.setattr(S, "job_detail", gone)
    with pytest.raises(SystemExit, match="wf-x is not a bundle job"):
        cli.main(["jobs", "get", "wf-x"])


def test_the_mcp_server_has_the_workspace_tools():
    from gcpbricks import notebooks_mcp as N

    names = {t.name for t in asyncio.run(N.server(lambda: None).list_tools())}
    assert {"inspect", "list_jobs", "job_runs", "why_failed", "task_logs", "run_job", "cancel_run",
            "repair_run", "read_notebook"} <= names

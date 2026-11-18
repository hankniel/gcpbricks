"""A dsl.ParallelFor in a Vertex pipeline run, read as Databricks reads a for_each_task: one node
with its iterations, a step of one iteration named `step[i]`, a failed iteration found.

The fixtures are real Vertex runs of a 3-item ParallelFor whose item 1 failed, and their repair
(a Vertex rerun) in which the cache kept items 0 and 2.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gcpbricks.console import failure as F
from gcpbricks.console import pipeline_view as PV
from gcpbricks.console import service as S

RUNS = Path(__file__).parent / "fixtures" / "pipeline_runs"


def _run(name: str) -> dict:
    return json.loads((RUNS / f"{name}.json").read_text())


def _nodes(job: dict) -> dict:
    return {n["key"]: n for n in S.pipeline_graph(job)["nodes"]}


def test_a_loop_is_one_for_each_node_with_its_iterations():
    nodes = _nodes(_run("parallel_for_failed"))
    loop = nodes["for-loop-2"]
    assert loop["kind"] == "for_each" and loop["state"] == "FAILED"
    assert [(i["index"], i["state"]) for i in loop["iterations"]] == [(0, "SUCCESS"), (1, "FAILED"), (2, "SUCCESS")]
    # The step inside: failed as any iteration did, with each iteration's state.
    work = nodes["work"]
    assert work["parent"] == "for-loop-2" and work["state"] == "FAILED"
    assert [i["state"] for i in work["iterations"]] == ["SUCCESS", "FAILED", "SUCCESS"]
    assert nodes["after"]["state"] == "SKIPPED" and "iterations" not in nodes["after"]


def test_a_repair_shows_the_iterations_the_cache_kept_as_succeeded():
    nodes = _nodes(_run("parallel_for_repaired"))
    assert nodes["for-loop-2"]["state"] == "SUCCESS" and nodes["work"]["state"] == "SUCCESS"
    job = _run("parallel_for_repaired")
    cached = [i for i in (0, 1, 2) if (PV._detail(job, f"work[{i}]").get("execution") or {}).get("state") == "CACHED"]
    assert cached == [0, 2]


def test_the_flow_draws_the_loop_as_a_task_not_a_decision():
    flow = S.pipeline_flow(S.pipeline_graph(_run("parallel_for_failed")))
    kinds = {n["key"]: n["kind"] for n in flow["nodes"]}
    assert kinds == {"for-loop-2": "for_each", "after": "step"}
    assert {(e["from"], e["to"]) for e in flow["edges"]} == {("for-loop-2", "after")}


def test_a_step_of_one_iteration_is_named_step_i():
    job = _run("parallel_for_failed")
    details = job["jobDetail"]["taskDetails"]
    assert sorted(S.step_ref(d) for d in details if d["taskName"] == "work") == ["work[0]", "work[1]", "work[2]"]
    assert PV._detail(job, "work[1]")["state"] == "FAILED"
    assert PV._detail(job, "for-loop-2")["state"] == "FAILED"
    with pytest.raises(KeyError, match=r"work\[0\] .. work\[2\]"):
        PV._detail(job, "work")
    with pytest.raises(KeyError, match="no iteration 7"):
        PV._detail(job, "work[7]")
    assert PV.base_key("work[1]") == "work" and PV.base_key("gate-op") == "gate-op"


def test_why_follows_a_failed_iteration_to_its_custom_job():
    job = _run("parallel_for_failed")
    failed = next(d for d in job["jobDetail"]["taskDetails"] if S.step_ref(d) == "work[1]")
    custom = failed["executorDetail"]["containerDetail"]["mainJob"]

    class Gcp:
        region, project = "us-central1", "p"

        def ai_get(self, name):
            return job if name == job["name"] else {"state": "JOB_STATE_FAILED", "createTime": "2026-11-18T07:10:00Z", "endTime": "2026-11-18T07:12:00Z"}

        def log_entries(self, flt, n=500, newest_first=False):
            assert custom.rsplit("/", 1)[-1] in flt
            return [{"timestamp": "2026-11-18T07:11:59Z", "textPayload": "RuntimeError: item 1 fails while the flag exists"}]

    out = {"path": [], "error": "", "logs": []}
    F._pipeline(Gcp(), job["name"], out)
    assert [(p["kind"], p["label"]) for p in out["path"]][1:] == [("step", "work[1]"), ("custom_job", custom.rsplit("/", 1)[-1])]
    assert out["logs"][-1]["text"].startswith("RuntimeError: item 1")

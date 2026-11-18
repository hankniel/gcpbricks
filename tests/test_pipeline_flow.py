"""A Vertex pipeline's If / Elif / Else groups drawn as Databricks draws condition tasks: a
decision per test with True / False edges (console.service.pipeline_flow), on the structure of
demo_model' ua_forecast_mh retrain pipeline."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from gcpbricks.console import service as S

GRAPH = json.loads((Path(__file__).parent / "fixtures" / "pipeline_flow_ua_forecast_mh.json").read_text())


def _flow(states: dict[str, str] | None = None, run: bool = True) -> dict:
    g = copy.deepcopy(GRAPH)
    for n in g["nodes"]:
        n["state"] = (states or {}).get(n["key"], "SKIPPED" if states else None)
    return S.pipeline_flow(g, run=run)


def _edges(f: dict) -> set[tuple[str, str, str]]:
    return {(e["from"], e["outcome"], e["to"]) for e in f["edges"]}


def test_groups_become_decisions_with_their_own_tests():
    f = _flow(run=False)
    decisions = {n["key"]: (n["label"], n["condition"]) for n in f["nodes"] if n["kind"] == "decision"}
    assert decisions == {
        "condition-1": ("If", "retrain_needed == true"), "condition-8": ("If", "train_needed == true"),
        "condition-4": ("If", "train_mode == 'parts_intervals'"), "condition-5": ("Else if", "train_mode == 'parts'"),
        "condition-6": ("Else if", "train_mode == 'one_intervals'"), "condition-10": ("If", "promote == true")}
    assert not any(n["kind"] == "condition" for n in f["nodes"])
    assert all(n.get("taken") is None for n in f["nodes"] if n["kind"] == "decision")


def test_an_elif_chain_runs_through_false_edges_to_the_else():
    e = _edges(_flow(run=False))
    assert ("prepare-op", "", "condition-1") in e
    assert ("condition-1", "true", "condition-8") in e
    assert {("condition-4", "false", "condition-5"), ("condition-5", "false", "condition-6")} <= e
    assert ("condition-6", "false", "custom-training-job-12") in e  # the Else's step
    assert ("condition-8", "false", "importer-6") in e  # !(train_needed) is the Else
    # every branch joins at register, and both of train_needed's ways at gate
    assert {("importer", "", "model-upload"), ("importer-4", "", "model-upload"), ("importer-5", "", "model-upload")} <= e
    assert {("model-upload", "", "gate-op"), ("importer-6", "", "gate-op"), ("gate-op", "", "condition-10")} <= e


def test_a_run_shows_the_way_each_decision_took():
    f = _flow({"prepare-op": "SUCCESS", "condition-1": "SUCCESS", "condition-branches-2": "SUCCESS",
               "condition-8": "SUCCESS", "condition-branches-3": "SUCCESS", "condition-5": "SUCCESS",
               "custom-training-job-6": "SUCCESS"})
    taken = {n["key"]: n["taken"] for n in f["nodes"] if n["kind"] == "decision"}
    assert taken["condition-1"] == "true" and taken["condition-8"] == "true"
    assert taken["condition-4"] == "false" and taken["condition-5"] == "true"
    assert taken["condition-6"] is None  # never reached: parts held first


def test_a_test_after_a_cancelled_step_was_not_evaluated():
    f = _flow({"prepare-op": "CANCELLED"})
    assert next(n for n in f["nodes"] if n["key"] == "condition-1")["taken"] is None

"""A batch_serve_task's declaration and workflows/batch_serve.yaml: the env the workflow sets
on a model's job is what gcpbricks.batch_serve.window() reads, and the workflow does
nothing but run each model over its window."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import batch_serve as C
from gcpbricks import bundle as B

MODULE = Path(__file__).resolve().parent.parent / "terraform" / "modules" / "bundle"
WORKFLOW_TEXT = (MODULE / "workflows" / "batch_serve.yaml").read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)

BASE = {"bundle": {"name": "demo", "mlops_version": "0.4.0"},
        "gcp": {"project": "p", "bucket": "b"},
        "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
        "targets": {"dev": {"default": True}, "prod": {}}}
SERVE = {"window_days": 14, "models": [{"model": "forecast_30d", "job": "forecast-30d-predict"},
                                       {"model": "forecast_90d", "job": "forecast-90d-predict",
                                        "window_days": 45}]}
VERTEX = {"registered_model": "forecast", "date_col": "snapshot_date", "input_table": "p.fs.features", "output_table": "p.preds.forecast",
          "prediction_col": "pred", "entity_id_columns": ["snapshot_date", "campaign"]}


def _steps(sub: str) -> dict:
    return {name: body for step in WORKFLOW[sub]["steps"] for name, body in step.items()}


def _score_env() -> list[dict]:
    return _steps("serve_model")["score"]["args"]["body"]["overrides"]["containerOverrides"][0]["env"]


def _loaded(target: str = "prod", **serve) -> B.Loaded:
    raw = {**BASE, "resources": {"jobs": {"daily": {"tasks": [
        {"task_key": "serve", "batch_serve_task": {**SERVE, **serve}}]}}}}
    return B.Loaded(bundle=B.Bundle.model_validate(raw), target=target, root=Path("."))


def _argument(target: str, **serve) -> dict:
    return B.job_definition(_loaded(target, **serve), "daily")["tasks"]["serve"]["argument"]


# --- the task's declaration ---------------------------------------------------------------

def test_args_name_the_targets_own_jobs():
    prod, dev = _argument("prod"), _argument("dev")
    assert [m["job"] for m in prod["models"]] == ["forecast-30d-predict", "forecast-90d-predict"]
    assert [m["job"] for m in dev["models"]] == ["forecast-30d-predict-dev", "forecast-90d-predict-dev"]
    assert [(m["from_days"], m["to_days"]) for m in prod["models"]] == [(-14, 0), (-45, 0)]
    assert prod["job"] == "daily-serve"


def test_the_argument_is_only_what_running_the_models_needs():
    assert set(_argument("prod")) == {"job", "target", "region", "max_hours", "models", "window_days",
                                      "utc_offset_hours", "service_account", "bucket", "progress_prefix"}
    assert _argument("dev")["progress_prefix"] == "dev/.bundle/demo/serve_progress/daily-serve"


def test_a_vertex_model_names_its_input_and_output_tables():
    arg = _argument("prod", models=[{"model": "forecast", "vertex_batch_prediction": VERTEX}])
    v = arg["models"][0]["vertex"]
    assert (v["input_table"], v["output_table"], v["version"]) == ("p.fs.features", "p.preds.forecast", "@champion")


def test_a_vertex_model_reads_and_writes_its_declared_date_col():
    arg = _argument("prod", models=[{"model": "forecast", "vertex_batch_prediction": {**VERTEX, "date_col": "event_date"}}])
    assert arg["models"][0]["vertex"]["date_col"] == "event_date"
    workflow = (Path(B.__file__).parents[2] / "terraform/modules/bundle/workflows/batch_serve.yaml").read_text()
    assert "snapshot_date" not in workflow.split("steps:", 1)[1]
    assert workflow.count("v.date_col") == 2


def test_a_vertex_model_must_declare_its_date_col():
    with pytest.raises(Exception, match="date_col"):
        _argument("prod", models=[{"model": "forecast", "vertex_batch_prediction":
                                   {k: v for k, v in VERTEX.items() if k != "date_col"}}])


def test_the_runner_workflow_is_named_per_target():
    assert B.render(_loaded("prod"))["task_workflows"] == {"batch_serve": "demo-batch-serve"}
    assert B.render(_loaded("dev"))["task_workflows"] == {"batch_serve": "demo-batch-serve-dev"}


@pytest.mark.parametrize("bad", [
    {"models": []},
    {"window_days": 0},
    {"models": [{"model": "m", "job": "j", "window_days": 0}]},
    {"parameters": {"from": "2026-01-01"}},
    {"parameters": {"rerun_failed": "true"}},
    {"models": [{"model": "m", "vertex_batch_prediction": {**VERTEX, "output_table": "preds.forecast"}}]},
])
def test_a_malformed_serve_task_is_rejected(bad):
    with pytest.raises(ValidationError):
        _loaded(**bad)


@pytest.mark.parametrize("removed", ["feature_table", "runs_table", "max_window_days", "min_keep_ratio"])
def test_a_removed_setting_says_what_replaces_it(removed):
    with pytest.raises(ValidationError, match="no longer takes .*write_window"):
        _loaded(**{removed: "x"})


# --- the workflow -------------------------------------------------------------------------

def test_the_score_step_sets_exactly_what_window_reads():
    assert {e["name"] for e in _score_env()} == {*C.ENV, C.PROGRESS_ENV, C.RUN_DATE_ENV, C.OUTPUT_ENV,
                                                C.BACKFILL_ENV}


def test_a_models_run_id_names_its_progress_so_a_fresh_rerun_skips_nothing():
    values = {e["name"]: e["value"] for e in _score_env()}
    assert values["SERVE_RUN_ID"] == "${m.run_id}" and "m.run_id" in values["SERVE_PROGRESS"]
    assert values["SERVE_STARTED_MS"] == "${string(started_ms)}"


def test_a_repair_keeps_the_runs_id_start_and_windows_and_runs_only_what_it_names():
    rp = {name: body for step in _steps("main")["repairing"]["switch"][0]["steps"] for name, body in step.items()}
    assert rp["repaired_run"]["assign"][1:3] == [{"run_id": "${rp.run_id}"}, {"started_ms": "${rp.started_ms}"}]
    window = rp["repaired_windows"]["for"]["steps"][0]["known"]["switch"][0]["steps"][0]["with_window"]["assign"]
    assert {"item.from_date": "${was.from_date}"} in window and {"item.to_date": "${was.to_date}"} in window
    assert _steps("main")["repair_windows_set"]["switch"][0]["next"] == "run"
    step = WORKFLOW["main"]["steps"][5]["run"]["parallel"]["for"]["steps"][0]["pending"]
    assert step["switch"][0]["condition"] == '${m.status == "PENDING"}'


def test_the_run_says_each_models_status_on_success_and_failure():
    raised = _steps("main")["any_failed"]["switch"][0]["steps"][0]["fail"]["raise"]
    assert {"run_id", "started_ms", "models"} <= set(raised) and "message" in raised
    assert set(_steps("main")["done"]["return"]) == {"run_id", "started_ms", "models"}


def test_each_model_runs_over_its_own_window():
    values = {e["name"]: e["value"] for e in _score_env()}
    assert values["FROM_DATE"] == "${m.from_date}" and values["TO_DATE"] == "${m.to_date}"
    assert values["SERVE_MODEL"] == "${m.model}"
    assert 'default(map.get(m, "window_days"), default(args.window_days, 0))' in WORKFLOW_TEXT


def test_the_workflow_writes_nothing_but_a_vertex_models_output():
    # No claims, no runs table, no publish: a Cloud Run model's job owns its writes.
    assert "storage.v1.objects" not in WORKFLOW_TEXT and "runs_table" not in WORKFLOW_TEXT
    writes = re.findall(r"CREATE OR REPLACE TABLE `\" \+ ([\w.]+)", WORKFLOW_TEXT)
    assert writes == ["ds", "v.output_table"]  # the input snapshot (expiring), then the output


def test_a_failed_model_doesnt_stop_the_others_but_fails_the_run():
    pending = WORKFLOW["main"]["steps"][5]["run"]["parallel"]["for"]["steps"][0]["pending"]
    one = pending["switch"][0]["steps"][0]["one_model"]
    assert "except" in one
    assert _steps("main")["any_failed"]["switch"][0]["condition"] == "${len(failed) > 0}"


def test_no_expression_is_longer_than_workflows_allows():
    # Cloud Workflows refuses to deploy an expression over 400 characters.
    expressions = re.findall(r"\$\{(.*?)\}'?$", WORKFLOW_TEXT, re.MULTILINE)
    long = [e for e in expressions if len(e) + 3 > 400]
    assert not long, [e[:80] for e in long]


def test_a_models_window_is_days_from_the_runs_date():
    arg = _argument("prod", window_days=None, models=[
        {"model": "a", "job": "a-predict", "window": {"from_days": -17, "to_days": -3}},
        {"model": "b", "job": "b-predict", "window": {"from_days": -2}}])
    assert [(m["from_days"], m["to_days"]) for m in arg["models"]] == [(-17, -3), (-2, 0)]
    assert "local_now + from_days * 86400" in WORKFLOW_TEXT and "local_now + to_days * 86400" in WORKFLOW_TEXT


def test_a_models_output_reaches_its_job_as_json():
    out = {"mode": "insert_new", "keys": ["snapshot_date", "user"], "stamps": {"predict_date": "run_date"}}
    m = _argument("prod", models=[{"model": "a", "job": "a-predict", "output": out}])["models"][0]
    assert B.ServeOutput.model_validate_json(m["output"]) == B.ServeOutput(**out)
    values = {e["name"]: e["value"] for e in _score_env()}
    assert values[C.OUTPUT_ENV] == '${default(map.get(m, "output"), "")}'


def test_a_run_given_its_dates_tells_its_jobs_it_is_a_backfill():
    values = {e["name"]: e["value"] for e in _score_env()}
    assert values[C.BACKFILL_ENV] == '${if(default(map.get(m, "backfill"), false), "true", "false")}'
    assert {"item.backfill": "${backfill}"} in _steps("main")["assign_windows"]["for"]["steps"][2]["add_window"]["assign"]
    rp = {name: body for step in _steps("main")["repairing"]["switch"][0]["steps"] for name, body in step.items()}
    window = rp["repaired_windows"]["for"]["steps"][0]["known"]["switch"][0]["steps"][0]["with_window"]["assign"]
    assert {"item.backfill": '${default(map.get(was, "backfill"), false)}'} in window


def test_the_run_date_survives_a_repair():
    assert {"item.run_date": "${today}"} in _steps("main")["assign_windows"]["for"]["steps"][2]["add_window"]["assign"]
    rp = {name: body for step in _steps("main")["repairing"]["switch"][0]["steps"] for name, body in step.items()}
    window = rp["repaired_windows"]["for"]["steps"][0]["known"]["switch"][0]["steps"][0]["with_window"]["assign"]
    assert {"item.run_date": '${default(map.get(was, "run_date"), today)}'} in window


@pytest.mark.parametrize("bad", [
    {"models": [{"model": "m", "job": "j", "window": {"from_days": 1, "to_days": 0}}]},
    {"models": [{"model": "m", "job": "j", "window_days": 3, "window": {"from_days": -3}}]},
    {"models": [{"model": "m", "job": "j", "output": {"mode": "insert_new"}}]},
    {"models": [{"model": "m", "job": "j", "output": {"mode": "upsert", "keys": ["k"]}}]},
    {"models": [{"model": "m", "job": "j", "output": {"stamps": {"d": "yesterday"}}}]},
    {"models": [{"model": "m", "vertex_batch_prediction": VERTEX, "output": {"mode": "append"}}]},
    {"models": [{"model": "m", "job": "j", "output": {"mode": "insert_new", "keys": ["k"], "watermark": []}}]},
    {"models": [{"model": "m", "job": "j", "output": {"mode": "append", "watermark": ["g"]}}]},
    {"window_days": None},
])
def test_a_malformed_window_or_output_is_rejected(bad):
    with pytest.raises(ValidationError):
        _loaded(**bad)

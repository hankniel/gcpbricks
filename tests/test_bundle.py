"""mlops.yaml as a Databricks Asset Bundle: loading, targets, variables, validation and the
plan rendered from it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import bundle as B

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "bundle"

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.4.0"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
CLUSTER = {"job_cluster_key": "main", "new_cluster": {"docker_image": {"url": "img"}}}
WHEEL = {"task_key": "score", "job_cluster_key": "main",
         "python_wheel_task": {"package_name": "pkg", "entry_point": "run"}}
DBT = {"task_key": "build", "dbt_task": {"schema": "ds", "commands": ["dbt build"]}}


def _write(tmp_path: Path, jobs: dict, **extra) -> Path:
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": {"jobs": jobs}, **extra}))
    return path


def _load(tmp_path, jobs, target=None, **extra) -> B.Loaded:
    return B.load(_write(tmp_path, jobs, **extra), target)


def _job(*tasks, **extra) -> dict:
    return {"job_clusters": [CLUSTER], "tasks": list(tasks), **extra}


# --- the fixture --------------------------------------------------------------------------

def test_the_fixture_loads_with_its_included_job():
    lb = B.load(FIXTURE / "mlops.yaml")
    assert lb.target == "dev" and list(lb.bundle.resources.jobs) == ["nightly"]
    assert lb.bundle.resources.jobs["nightly"].tasks[0].dbt_task.schema_ == "demo_app_dev"


def test_the_fixtures_committed_render_is_current():
    """tests/fixtures/bundle/.mlops/dev.json is what terraform-validate reads."""
    committed = json.loads((FIXTURE / ".mlops" / "dev.json").read_text())
    assert committed == B.render(B.load(FIXTURE / "mlops.yaml", "dev"))


# --- targets and variables ----------------------------------------------------------------

def test_a_target_overrides_a_task_by_its_key(tmp_path):
    lb = _load(tmp_path, {"j": _job(WHEEL, DBT)}, "prod", targets={
        **BASE["targets"],
        "prod": {"mode": "production",
                 "resources": {"jobs": {"j": {"tasks": [{"task_key": "score", "max_retries": 3}]}}}}})
    tasks = {t.task_key: t for t in lb.bundle.resources.jobs["j"].tasks}
    assert tasks["score"].max_retries == 3 and tasks["score"].python_wheel_task.package_name == "pkg"
    assert set(tasks) == {"score", "build"}


def test_variables_resolve_lowest_first_default_target_env_cli(tmp_path, monkeypatch):
    dbt = {**DBT, "dbt_task": {"schema": "${var.schema}", "commands": ["dbt build"]}}
    targets = {"dev": {"default": True, "variables": {"schema": "from_target"}}}
    path = _write(tmp_path, {"j": _job(dbt)}, targets=targets,
                  variables={"schema": {"default": "from_default"}})
    def schema(lb):
        return lb.bundle.resources.jobs["j"].tasks[0].dbt_task.schema_
    assert schema(B.load(path)) == "from_target"
    monkeypatch.setenv("BUNDLE_VAR_schema", "from_env")
    assert schema(B.load(path)) == "from_env"
    assert schema(B.load(path, variables={"schema": "from_cli"})) == "from_cli"


def test_bundle_target_and_job_ids_substitute(tmp_path):
    caller = {"task_key": "call", "run_job_task": {"job_id": "${resources.jobs.child.id}"}}
    dbt = {**DBT, "dbt_task": {"schema": "ds_${bundle.target}", "commands": ["dbt build"]}}
    lb = _load(tmp_path, {"parent": _job(caller), "child": _job(dbt)}, "prod")
    jobs = lb.bundle.resources.jobs
    assert jobs["parent"].tasks[0].run_job_task.job_id == "demo-child"
    assert jobs["child"].tasks[0].dbt_task.schema_ == "ds_prod"


@pytest.mark.parametrize("ref", ["${var.nope}", "${workspace.file_path}"])
def test_an_unknown_reference_is_refused(tmp_path, ref):
    with pytest.raises(ValueError, match="unknown reference"):
        _load(tmp_path, {"j": _job({**DBT, "dbt_task": {"schema": ref, "commands": ["dbt build"]}})})


def test_without_a_default_target_one_must_be_named(tmp_path):
    path = _write(tmp_path, {"j": _job(DBT)}, targets={"a": {}, "b": {}})
    with pytest.raises(ValueError, match="no -t given"):
        B.load(path)
    assert B.load(path, "b").target == "b"


# --- what a job may declare ---------------------------------------------------------------

@pytest.mark.parametrize("job", [
    _job({**WHEEL, "existing_cluster_id": "x"}),                       # Databricks-only key
    _job({**WHEEL, "job_cluster_key": None}),                          # a wheel needs compute
    _job({**WHEEL, "job_cluster_key": "other"}),                       # unknown cluster
    _job({**WHEEL, "dbt_task": DBT["dbt_task"]}),                      # two task types
    _job(WHEEL, {**DBT, "task_key": "score"}),                         # repeated task_key
    _job({**DBT, "depends_on": [{"task_key": "nope"}]}),               # unknown dependency
    _job({**DBT, "depends_on": [{"task_key": "x"}]},                   # a cycle
         {**DBT, "task_key": "x", "depends_on": [{"task_key": "build"}]}),
    _job(WHEEL, {**DBT, "depends_on": [{"task_key": "score", "outcome": "true"}]}),  # not a condition
    _job({**DBT, "run_if": "ALL_DONE"}),                               # run_if without deps
    _job({**DBT, "dbt_task": {"schema": "s", "commands": ["rm -rf /"]}}),
    _job({**DBT, "dbt_task": {"commands": ["dbt build"]}}),            # no schema, no profiles
    _job({**DBT, "dbt_task": {**DBT["dbt_task"], "state_location": "b/state"}}),  # not gs://
    _job({**WHEEL, "python_wheel_task": {"package_name": "p", "entry_point": "e",
                                         "parameters": ["{{tasks.x.values.y}}"]}}),
    _job({**WHEEL, "python_wheel_task": {"package_name": "p", "entry_point": "e",
                                         "parameters": ["{{job.parameters.undeclared}}"]}}),
    _job({**WHEEL, "python_wheel_task": {"package_name": "p", "entry_point": "e",
                                         "parameters": ["{{input}}"]}}),  # input outside for_each
    {"tasks": [DBT], "job_clusters": [{**CLUSTER, "new_cluster": {
        "docker_image": {"url": "i"}, "node_type_id": "n2-highmem-8"}}]},  # over Cloud Run's memory
    {"tasks": [DBT], "job_clusters": [{**CLUSTER, "new_cluster": {
        "docker_image": {"url": "i"}, "spark_version": "15.4.x-scala2.12"}}]},
    _job({"task_key": "fan", "for_each_task": {"inputs": "[1]", "concurrency": 50, "task": WHEEL}}),
    _job({**DBT, "batch_serve_task": None, "dbt_task": None}),        # no task type
])
def test_a_job_the_platform_cannot_run_is_refused(tmp_path, job):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, {"j": job})


def test_run_job_cycles_are_refused(tmp_path):
    a = {"task_key": "call", "run_job_task": {"job_id": "b"}}
    b = {"task_key": "call", "run_job_task": {"job_id": "a"}}
    with pytest.raises(ValueError, match="cycle"):
        _load(tmp_path, {"a": _job(a), "b": _job(b)})


def test_levels_put_every_dependency_first():
    job = B.Job.model_validate({"tasks": [
        {**DBT, "task_key": "d", "depends_on": [{"task_key": "b"}, {"task_key": "c"}]},
        {**DBT, "task_key": "a"},
        {**DBT, "task_key": "b", "depends_on": [{"task_key": "a"}]},
        {**DBT, "task_key": "c"},
    ]})
    assert B.levels(job) == [["a", "c"], ["b"], ["d"]]


# --- schedules and compute ----------------------------------------------------------------

@pytest.mark.parametrize("quartz, cron", [
    ("0 0 9 * * ?", "0 9 * * *"),
    ("0 30 6,12 ? * MON-FRI", "30 6,12 * * MON-FRI"),
    ("0 0 12 ? * 2", "0 12 * * 1"),            # Quartz Monday is 2
    ("0 0 12 ? * 2-6", "0 12 * * 1-5"),
    ("0 15 */2 * * ? *", "15 */2 * * *"),
])
def test_quartz_becomes_cloud_scheduler_cron(quartz, cron):
    assert B.unix_cron(quartz) == cron


@pytest.mark.parametrize("quartz", ["30 0 9 * * ?", "0 0 9 L * ?", "0 0 9 * * ? 2027", "0 9 * * *"])
def test_quartz_cloud_scheduler_cannot_run_is_refused(quartz):
    with pytest.raises(ValueError):
        B.unix_cron(quartz)


def test_node_types_size_the_cloud_run_job():
    assert B.cloud_run_resources("e2-standard-4") == {"cpu": "4", "memory": "16Gi"}
    assert B.cloud_run_resources("n2-highcpu-8") == {"cpu": "8", "memory": "8Gi"}


def test_development_pauses_schedules_unless_unpaused(tmp_path):
    sched = {"quartz_cron_expression": "0 0 9 * * ?", "timezone_id": "UTC"}
    jobs = {"a": _job(DBT, schedule=sched),
            "b": _job(DBT, schedule={**sched, "pause_status": "UNPAUSED"})}
    dev, prod = B.render(_load(tmp_path, jobs, "dev")), B.render(_load(tmp_path, jobs, "prod"))
    assert (dev["jobs"]["a"]["paused"], dev["jobs"]["b"]["paused"]) == (True, False)
    assert prod["jobs"]["a"]["paused"] is False


# --- the rendered plan --------------------------------------------------------------------

def test_names_are_namespaced_by_target(tmp_path):
    jobs = {"nightly": _job(WHEEL, DBT)}
    prod, dev = B.render(_load(tmp_path, jobs, "prod")), B.render(_load(tmp_path, jobs, "dev"))
    assert prod["jobs"]["nightly"]["workflow"] == "demo-nightly"
    assert dev["jobs"]["nightly"]["workflow"] == "demo-nightly-dev"
    assert set(dev["run_jobs"]) == {"demo-nightly-main-dev", "demo-dbt-dev"}
    assert dev["run_jobs"]["demo-dbt-dev"]["image"] is None


def test_a_wheel_task_runs_its_entry_point_with_its_parameters(tmp_path):
    named = {**WHEEL, "python_wheel_task": {"package_name": "pkg", "entry_point": "run",
                                            "named_parameters": {"date": "{{job.start_time.iso_date}}"}}}
    task = B.job_definition(_load(tmp_path, {"j": _job(named)}, "prod"), "j")["tasks"]["score"]
    assert task["run_job"] == "demo-j-main"
    assert task["args"][:2] == ["python", "-c"] and task["args"][3:] == [
        "pkg", "run", "--date={{job.start_time.iso_date}}"]


def test_a_dbt_task_reads_the_targets_files_and_writes_its_schema(tmp_path):
    task = B.job_definition(_load(tmp_path, {"j": _job(DBT)}, "dev"), "j")["tasks"]["build"]
    assert task["run_job"] == "demo-dbt-dev"
    assert task["args"] == ["mlops-task", "dbt", "--files", "gs://b/dev/.bundle/demo/files",
                            "--project-directory", ".", "--catalog", "p", "--schema", "ds",
                            "--location", "us-central1", "--", "dbt build"]


def test_a_dbt_tasks_state_location_reaches_the_runner_unresolved(tmp_path):
    kept = {**DBT, "dbt_task": {**DBT["dbt_task"], "state_location": "gs://b/state/{{job.run_id}}"}}
    task = B.job_definition(_load(tmp_path, {"j": _job(kept)}, "dev"), "j")["tasks"]["build"]
    assert task["args"][-4:] == ["--state-location", "gs://b/state/{{job.run_id}}", "--", "dbt build"]


def test_task_runner_workflows_get_their_argument_and_options(tmp_path):
    vertex = {"task_key": "train", "vertex_pipeline_task": {
        "spec_prefix": "m/spec", "build": "true", "parameters": {"force_retrain": "{{job.parameters.force}}"}}}
    serve = {"task_key": "serve", "depends_on": [{"task_key": "train"}], "batch_serve_task": {
        "models": [{"model": "m", "job": "m-predict"}], "window_days": 14,
        "parameters": {"only_models": "{{job.parameters.force}}"}}}
    lb = _load(tmp_path, {"j": _job(vertex, serve, parameters=[{"name": "force", "default": ""}])}, "dev")
    tasks = B.job_definition(lb, "j")["tasks"]
    assert tasks["train"]["workflow"] == "demo-vertex-pipeline-dev"
    assert tasks["train"]["argument"]["spec_prefix"] == "dev/m/spec"
    assert (tasks["train"]["options_into"], tasks["serve"]["options_into"]) == ("overrides", "")
    assert tasks["serve"]["argument"]["models"] == [{"model": "m", "job": "m-predict-dev",
                                                     "from_days": -14, "to_days": 0}]
    plan = B.render(lb)
    assert plan["task_workflows"] == {"batch_serve": "demo-batch-serve-dev",
                                      "vertex_pipeline": "demo-vertex-pipeline-dev"}
    assert "serve_sql" not in plan


def test_a_run_job_task_carries_the_called_jobs_argument(tmp_path):
    caller = {"task_key": "call", "run_job_task": {"job_id": "child", "job_parameters": {"x": "1"}}}
    lb = _load(tmp_path, {"parent": _job(caller), "child": _job(DBT)}, "prod")
    task = B.job_definition(lb, "parent")["tasks"]["call"]
    assert task["workflow"] == "demo-child" and task["options"] == {"x": "1"}
    assert task["argument"] == {} and task["options_into"] == "params"


def test_render_sends_every_argument_as_json_text(tmp_path):
    plan = B.render(_load(tmp_path, {"a": _job(WHEEL), "b": _job(DBT)}, "dev"))
    assert all(isinstance(j["argument"], str) for j in plan["jobs"].values())


# --- triggers, queue, health, notifications, tags -----------------------------------------

@pytest.mark.parametrize("interval, unit, cron", [
    (15, "MINUTES", "*/15 * * * *"), (60, "MINUTES", "0 * * * *"), (6, "HOURS", "0 */6 * * *"),
    (1, "DAYS", "0 0 * * *"), (1, "WEEKS", "0 0 * * 1"),
])
def test_a_periodic_trigger_becomes_the_jobs_schedule(tmp_path, interval, unit, cron):
    job = _job(DBT, trigger={"periodic": {"interval": interval, "unit": unit}})
    plan = B.render(_load(tmp_path, {"j": job}, "prod"))
    assert plan["jobs"]["j"]["schedule"] == cron and plan["triggers"] == {}


@pytest.mark.parametrize("interval, unit", [(7, "MINUTES"), (5, "HOURS"), (2, "DAYS"), (2, "WEEKS")])
def test_a_periodic_trigger_cron_cannot_hit_is_refused(tmp_path, interval, unit):
    with pytest.raises(ValueError, match="cron can run"):
        _load(tmp_path, {"j": _job(DBT, trigger={"periodic": {"interval": interval, "unit": unit}})})


def test_a_table_update_trigger_is_polled_by_the_trigger_workflow(tmp_path):
    job = _job(DBT, trigger={"table_update": {"table_names": ["p.fs.features"],
                                              "wait_after_last_change_seconds": 600}})
    plan = B.render(_load(tmp_path, {"j": job}, "dev"))
    assert plan["task_workflows"]["trigger"] == "demo-trigger-dev"
    t = plan["triggers"]["j"]
    assert t["schedule"] == B.TRIGGER_POLL_CRON and t["paused"] is True  # development
    arg = json.loads(t["argument"])
    assert arg["tables"] == [{"name": "p.fs.features", "kind": "bigquery", "project": "p", "dataset": "fs",
                              "table": "features"}]
    assert arg["job_argument"] == B.job_argument(_load(tmp_path, {"j": job}, "dev"), "j")
    assert arg["state_object"] == "dev/.bundle/demo/triggers/j.json"
    assert plan["jobs"]["j"]["schedule"] is None


def test_a_lakehouse_iceberg_table_is_watched_through_its_catalog(tmp_path):
    job = _job(DBT, trigger={"table_update": {"table_names": ["dp.lake.standardized.afly__cost", "p.fs.features"],
                                              "condition": "ALL_UPDATED"}})
    arg = json.loads(B.render(_load(tmp_path, {"j": job}, "dev"))["triggers"]["j"]["argument"])
    assert arg["tables"][0] == {"name": "dp.lake.standardized.afly__cost", "kind": "iceberg", "project": "dp",
                                "catalog": "lake", "namespace": "standardized", "table": "afly__cost"}
    assert arg["tables"][1]["kind"] == "bigquery"


def test_a_triggered_job_may_use_what_fired_it_as_databricks_names_it(tmp_path):
    one = "{{job.trigger.table_update.commit_timestamp.timestamp_ms}}"
    job = _job({**DBT, "dbt_task": {**DBT["dbt_task"], "commands": [f"dbt build --vars 'as_of: {one}'"]}},
               trigger={"table_update": {"table_names": ["p.fs.features"]}},
               parameters=[{"name": "tables", "default": "{{job.trigger.table_update.updated_tables}}"}])
    lb = _load(tmp_path, {"j": job}, "dev")
    values = B.job_definition(lb, "j")["trigger_values"]
    assert values["job.trigger.table_update.updated_tables"] == "[]"
    assert values["job.trigger.table_update.p.fs.features.version"] == ""
    assert values["job.trigger.table_update.commit_timestamp.timestamp_ms"] == ""


def test_trigger_references_need_a_table_update_trigger(tmp_path):
    job = _job({**DBT, "dbt_task": {**DBT["dbt_task"], "commands": ["dbt build --vars '{{job.trigger.table_update.version}}'"]}})
    with pytest.raises(ValueError, match="unsupported value reference"):
        _load(tmp_path, {"j": job})


def test_a_table_update_trigger_watches_at_most_ten_tables(tmp_path):
    with pytest.raises(ValueError, match="at most 10"):
        _load(tmp_path, {"j": _job(DBT, trigger={"table_update": {"table_names": [f"p.d.t{i}" for i in range(11)]}})})


@pytest.mark.parametrize("job", [
    _job(DBT, schedule={"quartz_cron_expression": "0 0 9 * * ?", "timezone_id": "UTC"},
         trigger={"periodic": {"interval": 1, "unit": "DAYS"}}),
    _job(DBT, trigger={"table_update": {"table_names": ["fs.features"]}}),
    _job(DBT, trigger={"file_arrival": {"url": "s3://b/x/"}}),
    _job(DBT, trigger={}),
    _job(DBT, continuous={"pause_status": "UNPAUSED"}),
    _job(DBT, health={"rules": [{"metric": "STREAMING_BACKLOG_BYTES", "op": "GREATER_THAN", "value": 1}]}),
    _job(DBT, email_notifications={"on_streaming_backlog_exceeded": ["a@b.c"]}),
    _job(DBT, tags={"Team": "UA"}),
    _job({**DBT, "email_notifications": {"on_duration_warning_threshold_exceeded": ["a@b.c"]}}),
])
def test_a_job_setting_the_platform_cannot_honour_is_refused(tmp_path, job):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, {"j": job})


def test_queue_health_and_timeouts_reach_the_definition(tmp_path):
    job = _job({**DBT, "retry_on_timeout": True}, queue={"enabled": True}, health={"rules": [
        {"metric": "RUN_DURATION_SECONDS", "op": "GREATER_THAN", "value": 1800}]})
    lb = _load(tmp_path, {"j": job}, "prod")
    d = B.job_definition(lb, "j")
    assert (d["queue"], d["duration_warning_seconds"], d["watchdog"]) == (True, 1800, "demo-watchdog")
    assert d["tasks"]["build"]["retry_on_timeout"] is True
    assert B.render(lb)["task_workflows"]["watchdog"] == "demo-watchdog"


def test_the_definition_carries_the_job_as_declared_for_the_console(tmp_path):
    job = _job({**DBT, "email_notifications": {"on_failure": ["t@x.com"]}},
               trigger={"table_update": {"table_names": ["p.fs.features"], "wait_after_last_change_seconds": 600}},
               email_notifications={"on_failure": ["ua@x.com"], "no_alert_for_skipped_runs": True},
               webhook_notifications={"on_success": [{"id": "123"}]},
               job_clusters=[{"job_cluster_key": "main", "new_cluster": {
                   "docker_image": {"url": "img"}, "node_type_id": "e2-standard-4",
                   "spark_env_vars": {"MODEL": "m"}}}])
    s = B.job_definition(_load(tmp_path, {"j": job}, "prod"), "j")["settings"]
    assert s["trigger"] == {"kind": "table_update", "table_names": ["p.fs.features"],
                            "condition": "ANY_UPDATED", "min_time_between_triggers_seconds": 0,
                            "wait_after_last_change_seconds": 600}
    assert s["notifications"] == [
        {"scope": "job", "event": "on_success", "emails": [], "webhooks": ["123"]},
        {"scope": "job", "event": "on_failure", "emails": ["ua@x.com"], "webhooks": []},
        {"scope": "build", "event": "on_failure", "emails": ["t@x.com"], "webhooks": []}]
    assert s["no_alert_for_skipped_runs"] is True
    assert s["job_clusters"] == [{"key": "main", "node_type_id": "e2-standard-4", "cpu": "4",
                                  "memory": "16Gi", "image": "img", "environment_key": None,
                                  "num_workers": None, "env_vars": {"MODEL": "m"}}]
    assert s["environments"] == []


def test_notifications_become_log_based_alerts(tmp_path):
    job = _job({**DBT, "webhook_notifications": {"on_failure": [{"id": "123"}]}},
               email_notifications={"on_success": ["ua@x.com"], "on_failure": ["ua@x.com"]},
               notification_settings={"no_alert_for_skipped_runs": True})
    alerts = {a["key"]: a for a in B.render(_load(tmp_path, {"j": job}, "prod"))["alerts"]}
    assert set(alerts) == {"j:on_success", "j:on_failure", "j.build:on_failure"}
    # The job's failure email is the platform's own (gcpbricks.notify), not the alert's.
    assert alerts["j:on_failure"]["emails"] == [] and alerts["j:on_failure"]["job_failure"]
    assert alerts["j:on_success"]["emails"] == ["ua@x.com"]
    assert 'jsonPayload.state=("FAILED")' in alerts["j:on_failure"]["filter"]
    assert alerts["j.build:on_failure"]["channels"] == ["projects/p/notificationChannels/123"]
    assert 'jsonPayload.task="build"' in alerts["j.build:on_failure"]["filter"]


def test_a_skipped_run_alerts_on_failure_unless_told_not_to(tmp_path):
    alerts = B.render(_load(tmp_path, {"j": _job(DBT)}, "prod"))["alerts"]
    assert [a["key"] for a in alerts] == ["j:on_failure"]
    assert '("FAILED" OR "SKIPPED")' in alerts[0]["filter"]


def test_tags_label_the_jobs_resources(tmp_path):
    plan = B.render(_load(tmp_path, {"j": _job(WHEEL, tags={"team": "ua"})}, "prod"))
    assert plan["jobs"]["j"]["labels"] == {"mlops_bundle": "demo", "mlops_target": "prod", "team": "ua"}
    assert plan["run_jobs"]["demo-j-main"]["labels"]["team"] == "ua"


def test_parameter_names_take_databricks_characters(tmp_path):
    job = _job(DBT, parameters=[{"name": "run.date-x", "default": "1"}])
    assert B.job_definition(_load(tmp_path, {"j": job}, "prod"), "j")["parameters"] == {"run.date-x": "1"}



# --- lookup and jobs across bundles -------------------------------------------------------

def test_a_lookup_runs_another_bundles_job_on_the_same_target(tmp_path):
    call = {"task_key": "features", "run_job_task": {"job_id": "${var.features_job}",
                                                     "job_parameters": {"day": "1"}}}
    serve = {**DBT, "depends_on": [{"task_key": "features"}]}
    lb = _load(tmp_path, {"daily": _job(call, serve)}, "dev",
               variables={"features_job": {"lookup": {"job": "dbt-demo_features"}}})
    task = B.job_definition(lb, "daily")["tasks"]["features"]
    assert task["workflow"] == "dbt-demo-features-dev"
    assert (task["argument"], task["options"]) == ({}, {"day": "1"})
    assert B.external_jobs(lb) == ["dbt-demo-features-dev"]
    assert B.external_jobs(_load(tmp_path, {"daily": _job(call, serve)}, "prod", variables={
        "features_job": {"lookup": {"job": "dbt-demo-features"}}})) == [
        "dbt-demo-features"]


@pytest.mark.parametrize("variable", [
    {"lookup": {"cluster": "x"}},
    {"lookup": {"job": "a"}, "default": "b"},
    {"lookup": {"job": "Not A Name"}},
])
def test_a_lookup_the_platform_cannot_resolve_is_refused(tmp_path, variable):
    with pytest.raises((ValidationError, ValueError)):
        _load(tmp_path, {"j": _job(DBT)}, variables={"v": variable})


def test_a_job_id_naming_nothing_resolvable_is_refused(tmp_path):
    with pytest.raises(ValueError, match="neither a job here"):
        _load(tmp_path, {"j": _job({"task_key": "c", "run_job_task": {"job_id": "Bad Name"}})})


def test_job_keys_take_underscores_and_gcp_names_take_hyphens(tmp_path):
    lb = _load(tmp_path, {"roas_daily": _job(WHEEL, DBT)}, "dev")
    plan = B.render(lb)
    assert plan["jobs"]["roas_daily"]["workflow"] == "demo-roas-daily-dev"
    assert set(plan["run_jobs"]) == {"demo-roas-daily-main-dev", "demo-dbt-dev"}
    assert plan["jobs"]["roas_daily"]["env"] == {
        "MLOPS_BUCKET": "b", "MLOPS_DEFINITION": "dev/.bundle/demo/jobs/roas_daily.json",
        "MLOPS_JOB": "roas_daily", "MLOPS_TARGET": "dev", "MLOPS_JOB_NAME": "roas_daily"}


def test_job_keys_colliding_once_hyphenated_are_refused(tmp_path):
    with pytest.raises(ValueError, match="collide"):
        _load(tmp_path, {"a_b": _job(DBT), "a-b": _job(DBT)})


def test_a_run_needs_no_argument(tmp_path):
    assert B.job_argument(_load(tmp_path, {"j": _job(DBT)}, "prod"), "j") == {}


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path):
    (tmp_path / "mlops.yaml").write_text("- just\n- a list\n")
    with pytest.raises(TypeError, match="not a mapping"):
        B.load(tmp_path / "mlops.yaml")


# --- a deploy's files, display names and retired jobs ---------------------------------------

def test_a_deploy_pins_every_task_that_reads_the_bundles_files_to_its_own(tmp_path):
    spark = {"task_key": "file", "job_cluster_key": "main", "spark_python_task": {"python_file": "x.py"}}
    lb = _load(tmp_path, {"j": _job(WHEEL, DBT, spark)}, "prod").model_copy(update={"files_version": "abc"})
    tasks = B.job_definition(lb, "j")["tasks"]
    assert tasks["build"]["env"] == tasks["file"]["env"] == {B.FILES_VERSION_ENV: "abc"}
    assert tasks["score"]["env"] == {}  # a wheel reads its image, not the bundle's files
    assert B.job_definition(lb.model_copy(update={"files_version": None}), "j")["tasks"]["build"]["env"] == {}


def test_a_jobs_name_is_its_display_name_and_its_key_its_identity(tmp_path):
    lb = _load(tmp_path, {"ua_features": _job(DBT, name="UA data")}, "prod")
    job = B.render(lb)["jobs"]["ua_features"]
    assert job["workflow"] == "demo-ua-features"
    assert (job["env"]["MLOPS_JOB"], job["env"]["MLOPS_JOB_NAME"]) == ("ua_features", "UA data")


def test_a_job_no_longer_declared_is_kept_retired_with_what_its_runs_use(tmp_path):
    for d in "abc":
        (tmp_path / d).mkdir()
    before = _load(tmp_path / "a", {"old": _job(WHEEL, DBT, schedule={"quartz_cron_expression": "0 0 1 * * ?",
                                                                      "timezone_id": "UTC"}),
                                    "kept": _job(WHEEL)}, "prod")
    record = {"job_plans": B.job_plans(before)}
    assert set(record["job_plans"]["old"]["run_jobs"]) == {"demo-old-main", "demo-dbt"}

    after = _load(tmp_path / "b", {"kept": _job(WHEEL)}, "prod")
    after = after.model_copy(update={"retired_jobs": B.retired_jobs(after, record, "2026-10-21")})
    plan = B.render(after)
    old = plan["jobs"]["old"]
    assert old["workflow"] == "demo-old" and old["schedule"] is None
    assert old["labels"]["mlops_retired"] == "2026-10-21"
    assert {"demo-old-main", "demo-dbt", "demo-kept-main"} <= set(plan["run_jobs"])
    assert not plan["alerts"] or all("old" not in a["key"] for a in plan["alerts"])

    # The next deploy keeps it, since the day it retired; declaring it again brings it back.
    again = B.retired_jobs(after, B.deployment(after, "v2", "0.7.7", "me", "", "now"), "2026-10-30")
    assert again["old"]["since"] == "2026-10-21"
    back = _load(tmp_path / "c", {"old": _job(WHEEL), "kept": _job(WHEEL)}, "prod")
    assert B.retired_jobs(back, B.deployment(after, "v2", "0.7.7", "me", "", "now"), "2026-10-30") == {}

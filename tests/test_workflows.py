"""The Workflows read exactly the arguments bundle builds: every key sent is read, and every
key read is sent or optional. Nothing runs these in CI otherwise."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from gcpbricks import bundle as B

WORKFLOWS = Path(__file__).resolve().parent.parent / "terraform" / "modules" / "bundle" / "workflows"
JOB_TEXT = (WORKFLOWS / "job.yaml").read_text()
TRIGGER_TEXT = (WORKFLOWS / "trigger.yaml").read_text()
WATCHDOG_TEXT = (WORKFLOWS / "watchdog.yaml").read_text()
WAKE_TEXT = (WORKFLOWS / "wake.yaml").read_text()
ALL_WORKFLOWS = ["job", "vertex_pipeline", "batch_serve", "notebook", "trigger", "watchdog", "wake"]

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.4.0"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "s"},
    "targets": {"prod": {"default": True}},
}
CLUSTER = {"job_cluster_key": "main", "new_cluster": {"docker_image": {"url": "img"}}}
#: One task of every type, so every descriptor shape is checked.
TASKS = [
    {"task_key": "wheel", "job_cluster_key": "main",
     "python_wheel_task": {"package_name": "p", "entry_point": "e"}},
    {"task_key": "file", "job_cluster_key": "main", "spark_python_task": {"python_file": "x.py"}},
    {"task_key": "dbt", "dbt_task": {"schema": "s", "commands": ["dbt build"]}},
    {"task_key": "check", "condition_task": {"op": "EQUAL_TO", "left": "1", "right": "1"}},
    {"task_key": "call", "depends_on": [{"task_key": "check", "outcome": "true"}],
     "run_job_task": {"job_id": "other"}},
    {"task_key": "fan", "for_each_task": {"inputs": "[1, 2]", "task": {
        "task_key": "inner", "job_cluster_key": "main",
        "python_wheel_task": {"package_name": "p", "entry_point": "e", "parameters": ["{{input}}"]}}}},
    {"task_key": "train", "vertex_pipeline_task": {"spec_prefix": "m/spec", "build": "true"}},
    {"task_key": "serve", "batch_serve_task": {
        "models": [{"model": "m", "job": "m-predict"}], "window_days": 14}},
    {"task_key": "nb", "notebook_task": {"notebook_path": "/Workspace/Shared/x.ipynb",
                                         "base_parameters": {"day": "{{job.start_time.iso_date}}"}}},
]


@pytest.fixture(scope="module")
def lb() -> B.Loaded:
    notify = {"on_start": ["a@b.c"], "on_success": ["a@b.c"], "on_failure": ["a@b.c"]}
    raw = {**BASE, "resources": {"jobs": {
        "all": {"job_clusters": [CLUSTER], "tasks": [
                    {**TASKS[0], "email_notifications": notify}, *TASKS[1:]],
                "queue": {"enabled": True},
                "health": {"rules": [{"metric": "RUN_DURATION_SECONDS", "op": "GREATER_THAN",
                                      "value": 3600}]},
                "email_notifications": {**notify, "on_duration_warning_threshold_exceeded": ["a@b.c"]},
                "trigger": {"table_update": {"table_names": ["p.d.t1", "p.d.t2"],
                                             "condition": "ALL_UPDATED"}}},
        "files": {"tasks": [TASKS[2]],
                  "trigger": {"file_arrival": {"url": "gs://landing/drops/"}}},
        "other": {"tasks": [TASKS[2]]}}}}
    return B.Loaded(bundle=B.Bundle.model_validate(raw), target="prod", root=Path("."))


def _read(text: str, var: str) -> set[str]:
    text = text.replace('"task.name"', "")  # a run value's name, not a descriptor key
    return (set(re.findall(rf"\b{var}\.([a-z_]+)", text))
            | set(re.findall(rf'map\.get\({var}, "([a-z_]+)"\)', text))
            | set(re.findall(rf'"([a-z_]+)" in {var}\b', text)))


def _descriptors(lb: B.Loaded) -> list[dict]:
    tasks = list(B.job_definition(lb, "all")["tasks"].values())
    return tasks + [t["task"] for t in tasks if t["type"] == "for_each"]


def test_the_job_workflow_reads_its_definition_from_the_env_the_module_sets(lb):
    assert B.job_argument(lb, "all") == {} and _read(JOB_TEXT, "args") == set()
    read = set(re.findall(r'sys\.get_env\("(MLOPS_[A-Z_]+)"\)', JOB_TEXT))
    assert read == set(B.job_env(lb, "all"))
    assert '"params")' in JOB_TEXT  # a run's parameters are the only argument


def test_the_job_workflow_reads_exactly_what_a_task_descriptor_holds(lb):
    sent = set().union(*(_descriptors(lb)))
    read = _read(JOB_TEXT, "task")
    assert sent - read == set(), f"sent but never read: {sorted(sent - read)}"
    assert read - sent == set(), f"read but never sent: {sorted(read - sent)}"


def test_every_task_type_has_a_branch(lb):
    types = {d["type"] for d in _descriptors(lb)}
    branches = set(re.findall(r'task\.type == "([a-z_]+)"', JOB_TEXT))
    assert types == branches


@pytest.mark.parametrize("kind, optional", [
    ("vertex_pipeline", {"overrides", "poll_seconds", "repair"}),  # repair: runs.repair_argument's
    ("batch_serve", B.BATCH_SERVE_PARAMS | {"repair"}),  # repair: a repair run's (runs.repair_argument)
    ("notebook", {"parameters", "poll_seconds"}),
])
def test_a_task_runner_reads_exactly_what_its_task_sends(lb, kind, optional):
    task = B.job_definition(lb, "all")["tasks"][{"vertex_pipeline": "train", "batch_serve": "serve", "notebook": "nb"}[kind]]
    text = (WORKFLOWS / f"{kind}.yaml").read_text()
    sent = set(task["argument"])
    read = _read(text, "args") | _read(text, "cfg")  # batch_serve hands args on as cfg
    assert sent - read == set(), f"sent but never read: {sorted(sent - read)}"
    assert read - sent - optional == set(), f"read but never sent: {sorted(read - sent - optional)}"


def test_the_job_workflow_reads_exactly_the_definition(lb):
    """All but `settings`, the job as declared, which only the console reads."""
    sent = set(B.job_definition(lb, "all")) - {"settings"}
    read = _read(JOB_TEXT, "defn")
    assert sent == read, (sorted(sent - read), sorted(read - sent))


@pytest.mark.parametrize("job, other_kind", [
    ("all", {"watch_bucket", "watch_prefix"}),
    ("files", {"tables", "condition"}),
])
def test_the_trigger_workflow_reads_exactly_what_a_trigger_sends(lb, job, other_kind):
    trigger = lb.bundle.resources.jobs[job].trigger
    sent = set(B._trigger_argument(lb, job, trigger))
    read = _read(TRIGGER_TEXT, "args")
    assert sent - read == set(), f"sent but never read: {sorted(sent - read)}"
    assert read - sent - other_kind == set(), f"read but never sent: {sorted(read - sent)}"


def test_the_trigger_reads_tables_as_the_bundle_splits_them(lb):
    tables = B._trigger_argument(lb, "all", lb.bundle.resources.jobs["all"].trigger)["tables"]
    assert tables[0] == {"name": "p.d.t1", "kind": "bigquery", "project": "p", "dataset": "d", "table": "t1"}
    iceberg = B._watched_table("p.cat.ns.t")
    assert set(tables[0]) | set(iceberg) == set(re.findall(r"\bt\.([a-z]+)", TRIGGER_TEXT))


def test_the_watchdog_reads_exactly_what_a_run_sends_it():
    block = JOB_TEXT.split("- watched:")[1].split("- watchdog:")[0]
    sent = set(re.findall(r"^\s+([a-z_]+): ", block, re.MULTILINE))
    assert sent == _read(WATCHDOG_TEXT, "args")


def test_every_alert_matches_a_state_the_workflows_log(lb):
    plan = B.render(lb)
    logged_runs = set(re.findall(r'state: "([A-Z_]+)"', JOB_TEXT)) | {"SUCCEEDED", "FAILED"}
    logged_runs |= set(re.findall(r"state: ([A-Z_]+)\n", WATCHDOG_TEXT))
    logged_tasks = set(re.findall(r'status: "([A-Z_]+)"', JOB_TEXT)) | {"SUCCESS"}
    for alert in plan["alerts"]:
        states = set(re.findall(r'"([A-Z_]+)"', alert["filter"].split("=(")[-1]))
        logged = logged_tasks if "mlops_task" in alert["filter"] else logged_runs
        assert states <= logged, (alert["key"], states - logged)
    assert {a["key"] for a in plan["alerts"]} >= {
        "all:on_start", "all:on_success", "all:on_failure",
        "all:on_duration_warning_threshold_exceeded", "all.wheel:on_failure", "files:on_failure"}


def test_the_workflow_files_are_the_runners_and_the_job():
    assert {p.stem for p in WORKFLOWS.glob("*.yaml")} == set(ALL_WORKFLOWS)


def _step_names(node) -> list[str]:
    """Every step name under `node`, at any depth (for, switch, try, parallel)."""
    if isinstance(node, list):
        return [n for item in node for n in _step_names(item)]
    if not isinstance(node, dict):
        return []
    names = []
    for key, value in node.items():
        if key == "steps" and isinstance(value, list):
            names += [name for step in value for name in step]
        names += _step_names(value)
    return names


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_step_names_are_unique_within_each_subworkflow(name):
    # Cloud Workflows refuses to deploy a subworkflow that reuses a step name at any depth.
    for sub, body in yaml.safe_load((WORKFLOWS / f"{name}.yaml").read_text()).items():
        names = _step_names(body)
        assert len(names) == len(set(names)), (sub, sorted({n for n in names if names.count(n) > 1}))


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_every_subworkflow_called_is_defined(name):
    text = (WORKFLOWS / f"{name}.yaml").read_text()
    called = {c for c in re.findall(r"call: ([a-z_]+)\n", text)}
    assert called <= set(yaml.safe_load(text))


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_no_expression_is_longer_than_workflows_allows(name):
    # Cloud Workflows refuses to deploy an expression over 400 characters.
    text = (WORKFLOWS / f"{name}.yaml").read_text()
    expressions = re.findall(r"\$\{(.*?)\}'?$", text, re.MULTILINE)
    assert not [e for e in expressions if len(e) + 3 > 400]


def test_every_run_value_the_bundle_allows_is_set():
    set_ = set(re.findall(r'values\["([a-z_.]+)"\]', JOB_TEXT))
    assert set_ - {"input"} == B.RUN_VALUES and 'item_values["input"]' in JOB_TEXT
    assert 'values["task.name"]' in JOB_TEXT
    assert 'values["job.parameters." + name]' in JOB_TEXT


def test_every_run_if_is_evaluated():
    run_if = set(B.RUN_IF.__args__)
    assert run_if == set(re.findall(r'r == "([A-Z_]+)"', JOB_TEXT))


def test_every_condition_op_is_evaluated():
    ops = set(B.ConditionTask.model_fields["op"].annotation.__args__)
    assert ops == set(re.findall(r'op == "([A-Z_]+)"', JOB_TEXT))


def test_the_wake_workflow_reads_the_file_triggers_argument_and_the_modules_env(lb):
    argument = B._trigger_argument(lb, "files", lb.bundle.resources.jobs["files"].trigger)
    assert _read(WAKE_TEXT, "cfg") <= set(argument)
    env = set(re.findall(r'sys\.get_env\("(MLOPS_[A-Z_]+)"\)', WAKE_TEXT))
    main_tf = (WORKFLOWS.parent / "main.tf").read_text()
    assert env == set(re.findall(r"(MLOPS_[A-Z_]+) = ", main_tf))


def test_a_file_trigger_gets_a_wake_workflow_and_a_table_trigger_none(lb):
    triggers = B.render(lb)["triggers"]
    assert (triggers["files"]["file_bucket"], triggers["files"]["wake_workflow"]) == (
        "landing", "demo-files-wake")
    assert triggers["all"]["file_bucket"] is None and triggers["all"]["wake_workflow"] is None


def test_the_trigger_state_is_written_before_a_run_starts():
    main = TRIGGER_TEXT.split("observe_tables:")[0]
    assert main.index("    - save:") < main.index("    - lost_the_race:") < main.index("start_job:")
    assert "ifGenerationMatch: ${generation}" in TRIGGER_TEXT



@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_no_expression_uses_what_workflows_cannot_parse(name):
    # Found deploying: Workflows' expression parser takes no map literal at all ("extraneous
    # input '{'"); maps are built with assign steps.
    text = (WORKFLOWS / f"{name}.yaml").read_text()
    expressions = re.findall(r"\$\{(.*?)\}'?$", text, re.MULTILINE)
    # A "{" outside a string literal is a map literal; "{{name}}" placeholders are strings.
    unquoted = [re.sub(r'"(?:[^"\\]|\\.)*"', '""', e) for e in expressions]
    assert not [e for e in unquoted if "{" in e], [e[:80] for e in unquoted if "{" in e]


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_no_if_branch_can_raise(name):
    # Found deploying: Workflows evaluates both branches of if(), so json.decode of a map
    # in the unused one fails the step. Branches that can raise go in a switch.
    text = (WORKFLOWS / f"{name}.yaml").read_text()
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in lines if "if(" in ln and "json.decode" in ln.split("if(", 1)[1]]


def test_the_console_names_each_task_by_its_bundle_task_type(lb):
    from gcpbricks.console.service import task_kind
    tasks = B.job_definition(lb, "all")["tasks"]
    kinds = {key: task_kind(t) for key, t in tasks.items()}
    declared = {t["task_key"]: next(k for k in t if k.endswith("_task")) for t in TASKS}
    assert kinds == declared
    assert task_kind(tasks["fan"]["task"]) == "python_wheel_task"
    # A script run as `python -c ...` is still a script, not a wheel.
    assert task_kind({"type": "cloud_run", "args": ["python", "-c", "print(1)"]}) == "spark_python_task"


def test_a_repair_hands_a_task_its_argument_and_keeps_the_child_run_a_later_repair_reads():
    run_task = {k: v for step in yaml.safe_load(JOB_TEXT)["run_task"]["steps"] for k, v in step.items()}
    assert run_task["repair_argument"]["switch"][0]["assign"] == [
        {"task.argument": "${map.merge(task.argument, reuse.argument)}"}]
    reused = run_task["reuse_success"]["switch"][0]["steps"][1]["reused"]
    assert reused == {"return": "${reuse}"}
    assert run_task["ran_child"]["switch"][0]["assign"][0]["done"]["execution"] == "${outcome.execution}"
    assert run_task["succeeded"] == {"return": "${done}"}
    # A cancelled run returns no results: its repair reads them from these logs.
    assert run_task["log_ok"]["args"]["result"] == "${done}"
    assert run_task["reuse_success"]["switch"][0]["steps"][0]["log_reused"]["args"]["result"] == "${reuse}"
    assert run_task["attempt"]["except"]["steps"][2]["failed"]["return"]["execution"] == "${child_run}"


def test_a_repair_keeps_a_task_it_did_not_pick_and_a_run_fails_by_its_leaf_tasks():
    main = {k: v for step in yaml.safe_load(JOB_TEXT)["main"]["steps"] for k, v in step.items()}
    run_task = {k: v for step in yaml.safe_load(JOB_TEXT)["run_task"]["steps"] for k, v in step.items()}
    reuse = run_task["reuse_success"]["switch"][0]
    assert reuse["condition"] == '${reuse != null and reuse.status != "REPAIR"}'
    assert reuse["steps"][0]["log_reused"]["args"]["status"] == '${if(reuse.status == "SUCCESS", "REUSED", "KEPT")}'
    feeds = main["find_feeds"]["for"]["steps"][0]["each_dependency"]["for"]
    # A run of some tasks (only): a task outside them depends on nothing it ran, so `--only forecast` fails on forecast.
    mark = feeds["steps"][0]["mark_feeds"]["switch"][0]
    assert mark["condition"] == "${only == null or tkey in only}"
    assert mark["steps"][0]["fed"]["assign"] == [{"feeds[dep.task_key]": True}]
    check = main["find_failed"]["for"]["steps"][0]["check_one"]["switch"][0]["condition"]
    assert check.startswith("${not (tkey in feeds) and ") and "UPSTREAM_FAILED" in check


def test_a_pipeline_runs_id_is_one_vertex_accepts():
    # A task_key like forecast_30d holds "_", which Vertex refuses in a pipeline job id.
    text = (WORKFLOWS / "vertex_pipeline.yaml").read_text()
    line = next(ln for ln in text.splitlines() if "- run_id:" in ln)
    assert 'text.replace_all(text.to_lower(args.job + "-" + args.target), "_", "-")' in line
    assert text.count("pipelineJobId: ${run_id}") == 2  # a rerun and a fresh run alike


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_every_started_workflow_is_started_not_waited_on(name):
    """executions.create waits for the run it starts (up to 30 minutes) unless told not to: the
    caller would log its child only once it ended, and fail on a longer one."""
    text = (WORKFLOWS / f"{name}.yaml").read_text()
    for m in re.finditer(r"call: googleapis\.workflowexecutions\.v1\.projects\.locations\.workflows\.executions\.create\n", text):
        block = text[m.end():m.end() + 600].split("\n    - ")[0]
        assert "skip_polling: true" in block, f"{name}.yaml line {text[:m.start()].count(chr(10)) + 1}"


def test_batch_serve_arithmetic_survives_a_task_without_window_days():
    """A task whose every model has its own window sends window_days null, and Workflows'
    default() evaluates both arguments: the fallback must never do arithmetic on a bare null."""
    text = (WORKFLOWS / "batch_serve.yaml").read_text()
    raw = {**BASE, "resources": {"jobs": {"s": {"tasks": [{"task_key": "serve", "batch_serve_task": {
        "models": [{"model": "m", "job": "m-predict", "window": {"from_days": -17}}]}}]}}}}
    lb = B.Loaded(bundle=B.Bundle.model_validate(raw), target="prod", root=Path("."))
    arg = B.job_definition(lb, "s")["tasks"]["serve"]["argument"]
    assert arg["window_days"] is None and arg["models"][0]["from_days"] == -17
    assert len(re.findall(r"args\.window_days", text)) == len(re.findall(r"default\(args\.window_days, 0\)", text)) > 0


def test_a_pipeline_run_holds_no_pipeline_spec_while_it_waits():
    """A Vertex pipeline job's resource carries its whole pipeline spec (demo_model's retrain is
    ~240 KB, its GET ~340 KB once tasks report), and a Workflows execution's variables are
    capped at 512 KB: every Vertex call asks for the fields it reads, and the spec, the request
    body and the create response are let go once the run is created."""
    doc = yaml.safe_load((WORKFLOWS / "vertex_pipeline.yaml").read_text())
    steps = {k: v for s in doc["main"]["steps"] for k, v in s.items()}
    assert steps["in_flight"]["args"]["query"]["fields"] == "pipelineJobs.name"
    assert steps["poll"]["args"]["query"]["fields"] == "name,state,error"
    assert "run_name" in steps["poll"]["args"]["url"]
    released = {k for a in steps["started"]["assign"] for k, v in a.items() if v is None}
    assert {"body", "orig", "created"} <= released
    assert {"spec": None} in steps["run_body"]["assign"]
    text = (WORKFLOWS / "vertex_pipeline.yaml").read_text()
    assert text.count("fields: name\n") == 2  # a rerun's create and a fresh one's alike
    assert "fields: labels,jobDetail.taskDetails(taskId,taskName,taskUniqueName,state)" in text


"""Publishing a build, the bundle's files and the CLI around them, without GCP: --out-dir
writes the tree GCS would get."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from gcpbricks import cli
from gcpbricks import runs as runs_mod
from gcpbricks.publish import built_names, pointer, publish


def _build(tmp_path, names=("train",)):
    d = tmp_path / "build"
    d.mkdir()
    for n in names:
        (d / f"{n}_pipeline.json").write_text("{}")
        (d / f"{n}_params.json").write_text('{"parameter_values": {}}')
    return d


def test_publish_writes_versions_then_a_pointer_to_them(tmp_path):
    out = tmp_path / "out"
    body = publish(_build(tmp_path, ("train", "hp_tune")), bucket="b", base="m/pipeline_spec",
                   version="v1", image="img", out_dir=out)
    assert body == pointer("v1", "img", "gs://b/m/pipeline_spec", ["hp_tune", "train"])
    assert json.loads((out / "m/pipeline_spec/current.json").read_text()) == body
    assert (out / "m/pipeline_spec/v1/hp_tune_params.json").is_file()


def test_a_build_missing_its_params_is_refused(tmp_path):
    d = _build(tmp_path)
    (d / "train_params.json").unlink()
    with pytest.raises(FileNotFoundError):
        built_names(d)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    (tmp_path / "build.py").write_text(
        "import os, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "(out / 'train_pipeline.json').write_text('{}')\n"
        "(out / 'train_params.json').write_text('{\"target\": \"%s\"}' % os.environ['TARGET'])\n")
    (tmp_path / "dbt").mkdir()
    (tmp_path / "dbt" / "dbt_project.yml").write_text("name: x\nprofile: x\n")
    (tmp_path / "secret.env").write_text("x")
    (tmp_path / ".gitignore").write_text("secret.env\nout/\n")
    (tmp_path / "mlops.yaml").write_text(yaml.safe_dump({
        "bundle": {"name": "demo", "mlops_version": "0.4.0"},
        "gcp": {"project": "p", "bucket": "b"},
        "run_as": {"service_principal_name": "s"},
        "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
        "resources": {"jobs": {"retrain": {
            "parameters": [{"name": "force", "default": "false"}],
            "tasks": [
                {"task_key": "build", "dbt_task": {"project_directory": "dbt", "schema": "s",
                                                   "commands": ["dbt build"]}},
                {"task_key": "train", "depends_on": [{"task_key": "build"}],
                 "vertex_pipeline_task": {"spec_prefix": "m/pipeline_spec",
                                          "build": f"{sys.executable} build.py {{out_dir}}"}},
            ]}}},
    }))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_deploy_uploads_the_files_and_publishes_under_the_targets_tree(project):
    out = project / "out"
    assert cli.main(["bundle", "deploy", "--version", "v9", "--out-dir", str(out)]) == 0
    params = json.loads((out / "dev/m/pipeline_spec/v9/train_params.json").read_text())
    assert params == {"target": "dev"}
    files = out / "b" / "dev" / ".bundle" / "demo" / "files"
    assert json.loads((files / "current.json").read_text())["version"] == "v9"

    import tarfile
    with tarfile.open(files / "v9.tar.gz") as tar:
        names = set(tar.getnames())
    assert {"mlops.yaml", "dbt/dbt_project.yml", "build.py"} <= names
    assert "secret.env" not in names


def test_deploy_records_what_it_deployed(project):
    out = project / "out"
    assert cli.main(["bundle", "deploy", "--version", "v9", "--out-dir", str(out)]) == 0
    record = json.loads((out / "b" / "dev" / ".bundle" / "demo" / "deployment.json").read_text())
    assert (record["bundle"], record["target"], record["mode"], record["version"]) == ("demo", "dev", "development", "v9")
    assert record["resources"]["jobs"] == {"retrain": "demo-retrain-dev"}
    assert record["mlops_version"] and record["deployed_at"]


class _DryRun:
    """BigQuery's jobs.insert, answering a dry run with a fixed schema."""

    def __init__(self, columns, error=None):
        self.columns, self.error, self.bodies = columns, error, []

    def post(self, url, json):
        self.bodies.append(json)
        ok, columns, error = self.error is None, self.columns, self.error

        class R:
            def __init__(self):
                self.ok, self.text = ok, error or ""

            def json(self):
                if not ok:
                    return {"error": {"message": error}}
                return {"statistics": {"query": {"schema": {"fields": [{"name": c} for c in columns]}}}}
        return R()


def _with_dashboard(project, widget, sql="SELECT day, model, mape FROM t WHERE day >= @start AND model = @model OR @model = ''"):
    (project / "ops.yaml").write_text(yaml.safe_dump({
        "datasets": {"acc": {"bigquery": sql, "time": "day"}},
        "filters": {"model": {"dataset": "acc", "field": "model"}},
        "widgets": [widget]}))
    raw = yaml.safe_load((project / "mlops.yaml").read_text())
    raw["resources"]["dashboards"] = {"ops": {"display_name": "ops", "file_path": "ops.yaml"}}
    (project / "mlops.yaml").write_text(yaml.safe_dump(raw))
    from gcpbricks import bundle as B
    return B.load(project / "mlops.yaml")


def test_a_dashboard_datasets_sql_is_dry_run_and_its_columns_checked(project):
    lb = _with_dashboard(project, {"title": "MAPE", "type": "table", "dataset": "acc",
                                   "measure": "avg(mape)", "by": "model"})
    session = _DryRun(["day", "model", "mape"])
    assert cli._dashboard_problems(lb, session) == []
    q = session.bodies[0]["configuration"]
    assert q["dryRun"] is True
    assert {p["name"] for p in q["query"]["queryParameters"]} == {"start", "model"}
    assert cli._dashboard_problems(lb, _DryRun(["day", "model"])) == [
        "ops.acc: no column ['mape']; it returns ['day', 'model']"]
    assert cli._dashboard_problems(lb, _DryRun([], error="Unrecognized name: mape")) == [
        "ops.acc: Unrecognized name: mape"]


def test_render_validate_and_summary_run_offline(project, capsys):
    assert cli.main(["bundle", "validate"]) == 0
    assert cli.main(["bundle", "summary", "-t", "prod"]) == 0
    capsys.readouterr()
    assert cli.main(["bundle", "render", "-t", "prod"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["jobs"]["retrain"]["workflow"] == "demo-retrain"


def test_an_unknown_target_is_refused(project):
    with pytest.raises(SystemExit):
        cli.main(["bundle", "render", "-t", "staging"])


def test_a_databricks_key_the_platform_does_not_run_says_so(project, capsys):
    raw = yaml.safe_load((project / "mlops.yaml").read_text())
    raw["resources"]["jobs"]["retrain"]["continuous"] = {"pause_status": "UNPAUSED"}
    (project / "mlops.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(SystemExit) as e:
        cli.main(["bundle", "validate"])
    assert "continuous: not supported by gcpbricks" in str(e.value)


def test_run_refuses_undeclared_parameters(project):
    with pytest.raises(SystemExit, match="not \\['nope'\\]"):
        cli.main(["bundle", "run", "retrain", "--params", "nope=1"])


def test_a_repair_reuses_the_failed_runs_successes_and_parameters():
    execution = {"state": "FAILED", "argument": json.dumps({"params": {"day": "1"}}),
                 "error": {"payload": json.dumps({"message": "x", "tasks": {
                     "a": {"status": "SUCCESS", "outcome": "true"}, "b": {"status": "FAILED", "outcome": ""},
                     "c": {"status": "UPSTREAM_FAILED", "outcome": ""}}})}}
    assert cli.repair_argument(execution) == {
        "params": {"day": "1"}, "repair": {"a": {"status": "SUCCESS", "outcome": "true"}}}


def test_a_run_that_succeeded_with_failures_is_repaired_and_a_clean_success_is_not():
    # serve_ua_inference: a serve task failed, its dependent ran on AT_LEAST_ONE_SUCCESS, the leaf succeeded.
    tasks = {"a": {"status": "SUCCESS", "outcome": ""}, "b": {"status": "FAILED", "outcome": ""},
             "c": {"status": "SUCCESS", "outcome": ""}}
    execution = {"state": "SUCCEEDED", "argument": "{}", "result": json.dumps({"status": "SUCCEEDED", "tasks": tasks})}
    assert runs_mod.repair_argument(execution, tasks=["c"]) == {"repair": {"a": {"status": "SUCCESS", "outcome": ""}}}
    clean = {**execution, "result": json.dumps({"status": "SUCCEEDED", "tasks": {**tasks, "b": tasks["a"]}})}
    with pytest.raises(ValueError, match="or one that succeeded with failures, can be repaired; this one is SUCCEEDED"):
        runs_mod.repair_argument(clean)


def test_a_repair_of_some_tasks_keeps_to_them():
    execution = {"state": "FAILED", "argument": json.dumps({"only": {"b": True}}),
                 "error": {"payload": json.dumps({"tasks": {"a": {"status": "EXCLUDED", "outcome": ""},
                                                            "b": {"status": "FAILED", "outcome": ""}}})}}
    assert cli.repair_argument(execution) == {"only": {"b": True}, "repair": {}}


CHILD = "projects/p/locations/r/workflows/demo-batch-serve/executions/c1"
SERVED = {"run_id": "daily-serve-prod-1", "started_ms": 1790000000000, "models": [
    {"model": "d30", "from_date": "2026-09-01", "to_date": "2026-09-30", "run_id": "daily-serve-prod-1", "status": "SUCCEEDED"},
    {"model": "d90", "from_date": "2026-07-03", "to_date": "2026-09-30", "run_id": "daily-serve-prod-1", "status": "FAILED"}]}


def _served_run(serve_status="FAILED"):
    run = {"state": "FAILED", "argument": "{}", "error": {"payload": json.dumps({"tasks": {
        "features": {"status": "SUCCESS", "outcome": ""},
        "serve": {"status": serve_status, "outcome": "", "execution": CHILD},
        "after": {"status": "FAILED", "outcome": ""}}})}}
    ok = {**SERVED, "models": [{**m, "status": "SUCCEEDED"} for m in SERVED["models"]]}
    child = {"state": serve_status.replace("SUCCESS", "SUCCEEDED"),
             **({"result": json.dumps(ok)} if serve_status == "SUCCESS"
                else {"error": {"payload": json.dumps({"message": "serving failed", **SERVED})}})}
    return run, lambda name: {CHILD: child}[name]


def test_a_repair_resumes_a_batch_serve_tasks_failed_models_only():
    run, child = _served_run()
    serve = cli.repair_argument(run, None, child)["repair"]["serve"]
    assert serve["status"] == "REPAIR"
    r = serve["argument"]["repair"]
    assert (r["run_id"], r["started_ms"], r["resume"], r["fresh"]) == ("daily-serve-prod-1", 1790000000000, ["d90"], [])
    assert r["models"]["d90"] == {"from_date": "2026-07-03", "to_date": "2026-09-30", "run_id": "daily-serve-prod-1"}


def test_a_repair_runs_the_models_asked_for_again_in_full_even_after_success():
    run, child = _served_run()
    r = cli.repair_argument(run, ["d30", "d90"], child)["repair"]["serve"]["argument"]["repair"]
    assert (r["resume"], r["fresh"]) == ([], ["d30", "d90"])
    run, child = _served_run("SUCCESS")
    argument = cli.repair_argument(run, ["d30"], child)
    assert argument["repair"]["serve"]["argument"]["repair"]["fresh"] == ["d30"]  # not reused
    assert set(argument["repair"]) == {"features", "serve"}


def test_a_succeeded_batch_serve_task_is_reused_and_an_unknown_model_refused():
    run, child = _served_run("SUCCESS")
    assert cli.repair_argument(run, None, child)["repair"]["serve"]["status"] == "SUCCESS"
    with pytest.raises(SystemExit, match="served \\['nope'\\]"):
        cli.repair_argument(run, ["nope"], child)


def test_a_repair_runs_the_succeeded_tasks_picked_again_and_a_picked_serve_task_in_full():
    from gcpbricks import runs

    run, child = _served_run("SUCCESS")
    argument = runs.repair_argument(run, None, child, ["features", "serve"])
    assert "features" not in argument["repair"]
    assert argument["repair"]["serve"]["argument"]["repair"]["fresh"] == ["d30", "d90"]
    with pytest.raises(ValueError, match="no task"):
        runs.repair_argument(run, None, child, ["nope"])


def test_a_batch_serve_run_that_said_nothing_runs_again_from_the_start():
    run, _ = _served_run()
    assert "serve" not in cli.repair_argument(run, None, lambda name: {"state": "CANCELLED"})["repair"]


def test_a_cancelled_run_is_repaired_from_the_results_its_logs_hold():
    from gcpbricks import runs

    logged = {"a": {"status": "SUCCESS", "outcome": "true"}, "b": {"status": "CANCELLED", "outcome": ""}}
    argument = runs.repair_argument({"state": "CANCELLED", "argument": "{}"}, logged=logged)
    assert argument == {"repair": {"a": {"status": "SUCCESS", "outcome": "true"}}}
    with pytest.raises(ValueError, match="no task results"):
        runs.repair_argument({"state": "CANCELLED", "argument": "{}"})


def test_a_repair_keeps_a_failed_task_it_was_told_not_to_run_as_it_ended():
    from gcpbricks import runs

    run = {"state": "FAILED", "argument": "{}", "error": {"payload": json.dumps({"tasks": {
        "build": {"status": "FAILED", "outcome": ""}, "retry": {"status": "FAILED", "outcome": ""}}})}}
    assert runs.repair_argument(run, keep=["build"]) == {"repair": {"build": {"status": "FAILED", "outcome": ""}}}
    with pytest.raises(ValueError, match="kept as it ended"):
        runs.repair_argument(run, keep=["nope"])
    with pytest.raises(ValueError, match="kept as it ended"):
        runs.repair_argument(run, tasks=["build"], keep=["build"])


@pytest.mark.parametrize("execution", [{"state": "SUCCEEDED"},
                                       {"state": "FAILED", "error": {"payload": "\"timed out\""}}])
def test_only_a_failed_run_with_task_results_can_be_repaired(execution):
    with pytest.raises(SystemExit):
        cli.repair_argument(execution)


def test_redeploy_takes_bundles_on_latest_from_their_saved_files(tmp_path, monkeypatch):
    from google.cloud import storage

    from gcpbricks import bundle as B
    from gcpbricks import files

    records = {"dev/.bundle/a/deployment.json": {"bundle": "a", "version": "s1", "declared_mlops_version": "latest"},
               "dev/.bundle/b/deployment.json": {"bundle": "b", "version": "s2", "declared_mlops_version": "0.5.4"},
               "dev/.bundle/a/files/s1.tar.gz": None}

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_as_bytes(self):
            return json.dumps(records[self.name]).encode()

    class Bucket:
        def list_blobs(self, prefix):
            return [Blob(n) for n in records if n.startswith(prefix)]
    monkeypatch.setattr(storage, "Client", lambda: type("C", (), {"bucket": lambda self, name: Bucket()})())
    fetched, deployed = [], []
    monkeypatch.setattr(files, "fetch", lambda uri, root, version: fetched.append((uri, version)))
    monkeypatch.setattr(B, "load", lambda path, target: ("loaded", path.name, target))
    monkeypatch.setattr(cli, "deploy", lambda lb, image, version, out_dir, snapshot: deployed.append(
        (lb, version, snapshot)) or 0)
    assert cli.redeploy("b", "dev", None) == 0
    assert fetched == [("gs://b/dev/.bundle/a/files", "s1")]
    assert deployed == [(("loaded", "mlops.yaml", "dev"), "s1", True)]
    fetched.clear()
    assert cli.redeploy("b", "dev", "b") == 0 and fetched == [("gs://b/dev/.bundle/b/files", "s2")]


def test_latest_runs_on_the_release_deploying_it(tmp_path):
    from importlib.metadata import version

    from gcpbricks import bundle as B
    from gcpbricks import environments, infra

    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({"bundle": {"name": "demo", "mlops_version": "latest"},
                                    "gcp": {"project": "p", "bucket": "b"}, "run_as": {"service_principal_name": "s"},
                                    "targets": {"dev": {"mode": "development", "default": True}}}))
    lb = B.load(path, "dev")
    assert environments.runtime_image(lb).endswith(f"/runtime:{version('gcpbricks')}")
    assert infra.dbt_image(lb).endswith(f"/dbt:{version('gcpbricks')}")


def test_a_deploy_records_the_commit_it_ships(monkeypatch, tmp_path):
    lb = type("LB", (), {"root": tmp_path})()
    monkeypatch.setenv("CI_COMMIT_SHA", "dc40c32aa")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "main")
    monkeypatch.setenv("CI_PROJECT_URL", "https://git.example/p")
    assert cli._source(lb) == {"commit": "dc40c32aa", "branch": "main",
                               "commit_url": "https://git.example/p/-/commit/dc40c32aa"}
    monkeypatch.delenv("CI_COMMIT_SHA")
    assert cli._source(lb) == {}  # not a git checkout


def test_logs_drop_the_executor_input_dump_and_the_runtimes_warnings():
    entries = [("t1", "INFO", "[KFP Executor 2026-11-05 01:49:40,683 INFO]: Got executor_input:"),
               ("t2", "INFO", "{"), ("t3", "INFO", '    "inputs": {'), ("t4", "INFO", "    }"), ("t5", "INFO", "}"),
               ("t6", "ERROR", "/x/_python_version_support.py:255: FutureWarning: You are using a Python version"),
               ("t7", "ERROR", "  warnings.warn(message, FutureWarning)"), ("t8", "INFO", "Job is running."),
               ("t9", "INFO", "[cell_hit_rate] n_segments=1335022  n=0  n_cells=0"), ("t10", "INFO", " ")]
    assert cli._quiet(entries) == [("t9", "INFO", "[cell_hit_rate] n_segments=1335022  n=0  n_cells=0")]

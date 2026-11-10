"""What a task's container runs: the wheel launcher, and mlops-task dbt over the bundle's
files."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import yaml

from gcpbricks import bundle as B
from gcpbricks import files, task_runner


def test_the_wheel_launcher_runs_an_entry_point_with_its_parameters():
    # pytest's console script, found the way a python_wheel_task finds its entry point.
    done = subprocess.run([sys.executable, "-c", B.WHEEL_LAUNCHER, "pytest", "pytest", "--version"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0 and "pytest" in (done.stdout + done.stderr)


def _deployed(tmp_path, with_packages=False):
    project = tmp_path / "project"
    (project / "dbt").mkdir(parents=True)
    (project / "dbt" / "dbt_project.yml").write_text("name: sd\nprofile: demo_app\n")
    if with_packages:
        (project / "dbt" / "packages.yml").write_text("packages: []\n")
    paths = [str(p.relative_to(project)) for p in project.rglob("*") if p.is_file()]
    root = tmp_path / "gcs"
    files.upload("gs://b/dev/.bundle/demo/files", "v1", files.archive(project, paths), root)
    return root


def _runner(calls, fail_on=None):
    def run(argv, cwd, env, check):
        calls.append((argv, cwd, env["DBT_PROFILES_DIR"]))
        return SimpleNamespace(returncode=1 if argv == fail_on else 0)
    return run


def _args(root, **kw):
    a = {"files": "gs://b/dev/.bundle/demo/files", "project_directory": "dbt",
         "profiles_directory": None, "catalog": "proj", "schema": "ds", "location": None, "local_root": root,
         "state_location": None, "commands": ["dbt build -s tag:x", "dbt test"]}
    return SimpleNamespace(**(a | kw))


def test_dbt_runs_each_command_in_the_project_with_a_bigquery_profile(tmp_path):
    calls = []
    assert task_runner.dbt(_args(_deployed(tmp_path)), _runner(calls)) == 0
    assert [c[0] for c in calls] == [["dbt", "build", "-s", "tag:x"], ["dbt", "test"]]
    assert calls[0][1].name == "dbt"
    profile = yaml.safe_load((calls[0][1].parent / ".mlops-profiles" / "profiles.yml").read_text())
    assert profile == {"demo_app": {"target": "mlops", "outputs": {"mlops": {
        "type": "bigquery", "method": "oauth", "project": "proj", "dataset": "ds"}}}}


def test_dbt_profile_carries_the_location_dbt_creates_datasets_in(tmp_path):
    calls = []
    assert task_runner.dbt(_args(_deployed(tmp_path), location="us-central1"), _runner(calls)) == 0
    profile = yaml.safe_load((calls[0][1].parent / ".mlops-profiles" / "profiles.yml").read_text())
    assert profile["demo_app"]["outputs"]["mlops"]["location"] == "us-central1"


def test_dbt_installs_packages_first_and_stops_at_the_first_failure(tmp_path):
    calls = []
    code = task_runner.dbt(_args(_deployed(tmp_path, with_packages=True)),
                           _runner(calls, fail_on=["dbt", "build", "-s", "tag:x"]))
    assert code == 1
    assert [c[0] for c in calls] == [["dbt", "deps"], ["dbt", "build", "-s", "tag:x"]]


def test_dbt_refuses_a_command_that_is_not_dbt(tmp_path):
    calls = []
    assert task_runner.dbt(_args(_deployed(tmp_path), commands=["bash -c x"]), _runner(calls)) == 2
    assert calls == []


STATE = "gs://b/dev/.bundle/demo/dbt-state/run-1"


def _dbt_writing_state(seen, fail_on=None, silent=()):
    """A dbt that records the run_results it finds in target/, then writes its own, as dbt
    does for every command but deps (and those in `silent`, failing before it writes any)."""
    def run(argv, cwd, env, check):
        results = cwd / "target" / "run_results.json"
        seen.append((argv[1], results.read_text() if results.exists() else None))
        if argv[1] != "deps" and argv[1] not in silent:
            results.parent.mkdir(exist_ok=True)
            results.write_text(argv[1])
            (cwd / "target" / "manifest.json").write_text("{}")
        return SimpleNamespace(returncode=1 if argv == fail_on else 0)
    return run


def test_a_retry_task_finds_the_failed_tasks_run_results_after_deps(tmp_path, monkeypatch):
    # In Cloud Run, save_run_results removes each command's run_results.json once kept.
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "exec-1")
    root = _deployed(tmp_path, with_packages=True)
    seen = []
    build = _args(root, state_location=STATE, commands=["dbt build"])
    assert task_runner.dbt(build, _dbt_writing_state(seen, fail_on=["dbt", "build"])) == 1
    retry = _args(root, state_location=STATE, commands=["dbt retry"])
    assert task_runner.dbt(retry, _dbt_writing_state(seen)) == 0
    assert seen == [("deps", None), ("build", None), ("deps", None), ("retry", "build")]
    assert (root / "b/dev/.bundle/demo/dbt-state/run-1/run_results.json").read_text() == "retry"
    assert (root / "b/dev/.bundle/demo/dbt-state/run-1/manifest.json").exists()


def test_a_command_that_writes_no_run_results_drops_the_previous_commands(tmp_path):
    # Outside Cloud Run nothing removes run_results.json between commands.
    root = _deployed(tmp_path)
    seen = []
    task = _args(root, state_location=STATE, commands=["dbt source freshness", "dbt build"])
    assert task_runner.dbt(task, _dbt_writing_state(seen, silent={"build"})) == 0
    assert seen == [("source", None), ("build", None)]
    assert not (root / "b/dev/.bundle/demo/dbt-state/run-1/run_results.json").exists()


def test_a_retry_that_writes_no_run_results_keeps_the_restored_ones(tmp_path):
    root = _deployed(tmp_path)
    task_runner.dbt(_args(root, state_location=STATE, commands=["dbt build"]), _dbt_writing_state([]))
    task_runner.dbt(_args(root, state_location=STATE, commands=["dbt retry"]),
                    _dbt_writing_state([], silent={"retry"}))
    assert (root / "b/dev/.bundle/demo/dbt-state/run-1/run_results.json").read_text() == "build"


def test_without_a_state_location_nothing_is_kept(tmp_path):
    root = _deployed(tmp_path)
    task_runner.dbt(_args(root, commands=["dbt build"]), _dbt_writing_state([]))
    assert not (root / "b/dev/.bundle/demo/dbt-state").exists()


def test_a_task_reads_the_files_of_the_deploy_that_rendered_it(tmp_path, monkeypatch):
    # A later deploy renamed the project's directory: the run started before it still finds its own.
    root = _deployed(tmp_path)
    renamed = tmp_path / "renamed"
    (renamed / "data").mkdir(parents=True)
    (renamed / "data" / "dbt_project.yml").write_text("name: sd\nprofile: demo_app\n")
    files.upload("gs://b/dev/.bundle/demo/files", "v2", files.archive(renamed, ["data/dbt_project.yml"]), root)
    monkeypatch.setenv(task_runner.FILES_VERSION_ENV, "v1")
    assert task_runner.dbt(_args(root), _runner([])) == 0
    assert task_runner.FILES_VERSION_ENV == B.FILES_VERSION_ENV


def test_a_killed_child_exits_as_a_shell_reports_it_and_says_why(capsys):
    assert task_runner._exit_code(-9, "serve.py") == 137
    assert "serve.py was killed by SIGKILL: most likely out of memory" in capsys.readouterr().out
    assert task_runner._exit_code(-15, "dbt build") == 143 and "killed by SIGTERM" in capsys.readouterr().out
    assert task_runner._exit_code(3, "x") == 3 and capsys.readouterr().out == ""

"""mlops-task: what a task's Cloud Run job runs when the platform, not the project's own
code, does the work.

  mlops-task dbt --files URI --project-directory DIR
                 (--profiles-directory DIR | --catalog PROJECT --schema DATASET [--location LOC])
                 [--state-location gs://...]
                 -- "dbt build -s tag:x" ["dbt test" ...]
  mlops-task python --files URI -- path/in/bundle.py [ARG ...]

A dbt_task: fetch the bundle's files its deploy uploaded (MLOPS_FILES_VERSION; else the
last deploy's, as before it was set), run
`dbt deps` when the project has packages, then each command in order, stopping at the
first that fails. Without --profiles-directory it writes a BigQuery profile that
authenticates as the job's service account (method: oauth), as Databricks' dbt_task
generates one for its warehouse. After each command it keeps the command's run_results, slimmed
(save_run_results), where the console's task metrics read each model's BigQuery job.
With --state-location it restores the artifacts `dbt retry` reads into target/ before the
first command and keeps each command's there after it (restore_state, save_state).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from gcpbricks import files

PROFILE_TARGET = "mlops"
#: bundle.FILES_VERSION_ENV (tests hold them equal).
FILES_VERSION_ENV = "MLOPS_FILES_VERSION"
#: What `dbt retry` reads of the previous run, in target/.
STATE_FILES = ("run_results.json", "manifest.json")


def bigquery_profile(project_dir: Path, catalog: str, schema: str,
                     location: str | None = None) -> dict:
    """profiles.yml for the project's own profile name, one BigQuery output."""
    profile = yaml.safe_load((project_dir / "dbt_project.yml").read_text())["profile"]
    output = {"type": "bigquery", "method": "oauth", "project": catalog, "dataset": schema}
    if location:
        # Where dbt creates a dataset it doesn't find: BigQuery's own default is US.
        output["location"] = location
    return {profile: {"target": PROFILE_TARGET, "outputs": {PROFILE_TARGET: output}}}


def slim_run_results(command: str, run_results: dict) -> dict:
    """What the console reads of a command's run_results: each node's status, time and
    BigQuery job, which it looks up for bytes billed, slots and the query plan."""
    def node(r: dict) -> dict:
        ar = r.get("adapter_response") or {}
        execute = next((t for t in r.get("timing") or [] if t.get("name") == "execute"), {})
        return {"unique_id": r.get("unique_id"), "status": r.get("status"),
                "execution_time": r.get("execution_time"), "relation_name": r.get("relation_name"),
                "message": r.get("message"), "started_at": execute.get("started_at"),
                "completed_at": execute.get("completed_at"),
                "adapter_response": {k: ar.get(k) for k in ("job_id", "location", "project_id",
                                                            "bytes_processed", "rows_affected")}}
    return {"command": command, "invocation_id": (run_results.get("metadata") or {}).get("invocation_id"),
            "elapsed_time": run_results.get("elapsed_time"),
            "results": [node(r) for r in run_results.get("results") or []]}


def save_run_results(project_dir: Path, files_uri: str, i: int, command: str,
                     local_root: Path | None = None) -> None:
    """The command's target/run_results.json, slimmed, at runs_uri(...)/dbt-<i>.json; nothing
    outside a Cloud Run execution or when the command wrote none. Never fails the task."""
    execution = os.environ.get("CLOUD_RUN_EXECUTION")
    path = project_dir / "target" / "run_results.json"
    if not execution or not path.exists():
        return
    try:
        slim = slim_run_results(command, json.loads(path.read_text()))
        files.write_json(f"{files.runs_uri(files_uri, execution)}/dbt-{i}.json", slim, local_root)
        path.unlink()
    except Exception as e:  # noqa: BLE001 - metrics are a nicety, the run's outcome is dbt's
        print(f"mlops-task: could not keep {command!r}'s run_results: {type(e).__name__}: {e}", flush=True)


def restore_state(project_dir: Path, state_location: str, local_root: Path | None = None) -> None:
    """The previous task's STATE_FILES into target/, those it kept."""
    for name in STATE_FILES:
        if files.get_file(f"{state_location}/{name}", project_dir / "target" / name, local_root):
            print(f"mlops-task: restored {name} from {state_location}", flush=True)


def save_state(project_dir: Path, state_location: str, local_root: Path | None = None) -> None:
    """The STATE_FILES in target/ at state_location; one that isn't is removed there, so a
    retry never re-runs an earlier command's failures. Never fails the task."""
    for name in STATE_FILES:
        path = project_dir / "target" / name
        try:
            files.put_file(f"{state_location}/{name}", path if path.exists() else None, local_root)
        except Exception as e:  # noqa: BLE001 - the run's outcome is dbt's
            print(f"mlops-task: could not keep {name} at {state_location}: {type(e).__name__}: {e}",
                  flush=True)


def run_dbt(project_dir: Path, profiles_dir: Path, commands: list[str], runner=subprocess.run,
            before=lambda i, command: None, after=lambda i, command: None) -> int:
    """Each command in order between `before(i, command)` and `after(i, command)`, stopping
    at the first that fails. `dbt deps` comes first when the project has packages."""
    env = {**os.environ, "DBT_PROFILES_DIR": str(profiles_dir)}
    steps = list(commands)
    if (project_dir / "packages.yml").exists() or (project_dir / "dependencies.yml").exists():
        steps.insert(0, "dbt deps")
    for i, command in enumerate(steps):
        argv = shlex.split(command)
        if not argv or argv[0] != "dbt":
            print(f"mlops-task: refusing {command!r}: dbt_task commands start with dbt", flush=True)
            return 2
        before(i, command)
        print(f"+ {command}", flush=True)
        code = _exit_code(runner(argv, cwd=project_dir, env=env, check=False).returncode, command)
        after(i, command)
        if code:
            return code
    return 0


def _exit_code(code: int, what: str) -> int:
    """A child's exit code as a shell reports it: a signal is 128 + its number (Python's -9
    would leave as 247), and a SIGKILL is named, since the out-of-memory killer logs nothing."""
    if code >= 0:
        return code
    sig = -code
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = f"signal {sig}"
    hint = ": most likely out of memory; give the task more memory or read less at once" if sig == signal.SIGKILL else ""
    print(f"mlops-task: {what} was killed by {name}{hint}", flush=True)
    return 128 + sig


def _files_version() -> str | None:
    """The deploy whose files this run reads (bundle.files_env), else the current one."""
    return os.environ.get(FILES_VERSION_ENV) or None


def dbt(a: argparse.Namespace, runner=subprocess.run) -> int:
    work = Path(tempfile.mkdtemp(prefix="mlops-dbt-"))
    version = files.fetch(a.files, work, local_root=a.local_root, version=_files_version())
    print(f"mlops-task: bundle files {version}", flush=True)
    project_dir = work / a.project_directory
    if a.profiles_directory is not None:
        profiles_dir = work / a.profiles_directory
    else:
        if not (a.catalog and a.schema):
            sys.exit("mlops-task: give --profiles-directory, or --catalog and --schema")
        profiles_dir = work / ".mlops-profiles"
        profiles_dir.mkdir()
        (profiles_dir / "profiles.yml").write_text(
            yaml.safe_dump(bigquery_profile(project_dir, a.catalog, a.schema, a.location)))
    restored = []

    def before(i: int, command: str) -> None:
        if not a.state_location or command == "dbt deps":
            return
        if restored:
            # Already kept: what target/ holds after this command is then its own.
            for name in STATE_FILES:
                (project_dir / "target" / name).unlink(missing_ok=True)
        else:
            # After `dbt deps`, whose save_run_results would take them as its own.
            restore_state(project_dir, a.state_location, a.local_root)
            restored.append(True)

    def after(i: int, command: str) -> None:
        # Before save_run_results, which removes run_results.json.
        if a.state_location and command != "dbt deps":
            save_state(project_dir, a.state_location, a.local_root)
        save_run_results(project_dir, a.files, i, command, a.local_root)
    return run_dbt(project_dir, profiles_dir, a.commands, runner, before=before, after=after)


def python(a: argparse.Namespace, runner=subprocess.run) -> int:
    """A spark_python_task or served model's bundle file: fetch the deployed files, then run
    the file from the bundle's root with the root on PYTHONPATH, as Databricks runs a
    bundle's python_file. Its exit code is the task's."""
    work = Path(tempfile.mkdtemp(prefix="mlops-python-"))
    version = files.fetch(a.files, work, local_root=a.local_root, version=_files_version())
    print(f"mlops-task: bundle files {version}", flush=True)
    script = work / a.python_file
    if not script.is_file():
        print(f"mlops-task: {a.python_file} is not in the bundle's files", flush=True)
        return 2
    env = {**os.environ, "MLOPS_BUNDLE_VERSION": version,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(work), os.environ.get("PYTHONPATH")]))}
    return _exit_code(runner([sys.executable, str(script), *a.args], cwd=work, env=env, check=False).returncode,
                      a.python_file)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mlops-task", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dbt")
    p.add_argument("--files", required=True, help="gs://.../files: bundle.files_uri")
    p.add_argument("--project-directory", default=".")
    p.add_argument("--profiles-directory")
    p.add_argument("--catalog")
    p.add_argument("--schema")
    p.add_argument("--location", help="BigQuery location of the profile: the bundle's gcp.region")
    p.add_argument("--state-location", help="gs://... where dbt retry's artifacts are kept")
    p.add_argument("--local-root", type=Path, help=argparse.SUPPRESS)
    p.add_argument("commands", nargs="+")
    p = sub.add_parser("python")
    p.add_argument("--files", required=True, help="gs://.../files: bundle.files_uri")
    p.add_argument("--local-root", type=Path, help=argparse.SUPPRESS)
    p.add_argument("python_file")
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    return python(a) if a.cmd == "python" else dbt(a)


if __name__ == "__main__":
    sys.exit(main())

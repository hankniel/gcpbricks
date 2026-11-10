"""Code shipped by the bundle, as Databricks ships it: spark_python_task's and a served
model's python_file run from the deployed files (mlops-task python), not from an image."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gcpbricks import bundle as B
from gcpbricks import files, task_runner

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.4.3"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
RUNTIME = {"job_cluster_key": "runtime", "new_cluster": {
    "docker_image": {"url": "reg/runtime:lock-abc"}, "node_type_id": "e2-standard-8", "num_workers": 2}}
FILES = "gs://b/dev/.bundle/demo/files"


def _load(tmp_path: Path, jobs: dict, target="dev") -> B.Loaded:
    path = tmp_path / "mlops.yaml"
    path.write_text(yaml.safe_dump({**BASE, "resources": {"jobs": jobs}}))
    return B.load(path, target)


def _python_job(python_file: str) -> dict:
    return {"job_clusters": [RUNTIME], "tasks": [{"task_key": "t", "job_cluster_key": "runtime",
            "spark_python_task": {"python_file": python_file, "parameters": ["--x", "1"]}}]}


def test_a_relative_python_file_runs_from_the_bundles_files(tmp_path):
    lb = _load(tmp_path, {"j": _python_job("jobs/score.py")})
    assert B.job_definition(lb, "j")["tasks"]["t"]["args"] == [
        "mlops-task", "python", "--files", FILES, "--", "jobs/score.py", "--x", "1"]
    assert B.needs_files(lb.bundle.resources.jobs["j"])


def test_an_absolute_python_file_is_the_images_own(tmp_path):
    lb = _load(tmp_path, {"j": _python_job("/app/score.py")})
    assert B.job_definition(lb, "j")["tasks"]["t"]["args"] == ["python", "/app/score.py", "--x", "1"]
    assert not B.needs_files(lb.bundle.resources.jobs["j"])


def test_a_served_model_with_a_python_file_gets_a_job_of_its_own_running_it(tmp_path):
    serve = {"job_clusters": [RUNTIME], "tasks": [{"task_key": "serve", "batch_serve_task": {
        "models": [{"model": "roas_d90", "job_cluster_key": "runtime", "python_file": "serving/serve.py"},
                   {"model": "roas_d30", "job_cluster_key": "runtime"}],
        "window_days": 14}}]}
    lb = _load(tmp_path, {"serve": serve})
    plan = B.render(lb)
    own = plan["run_jobs"]["demo-serve-roas-d90-dev"]
    assert own["image"] == "reg/runtime:lock-abc" and own["tasks"] == 2
    assert own["args"] == ["mlops-task", "python", "--files", FILES, "--", "serving/serve.py"]
    assert plan["run_jobs"]["demo-serve-runtime-dev"]["args"] is None
    models = B.job_definition(lb, "serve")["tasks"]["serve"]["argument"]["models"]
    assert [m["job"] for m in models] == ["demo-serve-roas-d90-dev", "demo-serve-runtime-dev"]
    assert B.needs_files(lb.bundle.resources.jobs["serve"])


@pytest.mark.parametrize("model, message", [
    ({"model": "m", "job": "x", "python_file": "s.py"}, "runs on a job_cluster_key"),
    ({"model": "m", "job_cluster_key": "runtime", "python_file": "/app/s.py"}, "path in the bundle"),
])
def test_a_served_python_file_is_a_bundle_file_on_a_cluster(tmp_path, model, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, {"serve": {"job_clusters": [RUNTIME], "tasks": [{"task_key": "s", "batch_serve_task": {
            "models": [model], "window_days": 1}}]}})


def test_mlops_task_python_runs_the_file_from_the_deployed_root_with_it_importable(tmp_path):
    project = tmp_path / "project"
    (project / "serving").mkdir(parents=True)
    (project / "serving" / "__init__.py").write_text("")
    (project / "serving" / "helper.py").write_text("VALUE = 'from the bundle'\n")
    (project / "serving" / "serve.py").write_text(
        "import os, sys\nfrom serving.helper import VALUE\n"
        "open(os.environ['OUT'], 'w').write(VALUE + ' ' + ' '.join(sys.argv[1:]) + ' ' + os.environ['MLOPS_BUNDLE_VERSION'])\n")
    paths = [str(p.relative_to(project)) for p in project.rglob("*") if p.is_file()]
    root = tmp_path / "gcs"
    files.upload(FILES, "v7", files.archive(project, paths), root)
    out = tmp_path / "out.txt"
    import os
    os.environ["OUT"] = str(out)
    try:
        code = task_runner.main(["python", "--files", FILES, "--local-root", str(root), "--",
                                 "serving/serve.py", "--day", "2026-10-16"])
    finally:
        del os.environ["OUT"]
    assert code == 0
    assert out.read_text() == "from the bundle --day 2026-10-16 v7"


def test_mlops_task_python_refuses_a_file_the_bundle_lacks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("")
    root = tmp_path / "gcs"
    files.upload(FILES, "v1", files.archive(project, ["a.py"]), root)
    assert task_runner.main(["python", "--files", FILES, "--local-root", str(root), "--", "nope.py"]) == 2

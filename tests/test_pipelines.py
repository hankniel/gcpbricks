"""A vertex_pipeline_task's python_file: compiled with its steps' bundle env, published with
parameters the runner reads, and use_bundle putting a deploy's files on sys.path."""
from __future__ import annotations

import json
import sys
import textwrap

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import bundle as B
from gcpbricks import cli, pipelines

pytest.importorskip("kfp")

IMAGE = "us-central1-docker.pkg.dev/p/mlops/demo-training:abc"
BASE = {"bundle": {"name": "demo", "mlops_version": "0.5.0"},
        "gcp": {"project": "p", "bucket": "b"},
        "run_as": {"service_principal_name": "sa@p.iam.gserviceaccount.com"},
        "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}}}
PIPELINE = textwrap.dedent('''
    from kfp import dsl
    from gcpbricks import pipelines

    @dsl.component(base_image=pipelines.image())
    def train(model: str) -> str:
        return model

    @dsl.component(base_image="python:3.11-slim")
    def other() -> str:
        return "x"

    @dsl.pipeline(name="train")
    def pipeline(model: str = "m"):
        train(model=model)
        other()
''')


def _bundle(tmp_path, task=None) -> B.Loaded:
    (tmp_path / "training").mkdir(exist_ok=True)
    (tmp_path / "training" / "pipeline.py").write_text(PIPELINE)
    task = task or {"python_file": "training/pipeline.py", "base_parameters": {"model": "forecast_30d"}}
    raw = {**BASE, "resources": {"jobs": {"retrain": {"tasks": [
        {"task_key": "forecast_30d", "vertex_pipeline_task": task}]}}}}
    (tmp_path / "mlops.yaml").write_text(yaml.safe_dump(raw))
    return B.load(tmp_path / "mlops.yaml", "dev")


def test_compile_sets_the_bundle_env_on_the_environments_steps_only(tmp_path, monkeypatch):
    _bundle(tmp_path)
    monkeypatch.setenv("MLOPS_PIPELINE_IMAGE", IMAGE)
    monkeypatch.setenv("MLOPS_BUNDLE_FILES", "gs://b/dev/.bundle/demo/files")
    monkeypatch.setenv("MLOPS_BUNDLE_VERSION", "abc123")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("MLFLOW_ARTIFACT_ROOT", raising=False)
    spec = pipelines.compile_pipeline(tmp_path, "training/pipeline.py", "pipeline", tmp_path / "out.json")
    executors = spec["deploymentSpec"]["executors"]
    env = {c["container"]["image"]: {e["name"]: e["value"] for e in c["container"].get("env", [])}
           for c in executors.values()}
    # Not MLOPS_BUNDLE_VERSION: which deploy is the run's label, outside a step's cache key.
    assert env[IMAGE] == {"MLOPS_BUNDLE_FILES": "gs://b/dev/.bundle/demo/files"}
    assert env["python:3.11-slim"] == {}
    sys.path.remove(str(tmp_path))


NESTED = textwrap.dedent('''
    from kfp import dsl
    from gcpbricks import pipelines

    @dsl.component(base_image=pipelines.image())
    def shard(i: int) -> int:
        return i

    @dsl.component(base_image=pipelines.image())
    def gate(model: str) -> str:
        return model

    @dsl.pipeline(name="fanout")
    def pipeline(model: str = "m"):
        with dsl.ParallelFor(items=[0, 1, 2], parallelism=3) as i:
            shard(i=i)
        with dsl.If(model == "m"):
            gate(model=model)
''')


def test_steps_inside_a_parallel_for_or_condition_get_the_bundle_env(tmp_path, monkeypatch):
    """KFP lists every executor in deploymentSpec, nested sub-DAGs' included, so a fanned-out
    step reads the bundle's code as a top-level one does."""
    _bundle(tmp_path)
    (tmp_path / "training" / "pipeline.py").write_text(NESTED)
    monkeypatch.setenv("MLOPS_PIPELINE_IMAGE", IMAGE)
    monkeypatch.setenv("MLOPS_BUNDLE_FILES", "gs://b/dev/.bundle/demo/files")
    monkeypatch.setenv("MLOPS_BUNDLE_VERSION", "abc123")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("MLFLOW_ARTIFACT_ROOT", raising=False)
    sys.modules.pop("training.pipeline", None)
    spec = pipelines.compile_pipeline(tmp_path, "training/pipeline.py", "pipeline", tmp_path / "out.json")
    nested = {t for c in spec["components"].values() if "dag" in c for t in c["dag"]["tasks"]}
    assert {"shard", "gate"} <= nested and not {"shard", "gate"} & set(spec["root"]["dag"]["tasks"])
    executors = spec["deploymentSpec"]["executors"]
    assert {k for k in executors} == {"exec-shard", "exec-gate"}
    for c in executors.values():
        assert {e["name"]: e["value"] for e in c["container"]["env"]} == {
            "MLOPS_BUNDLE_FILES": "gs://b/dev/.bundle/demo/files"}
    sys.path.remove(str(tmp_path))


def test_a_step_reads_the_deploy_its_run_is_labelled_with(tmp_path, monkeypatch):
    """Vertex hands every step its run's labels; the deploy env, where set, still wins."""
    got = []
    monkeypatch.setattr("gcpbricks.files.fetch", lambda uri, dest, version=None: got.append(version) or version)
    monkeypatch.setattr(pipelines.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MLOPS_BUNDLE_FILES", "gs://b/dev/.bundle/demo/files")
    monkeypatch.delenv("MLOPS_BUNDLE_VERSION", raising=False)
    monkeypatch.setenv("VERTEX_AI_PIPELINES_RUN_LABELS", json.dumps({"mlops_bundle_version": "v7", "mlops_job": "j"}))
    root = pipelines.use_bundle()
    assert got == ["v7"] and root.endswith("mlops-bundle/v7")
    monkeypatch.setenv("MLOPS_BUNDLE_VERSION", "v8")
    pipelines.use_bundle()
    assert got == ["v7", "v8"]
    monkeypatch.delenv("MLOPS_BUNDLE_VERSION")
    monkeypatch.setenv("VERTEX_AI_PIPELINES_RUN_LABELS", "not json")
    assert pipelines.run_label("mlops_bundle_version") is None
    for p in list(sys.path):
        if p.startswith(str(tmp_path)):
            sys.path.remove(p)


def test_the_runner_labels_a_run_with_the_deploy_its_steps_read():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "terraform/modules/bundle/workflows/vertex_pipeline.yaml").read_text()
    assert f'run_labels["{pipelines.VERSION_LABEL}"]: ${{text.substring(default(map.get(current, "bundle_version"), current.version), 0, 63)}}' in text


def test_a_pipeline_needs_its_image_to_compile(tmp_path, monkeypatch):
    _bundle(tmp_path)
    monkeypatch.delenv("MLOPS_PIPELINE_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="MLOPS_PIPELINE_IMAGE"):
        pipelines.compile_pipeline(tmp_path, "training/pipeline.py", "pipeline", tmp_path / "out.json")
    sys.path.remove(str(tmp_path))


def test_the_published_parameters_carry_the_run_settings(tmp_path):
    lb = _bundle(tmp_path)
    task = lb.bundle.resources.jobs["retrain"].tasks[0]
    assert B.pipeline_params(lb, "retrain", task) == {
        "display_name": "demo-retrain-forecast-30d",
        "labels": {"mlops_bundle": "demo", "mlops_target": "dev", "mlops_task": "forecast-30d"},
        "pipeline_root": "gs://b/dev/.bundle/demo/pipeline_root/retrain-forecast_30d",
        "service_account": "sa@p.iam.gserviceaccount.com",
        "parameter_values": {"model": "forecast_30d"}}
    arg = B.job_definition(lb, "retrain")["tasks"]["forecast_30d"]["argument"]
    assert arg["spec_prefix"] == "dev/.bundle/demo/pipelines/retrain-forecast_30d"
    assert B.needs_files(lb.bundle.resources.jobs["retrain"])


def test_deploy_compiles_and_publishes_a_python_file_pipeline(tmp_path, monkeypatch):
    lb = _bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    task = lb.bundle.resources.jobs["retrain"].tasks[0]
    assert cli._publish_pipeline(lb, "retrain", task, IMAGE, "v1", out) == 0
    base = out / "dev/.bundle/demo/pipelines/retrain-forecast_30d"
    current = json.loads((base / "current.json").read_text())
    assert current["image"] == IMAGE and set(current) == {"version", "bundle_version", "image", "train"}
    assert current["bundle_version"] == "v1"
    params = json.loads((base / "v1" / "train_params.json").read_text())
    assert params["parameter_values"] == {"model": "forecast_30d"}
    spec = json.loads((base / "v1" / "train_pipeline.json").read_text())
    assert spec["pipelineInfo"]["name"] == "train"


@pytest.mark.parametrize("task, message", [
    ({"python_file": "training/pipeline.py", "build": "x", "spec_prefix": "p"}, "exactly one"),
    ({"build": "x"}, "needs spec_prefix"),
    ({"python_file": "/abs/pipeline.py"}, "path in the bundle"),
    ({"build": "x", "spec_prefix": "p", "base_parameters": {"a": 1}}, "base_parameters go with python_file"),
])
def test_invalid_pipeline_tasks_are_refused(tmp_path, task, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        _bundle(tmp_path, task)


def test_use_bundle_fetches_the_pinned_deploy_once_and_puts_it_first(tmp_path, monkeypatch):
    fetched = []

    def fetch(uri, dest, local_root=None, version=None):
        fetched.append((uri, version))
        (dest / "training").mkdir(parents=True)
        (dest / "training" / "__init__.py").write_text("")
        return version

    monkeypatch.setattr("gcpbricks.files.fetch", fetch)
    monkeypatch.setattr(pipelines.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MLOPS_BUNDLE_FILES", "gs://b/files")
    monkeypatch.setenv("MLOPS_BUNDLE_VERSION", "v7")
    monkeypatch.setenv("PYTHONPATH", "")
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = pipelines.use_bundle()
    assert pipelines.use_bundle() == root and fetched == [("gs://b/files", "v7")]
    assert sys.path[0] == root and root.endswith("mlops-bundle/v7")
    assert root in pipelines.os.environ["PYTHONPATH"].split(pipelines.os.pathsep)

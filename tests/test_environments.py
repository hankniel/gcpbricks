"""Job environments: the schema, the build context and its content tag, the image a cluster
or a vertex_pipeline_task gets, and deploy's build-if-missing."""
from __future__ import annotations

import threading

import pytest
import yaml
from pydantic import ValidationError

from gcpbricks import bundle as B
from gcpbricks import environments as E

BASE = {
    "bundle": {"name": "demo", "mlops_version": "0.5.0"},
    "gcp": {"project": "p", "bucket": "b"},
    "run_as": {"service_principal_name": "s"},
    "targets": {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}},
}
ENVS = [{"environment_key": "serving", "spec": {"dependencies": ["-r requirements/serving.txt", "rich==13"]}},
        {"environment_key": "training", "spec": {"dependencies": ["optuna==4"]}}]


def _job(**over):
    job = {"environments": ENVS,
           "job_clusters": [{"job_cluster_key": "big", "new_cluster": {
               "environment_key": "serving", "cpu": 8, "memory": "32Gi"}}],
           "tasks": [{"task_key": "score", "job_cluster_key": "big",
                      "spark_python_task": {"python_file": "score.py"}},
                     {"task_key": "train", "environment_key": "training",
                      "vertex_pipeline_task": {"spec_prefix": "m/spec", "build": "build {out_dir} {image}"}}]}
    return {**job, **over}


def _load(tmp_path, job=None, gcp=None) -> B.Loaded:
    (tmp_path / "requirements").mkdir(exist_ok=True)
    (tmp_path / "requirements" / "serving.txt").write_text("-c base.txt\npandas==2.3.3\n")
    (tmp_path / "requirements" / "base.txt").write_text("numpy==2.4.6\n")
    raw = {**BASE, "gcp": {**BASE["gcp"], **(gcp or {})}, "resources": {"jobs": {"j": job or _job()}}}
    (tmp_path / "mlops.yaml").write_text(yaml.safe_dump(raw))
    return B.load(tmp_path / "mlops.yaml", "dev")


def _env(lb, key):
    return next(e for e in lb.bundle.resources.jobs["j"].environments if e.environment_key == key)


def test_the_context_installs_the_lines_on_the_runtime_with_every_file_they_name(tmp_path):
    ctx = E.context(lb := _load(tmp_path), _env(lb, "serving"))
    assert sorted(ctx) == ["Dockerfile", "files/requirements/base.txt", "files/requirements/serving.txt",
                           "requirements.txt"]
    assert ctx["requirements.txt"] == b"-r files/requirements/serving.txt\nrich==13\n"
    assert ctx["Dockerfile"].startswith(
        b"FROM us-central1-docker.pkg.dev/my-gcp-project/mlops/runtime:0.5.0\n")


def test_a_cluster_and_a_pipeline_task_run_their_environments_image(tmp_path):
    lb = _load(tmp_path)
    serving = E.image(lb, _env(lb, "serving"))
    assert serving.startswith("us-central1-docker.pkg.dev/p/mlops/demo-serving:")
    assert B.render(lb)["run_jobs"]["demo-j-big-dev"]["image"] == serving
    assert B.environment_image(lb, "j", "training") == E.image(lb, _env(lb, "training"))
    assert sorted(E.used(lb)) == sorted([serving, E.image(lb, _env(lb, "training"))])


def test_the_tag_moves_only_with_what_the_environment_installs(tmp_path):
    lb = _load(tmp_path)
    before = E.image(lb, _env(lb, "serving"))
    assert E.image(_load(tmp_path), _env(lb, "serving")) == before
    (tmp_path / "requirements" / "base.txt").write_text("numpy==2.4.7\n")
    assert E.image(lb, _env(lb, "serving")) != before
    newer = _load(tmp_path)
    newer.bundle.bundle.mlops_version = "0.5.1"
    assert E.image(newer, _env(newer, "serving")).rsplit(":", 1)[1] != before.rsplit(":", 1)[1]


def test_the_repository_follows_gcp_image_repository(tmp_path):
    lb = _load(tmp_path, gcp={"image_repository": "europe-docker.pkg.dev/q/envs"})
    assert E.image(lb, _env(lb, "training")).startswith("europe-docker.pkg.dev/q/envs/demo-training:")


@pytest.mark.parametrize("job, message", [
    (_job(job_clusters=[{"job_cluster_key": "big", "new_cluster": {
        "environment_key": "serving", "docker_image": {"url": "x"}, "cpu": 8, "memory": "32Gi"}}]),
     "docker_image or environment_key"),
    (_job(job_clusters=[{"job_cluster_key": "big", "new_cluster": {"environment_key": "nope", "cpu": 8,
                                                                  "memory": "32Gi"}}]),
     "no environment_key 'nope'"),
    (_job(environments=[*ENVS, ENVS[0]]), "environment_key repeated"),
    (_job(tasks=[{"task_key": "score", "environment_key": "serving",
                  "spark_python_task": {"python_file": "score.py"}}]), "applies to a vertex_pipeline_task"),
])
def test_invalid_environment_use_is_refused(tmp_path, job, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        _load(tmp_path, job)


def test_a_dependency_file_must_be_in_the_bundle(tmp_path):
    lb = _load(tmp_path, _job(environments=[{"environment_key": "serving",
                                             "spec": {"dependencies": ["-r missing.txt"]}}, ENVS[1]]))
    with pytest.raises(ValueError, match="names no file"):
        E.context(lb, _env(lb, "serving"))


def test_a_dry_run_writes_each_context_instead_of_building(tmp_path):
    lb = _load(tmp_path)
    out = tmp_path / "out"
    E.ensure(lb, out, log=lambda _: None)
    assert (out / "environments" / "serving" / "files" / "requirements" / "serving.txt").is_file()
    assert (out / "environments" / "training" / "Dockerfile").is_file()


class FakeSession:
    def __init__(self, existing):
        self.existing, self.built = existing, []

    def head(self, url, headers):
        return type("R", (), {"status_code": 200 if any(url.endswith(e) for e in self.existing) else 404})()


def test_deploy_builds_only_the_tags_the_registry_lacks(tmp_path, monkeypatch):
    lb = _load(tmp_path)
    serving, training = E.image(lb, _env(lb, "serving")), E.image(lb, _env(lb, "training"))
    built = []
    monkeypatch.setattr(E, "build", lambda session, lb, ref, ctx, log=print: built.append(ref))
    E.ensure(lb, None, FakeSession([f"manifests/{serving.rsplit(':', 1)[1]}"]), log=lambda _: None)
    assert built == [training]


def test_missing_environments_build_side_by_side(tmp_path, monkeypatch):
    lb = _load(tmp_path)
    started, both = [], threading.Barrier(2, timeout=5)

    def build(session, lb, ref, ctx, log=print):
        started.append(ref)
        both.wait()  # breaks, failing the deploy, unless the other build runs at the same time
    monkeypatch.setattr(E, "build", build)
    E.ensure(lb, None, FakeSession([]), log=lambda _: None)
    assert sorted(started) == sorted([E.image(lb, _env(lb, "serving")), E.image(lb, _env(lb, "training"))])

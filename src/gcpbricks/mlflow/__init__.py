"""MLflow's tracking and registry API, the subset Databricks code uses, over Vertex AI: so a
project's code reads as it would on Databricks, and moving to real MLflow is an import.

    from gcpbricks import mlflow

    mlflow.set_experiment("roas-d30")
    with mlflow.start_run(run_name="v42"):
        mlflow.log_params({...}); mlflow.log_metrics({...})
        with mlflow.start_run(run_name="v42-trial-0", nested=True):   # a child run
            mlflow.log_metric("r2", 0.4)
        info = mlflow.pyfunc.log_model("model", python_model=MyModel(),
                                       artifacts={"estimator": "model.pkl"},
                                       registered_model_name="roas_d30")
    client = mlflow.MlflowClient()
    client.set_registered_model_alias("roas_d30", "champion", info.registered_model_version)
    run = client.get_run(client.get_model_version_by_alias("roas_d30", "champion").run_id)
    model = mlflow.pyfunc.load_model("models:/roas_d30@champion")
    result = mlflow.evaluate(data=df, predictions="pred", targets="y", model_type="regressor")
    mlflow.validate_evaluation_results({"r2_score": mlflow.models.MetricThreshold(
        threshold=0.3, greater_is_better=True)}, result)   # see _evaluation.py

Where things live:
  tracking       Vertex AI Experiments: an experiment, its runs' params and metrics. A run's
                 tags are logged as params named tag_<key> (Vertex runs have no tags). A
                 nested run is MLflow's: a run of the same experiment tagged
                 mlflow.parentRunId; Vertex knows nothing of the nesting.
  registry       Vertex Model Registry: a registered model is a Vertex model by display name,
                 a version its version, an alias its version alias.
  artifacts      MLFLOW_ARTIFACT_ROOT (gs://...): <root>/<experiment>/<run_id>/artifacts/<path>,
                 each model in MLflow's own MLmodel layout.

Unlike MLflow, a version's tags are its Vertex labels, so their keys and values follow GCP's
label rules (lowercase letters, digits, _ and -, at most 63 characters); others are refused.
Configuration: MLFLOW_TRACKING_URI=vertex://<project>/<location> (or set_tracking_uri()),
MLFLOW_ARTIFACT_ROOT.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Self

from . import _storage

_LABEL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_LABEL_VALUE = re.compile(r"^[a-z0-9_-]{0,63}$")
#: Version labels the SDK itself writes.
RUN_ID_LABEL = "mlflow_run_id"
EXPERIMENT_LABEL = "mlflow_experiment"
GIT_COMMIT_LABEL = "git_commit"
#: MLflow's own tag on a nested run, naming its parent.
PARENT_RUN_TAG = "mlflow.parentRunId"

_tracking_uri: str | None = None
_experiment: str | None = None
_runs: list[Run] = []
_backends: dict[str, object] = {}


# --- configuration ------------------------------------------------------------------------

def set_tracking_uri(uri: str) -> None:
    global _tracking_uri
    _tracking_uri = uri


def get_tracking_uri() -> str:
    uri = _tracking_uri or os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        raise RuntimeError("no tracking URI: set MLFLOW_TRACKING_URI=vertex://<project>/<location> "
                           "or call mlflow.set_tracking_uri()")
    return uri


def _backend():
    uri = get_tracking_uri()
    if uri not in _backends:
        m = re.fullmatch(r"vertex://([^/]+)/([^/]+)/?", uri)
        if not m:
            raise ValueError(f"tracking URI {uri!r} is not vertex://<project>/<location>")
        from ._vertex import VertexBackend
        _backends[uri] = VertexBackend(m.group(1), m.group(2))
    return _backends[uri]


def _artifact_root() -> str:
    root = os.environ.get("MLFLOW_ARTIFACT_ROOT")
    if not root:
        raise RuntimeError("no artifact root: set MLFLOW_ARTIFACT_ROOT=gs://<bucket>/<prefix>")
    return root


def _check_tags(tags: dict) -> dict[str, str]:
    out = {}
    for k, v in tags.items():
        v = "" if v is None else str(v)
        if not _LABEL_KEY.match(k) or not _LABEL_VALUE.match(v):
            raise ValueError(f"tag {k}={v!r}: a version's tags are Vertex labels: keys and values "
                             "of lowercase letters, digits, _ and - (a key starts with a letter), "
                             "at most 63 characters")
        out[k] = v
    return out


def _git_commit() -> str | None:
    sha = os.environ.get("CI_COMMIT_SHA") or os.environ.get("GIT_COMMIT")
    if sha:
        return sha.lower()
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


# --- tracking -----------------------------------------------------------------------------

@dataclass
class RunInfo:
    run_id: str
    run_name: str
    experiment_id: str
    artifact_uri: str
    #: FINISHED or FAILED once logged; "" while it runs.
    status: str = ""


@dataclass
class RunData:
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)


@dataclass
class Run:
    info: RunInfo
    data: RunData

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, *_):
        end_run("FAILED" if exc_type else "FINISHED")


def set_experiment(experiment_name: str) -> None:
    global _experiment
    _experiment = experiment_name


def _run_id(name: str) -> str:
    """A Vertex run id: lowercase [a-z0-9-], starting with a letter."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not s or not s[0].isalpha():
        s = f"r-{s}"
    return s[:63].rstrip("-")


def start_run(run_name: str | None = None, nested: bool = False) -> Run:
    """nested: the run is a child of the active run, as MLflow's start_run(nested=True)."""
    if _experiment is None:
        raise RuntimeError("no experiment: call mlflow.set_experiment() first")
    parent = active_run() if nested else None
    run_name = run_name or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_id = _run_id(run_name)
    run = Run(RunInfo(run_id, run_name, _experiment,
                      _storage.join(_artifact_root(), _experiment, run_id, "artifacts")), RunData())
    commit = _git_commit()
    if commit:
        run.data.tags["mlflow.source.git.commit"] = commit
    if parent:
        run.data.tags[PARENT_RUN_TAG] = parent.info.run_id
    _runs.append(run)
    return run


def active_run() -> Run | None:
    return _runs[-1] if _runs else None


def _require_run() -> Run:
    return active_run() or start_run()


def end_run(status: str = "FINISHED") -> None:
    """Logs the run to Vertex Experiments, even a failed one, as MLflow keeps a failed run."""
    if not _runs:
        return
    run = _runs.pop()
    params = {**run.data.params, **{f"tag_{k}": v for k, v in run.data.tags.items()}, "status": status}
    _backend().log_run(run.info.experiment_id, run.info.run_id, params, run.data.metrics)


def log_param(key: str, value) -> None:
    _require_run().data.params[key] = value


def log_params(params: dict) -> None:
    _require_run().data.params.update({k: v for k, v in params.items() if v is not None})


def log_metric(key: str, value: float) -> None:
    _require_run().data.metrics[key] = float(value)


def log_metrics(metrics: dict) -> None:
    _require_run().data.metrics.update(
        {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})


def set_tag(key: str, value) -> None:
    _require_run().data.tags[key] = value


def set_tags(tags: dict) -> None:
    _require_run().data.tags.update(tags)


# --- registry -----------------------------------------------------------------------------

@dataclass
class ModelVersion:
    name: str
    version: str
    source: str
    run_id: str | None
    tags: dict
    aliases: list[str]
    description: str
    creation_timestamp: int


@dataclass
class RegisteredModel:
    name: str
    description: str
    #: alias -> version, as MLflow's RegisteredModel.aliases.
    aliases: dict


def _mv(name: str, v: dict) -> ModelVersion:
    return ModelVersion(name=name, version=v["version"], source=v["uri"],
                        run_id=v["labels"].get(RUN_ID_LABEL), tags=v["labels"], aliases=v["aliases"],
                        description=v["description"], creation_timestamp=v["created_ms"])


class MlflowException(Exception):
    pass


class MlflowClient:
    def __init__(self, tracking_uri: str | None = None, registry_uri: str | None = None):
        if tracking_uri or registry_uri:
            set_tracking_uri(registry_uri or tracking_uri)

    def create_model_version(self, name: str, source: str, run_id: str | None = None,
                             tags: dict | None = None, description: str | None = None) -> ModelVersion:
        labels = _check_tags(tags or {})
        if run_id:
            labels[RUN_ID_LABEL] = _check_tags({RUN_ID_LABEL: run_id})[RUN_ID_LABEL]
            # A run id is unique only within its experiment; the console finds the run by both.
            if _experiment:
                labels[EXPERIMENT_LABEL] = _check_tags({EXPERIMENT_LABEL: _experiment})[EXPERIMENT_LABEL]
        commit = _git_commit()
        if commit and GIT_COMMIT_LABEL not in labels:
            labels[GIT_COMMIT_LABEL] = commit
        return _mv(name, _backend().upload_version(name, source, labels, description or ""))

    def get_run(self, run_id: str, experiment: str | None = None) -> Run:
        """A logged run, its tags split back out of its params. Vertex run ids are unique per
        experiment, not globally as MLflow's: `experiment` defaults to the active one."""
        experiment = experiment or _experiment
        if experiment is None:
            raise RuntimeError("get_run reads a run of an experiment: pass experiment= or call set_experiment()")
        got = _backend().get_run(experiment, run_id)
        if got is None:
            raise MlflowException(f"run {run_id!r} not found in experiment {experiment!r}")
        params = dict(got["params"])
        status = str(params.pop("status", ""))
        tags = {k.removeprefix("tag_"): v for k, v in params.items() if k.startswith("tag_")}
        params = {k: v for k, v in params.items() if not k.startswith("tag_")}
        return Run(RunInfo(run_id, run_id, experiment,
                           _storage.join(_artifact_root(), experiment, run_id, "artifacts"), status),
                   RunData(params=params, metrics=dict(got["metrics"]), tags=tags))

    def get_registered_model(self, name: str) -> RegisteredModel:
        model = _backend().model(name)
        if model is None:
            raise MlflowException(f"registered model {name!r} not found")
        aliases = {a: v["version"] for v in _backend().versions(name) for a in v["aliases"]}
        return RegisteredModel(name=name, description=model["description"], aliases=aliases)

    def get_model_version(self, name: str, version: str | int) -> ModelVersion:
        v = _backend().version(name, str(version))
        if v is None:
            raise MlflowException(f"model version {name}/{version} not found")
        return _mv(name, v)

    def get_model_version_by_alias(self, name: str, alias: str) -> ModelVersion:
        for v in _backend().versions(name):
            if alias in v["aliases"]:
                return _mv(name, v)
        raise MlflowException(f"registered model {name!r} has no alias {alias!r}")

    def set_registered_model_alias(self, name: str, alias: str, version: str | int) -> None:
        """Moves the alias: an alias names exactly one version."""
        version = str(version)
        for v in _backend().versions(name):
            if alias in v["aliases"] and v["version"] != version:
                _backend().remove_alias(name, v["version"], alias)
        _backend().add_alias(name, version, alias)

    def delete_registered_model_alias(self, name: str, alias: str) -> None:
        mv = self.get_model_version_by_alias(name, alias)
        _backend().remove_alias(name, mv.version, alias)

    def set_model_version_tag(self, name: str, version: str | int, key: str, value) -> None:
        mv = self.get_model_version(name, version)
        _backend().set_labels(name, mv.version, {**mv.tags, **_check_tags({key: value})})

    def delete_model_version_tag(self, name: str, version: str | int, key: str) -> None:
        mv = self.get_model_version(name, version)
        _backend().set_labels(name, mv.version, {k: v for k, v in mv.tags.items() if k != key})

    def search_model_versions(self, filter_string: str) -> list[ModelVersion]:
        """`name='<model>'`, optionally AND-ed with `tags.<key>='<value>'` terms."""
        terms = [t.strip() for t in re.split(r"\s+and\s+", filter_string.strip(), flags=re.IGNORECASE) if t.strip()]
        name, tags = None, {}
        for t in terms:
            m = re.fullmatch(r"(name|tags\.([\w-]+))\s*=\s*['\"]([^'\"]*)['\"]", t)
            if not m:
                raise MlflowException(f"filter term {t!r}: only name='...' and tags.<key>='...' "
                                      "joined by AND are supported")
            if m.group(1) == "name":
                name = m.group(3)
            else:
                tags[m.group(2)] = m.group(3)
        if name is None:
            raise MlflowException("search_model_versions needs name='<registered model>'")
        found = [_mv(name, v) for v in _backend().versions(name, _check_tags(tags))]
        return sorted(found, key=lambda mv: int(mv.version), reverse=True)


def get_run(run_id: str) -> Run:
    """A run of the active experiment, as MlflowClient().get_run."""
    return MlflowClient().get_run(run_id)


def register_model(model_uri: str, name: str, tags: dict | None = None) -> ModelVersion:
    """model_uri: runs:/<run_id>/<path> (of the active experiment) or a gs:// directory."""
    source, run_id = model_uri, None
    m = re.fullmatch(r"runs:/([^/]+)/(.+)", model_uri)
    if m:
        if _experiment is None:
            raise RuntimeError("runs:/ URIs resolve under the experiment: call set_experiment() first")
        run_id = m.group(1)
        source = _storage.join(_artifact_root(), _experiment, run_id, "artifacts", m.group(2))
    return MlflowClient().create_model_version(name, source, run_id=run_id, tags=tags)


def _resolve(model_uri: str) -> str:
    """A models:/ or runs:/ URI as the model directory it names; anything else as is."""
    m = re.fullmatch(r"models:/([^/@]+)(?:/(\w+)|@([\w-]+))", model_uri)
    if m:
        client = MlflowClient()
        mv = (client.get_model_version_by_alias(m.group(1), m.group(3)) if m.group(3)
              else client.get_model_version(m.group(1), m.group(2)))
        return mv.source
    m = re.fullmatch(r"runs:/([^/]+)/(.+)", model_uri)
    if m:
        if _experiment is None:
            raise RuntimeError("runs:/ URIs resolve under the experiment: call set_experiment() first")
        return _storage.join(_artifact_root(), _experiment, m.group(1), "artifacts", m.group(2))
    return model_uri


from . import metrics, models, pyfunc  # pyfunc reads the tracking state above
from ._evaluation import evaluate, validate_evaluation_results
from .features import log_features
from .training import log_training
from .training import summarize as training_summary

__all__ = [
    "MlflowClient",
    "MlflowException",
    "ModelVersion",
    "RegisteredModel",
    "Run",
    "active_run",
    "end_run",
    "evaluate",
    "get_run",
    "get_tracking_uri",
    "log_features",
    "log_metric",
    "log_metrics",
    "log_param",
    "log_params",
    "log_training",
    "metrics",
    "models",
    "pyfunc",
    "register_model",
    "set_experiment",
    "set_tag",
    "set_tags",
    "set_tracking_uri",
    "start_run",
    "training_summary",
    "validate_evaluation_results",
]

"""mlflow.pyfunc: a model as a PythonModel plus its artifacts, saved in MLflow's own MLmodel
layout (so real MLflow reads it too):

    <model dir>/MLmodel              flavors.python_function: python_model, artifacts
    <model dir>/python_model.pkl     the PythonModel, cloudpickled
    <model dir>/artifacts/<file>     each artifact, as context.artifacts[name] on load
    <model dir>/feature_spec.json    optional: the features the model was trained on, as
                                     Databricks Feature Engineering's log_model stores them

MLmodel's `signature` is mlflow.models' (models.py): predict() checks a DataFrame against it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import _storage
from . import features as FT
from . import training as TR
from .models import ModelSignature, check_inputs

MLMODEL = "MLmodel"
PYTHON_MODEL = "python_model.pkl"


class PythonModelContext:
    def __init__(self, artifacts: dict[str, str]):
        self.artifacts = artifacts


class PythonModel:
    """Subclass it: load_context() once on load, predict() per call."""

    def load_context(self, context: PythonModelContext) -> None:
        pass

    def predict(self, context: PythonModelContext, model_input, params: dict | None = None):
        raise NotImplementedError


@dataclass
class ModelMetadata:
    run_id: str | None
    artifact_path: str | None
    metadata: dict = field(default_factory=dict)
    signature: ModelSignature | None = None
    #: gcpbricks.features.FeatureService, when the model was saved with one.
    feature_spec: object | None = None

    def get_input_schema(self):
        return self.signature.inputs if self.signature else None


class PyFuncModel:
    def __init__(self, python_model: PythonModel, context: PythonModelContext, metadata: ModelMetadata):
        self._model, self._context, self.metadata = python_model, context, metadata

    def predict(self, data, params: dict | None = None):
        check_inputs(self.metadata.signature, data)
        return self._model.predict(self._context, data, params=params)

    def unwrap_python_model(self) -> PythonModel:
        return self._model


@dataclass
class ModelInfo:
    artifact_path: str
    model_uri: str
    run_id: str
    registered_model_version: str | None
    metadata: dict


def save_model(path: str | Path, python_model: PythonModel, artifacts: dict[str, str] | None = None,
               metadata: dict | None = None, pip_requirements: list[str] | None = None,
               run_id: str | None = None, artifact_path: str | None = None,
               signature: ModelSignature | None = None, feature_spec=None, features=None,
               training: dict | None = None) -> dict | None:
    """feature_spec: a gcpbricks.features.FeatureService, saved as feature_spec.json.
    features: the model's feature_manager FeaturePipeline. With either, or a signature, the
    model gets a features.json (mlflow/features.py: what it reads, what it is fit on), which
    is returned. training: mlflow.training_summary()'s, saved as training_summary.json."""
    import cloudpickle

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    stored = {}
    for name, src in (artifacts or {}).items():
        src_path = Path(src)
        dest = path / "artifacts" / src_path.name
        dest.parent.mkdir(exist_ok=True)
        (shutil.copytree if src_path.is_dir() else shutil.copy2)(src_path, dest)
        stored[name] = {"path": f"artifacts/{src_path.name}"}
    (path / PYTHON_MODEL).write_bytes(cloudpickle.dumps(python_model))
    if pip_requirements:
        (path / "requirements.txt").write_text("\n".join(pip_requirements) + "\n")
    if feature_spec is not None:
        feature_spec.save(path)
    described = FT.describe(features, signature=signature, feature_spec=feature_spec)
    if described:
        FT.save(described, path)
    if training:
        TR.save(training, path)
    mlmodel = {
        "artifact_path": artifact_path,
        "run_id": run_id,
        "utc_time_created": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "metadata": metadata or None,
        "flavors": {"python_function": {
            "loader_module": "mlflow.pyfunc.model",
            "python_model": PYTHON_MODEL,
            "artifacts": stored,
            "python_version": ".".join(map(str, sys.version_info[:3])),
        }},
        "signature": signature.to_dict() if signature else None,
    }
    (path / MLMODEL).write_text(yaml.safe_dump({k: v for k, v in mlmodel.items() if v is not None},
                                               sort_keys=False))
    return described


def log_model(artifact_path: str, python_model: PythonModel, artifacts: dict[str, str] | None = None,
              registered_model_name: str | None = None, metadata: dict | None = None,
              pip_requirements: list[str] | None = None, tags: dict | None = None,
              signature: ModelSignature | None = None, feature_spec=None, features=None,
              training: dict | None = None) -> ModelInfo:
    """Saves the model under the active run's artifacts (starting a run if none is active),
    and registers a new version of registered_model_name when given. tags become the
    version's tags. What the model reads and is fit on (save_model's features.json) is
    counted in the run's params too, as is `training` (mlflow.training_summary()): rows per
    split, the label's positive rate."""
    from . import MlflowClient, _require_run, log_params

    run = _require_run()
    dest = _storage.join(run.info.artifact_uri, artifact_path)
    with tempfile.TemporaryDirectory() as tmp:
        described = save_model(tmp, python_model, artifacts, metadata, pip_requirements,
                               run_id=run.info.run_id, artifact_path=artifact_path,
                               signature=signature, feature_spec=feature_spec, features=features, training=training)
        _storage.upload_tree(Path(tmp), dest)
    if described:
        log_params(FT.params(described))
    if training:
        log_params(TR.params(training))
    version = None
    if registered_model_name:
        version = MlflowClient().create_model_version(
            registered_model_name, dest, run_id=run.info.run_id, tags=tags).version
    return ModelInfo(artifact_path=artifact_path, model_uri=f"runs:/{run.info.run_id}/{artifact_path}",
                     run_id=run.info.run_id, registered_model_version=version, metadata=metadata or {})


def load_model(model_uri: str, dst_path: str | None = None) -> PyFuncModel:
    """model_uri: models:/<name>/<version>, models:/<name>@<alias>, runs:/<run>/<path>, a
    gs:// directory or a local one. Remote models download to dst_path (default: a temp dir)."""
    import cloudpickle

    from . import _resolve

    source = _resolve(model_uri)
    if source.startswith("gs://"):
        local = Path(dst_path or tempfile.mkdtemp(prefix="mlflow-model-"))
        _storage.download_tree(source, local)
    else:
        local = Path(source)
    mlmodel = yaml.safe_load((local / MLMODEL).read_text())
    flavor = mlmodel["flavors"]["python_function"]
    context = PythonModelContext({name: str(local / a["path"]) for name, a in flavor.get("artifacts", {}).items()})
    try:
        python_model = cloudpickle.loads((local / flavor["python_model"]).read_bytes())
        python_model.load_context(context)
    except Exception as e:
        saved, running = str(flavor.get("python_version", "")), ".".join(map(str, sys.version_info[:2]))
        if saved and ".".join(saved.split(".")[:2]) != running:
            # Code pickled by value is bytecode, which another Python version misreads (often
            # as a nonsense AttributeError).
            e.add_note(f"{model_uri} was saved by Python {saved} and this is Python {running}: code "
                       f"cloudpickle pickled by value only loads on the version that saved it. Save "
                       f"the model again under Python {running}.")
        raise
    return PyFuncModel(python_model, context, ModelMetadata(
        run_id=mlmodel.get("run_id"), artifact_path=mlmodel.get("artifact_path"),
        metadata=mlmodel.get("metadata") or {},
        signature=ModelSignature.from_dict(mlmodel["signature"]) if mlmodel.get("signature") else None,
        feature_spec=_feature_spec(local)))


def _feature_spec(model_dir: Path):
    from gcpbricks.features import SPEC_FILE, FeatureService

    return FeatureService.load(model_dir) if (model_dir / SPEC_FILE).exists() else None

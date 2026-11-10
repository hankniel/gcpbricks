"""What a model reads and what it is fit on, saved beside it as features.json, so the console's
model version page names them and counts them, however the model was built:

    read           the columns fetched before any transform: the feature table read (a
                   feature_manager plan's fetch_columns) or the feature store (a FeatureService)
    model_inputs   the columns the estimator is fit on, per input set: one ("model"), or one per
                   feature_manager inputs set or submodel
    computed       the features a pipeline derives from `read` (feature_manager's outputs)

pyfunc.log_model writes it on its own from what it is given (features=, signature=,
feature_spec=); a model logged by hand (a torch checkpoint, any flavor) calls log_features().
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

FILE = "features.json"


def _plan(pipeline):
    """A feature_manager FeaturePipeline's plan: the fitted one when it was fitted."""
    return getattr(pipeline, "plan_", None) or pipeline.plan()


def describe(features=None, *, signature=None, feature_spec=None, read: list[str] | None = None,
             model_inputs: list[str] | dict[str, list[str]] | None = None,
             computed: list[str] | None = None) -> dict | None:
    """features.json's content; None when nothing says what the model reads or is fit on.
    `features`: a feature_manager FeaturePipeline. Given lists win over what is derived."""
    rec: dict = {"read": None, "model_inputs": None, "computed": None, "sources": {}}
    if features is not None:
        plan = _plan(features)
        rec["read"] = list(plan.fetch_columns or plan.raw_columns)
        rec["model_inputs"] = {k: list(v) for k, v in plan.inputs.items()} or None
        rec["computed"] = list(plan.outputs)
        rec["sources"] = {"read": "feature_manager fetch_columns", "computed": "feature_manager outputs",
                          **({"model_inputs": "feature_manager inputs"} if rec["model_inputs"] else {})}
    if feature_spec is not None and rec["read"] is None:
        rec["read"] = [*feature_spec.feature_names, *feature_spec.spine_inputs]
        rec["sources"]["read"] = f"feature service {feature_spec.name}" if feature_spec.name else "feature_spec.json"
    if signature is not None and rec["model_inputs"] is None:
        rec["model_inputs"] = {"model": signature.inputs.input_names()}
        rec["sources"]["model_inputs"] = "MLmodel signature"
    for key, given in (("read", read), ("model_inputs", model_inputs), ("computed", computed)):
        if given is not None:
            rec[key] = {"model": list(given)} if key == "model_inputs" and not isinstance(given, dict) else (
                {k: list(v) for k, v in given.items()} if isinstance(given, dict) else list(given))
            rec["sources"][key] = "logged"
    if rec["read"] is None and rec["model_inputs"] is None:
        return None
    rec["counts"] = {"read": len(rec["read"]) if rec["read"] is not None else None,
                     "model_inputs": {k: len(v) for k, v in (rec["model_inputs"] or {}).items()},
                     "computed": len(rec["computed"]) if rec["computed"] is not None else None}
    return rec


def save(rec: dict, model_dir: str | Path) -> Path:
    path = Path(model_dir) / FILE
    path.write_text(json.dumps(rec, indent=2))
    return path


def params(rec: dict) -> dict:
    """The counts as run params, so an experiment's runs compare on them."""
    inputs = rec["counts"]["model_inputs"]
    return {k: v for k, v in {"n_features_read": rec["counts"]["read"],
                              "n_model_inputs": max(inputs.values()) if inputs else None,
                              "n_features_computed": rec["counts"]["computed"]}.items() if v is not None}


def log_features(read: list[str] | None = None, model_inputs: list[str] | dict[str, list[str]] | None = None,
                 computed: list[str] | None = None, *, features=None, artifact_path: str = "model") -> dict:
    """For a model logged without pyfunc.log_model: saves features.json under the active run's
    artifacts at `artifact_path` (next to the model, where the console reads it) and logs the
    counts as params. Returns what it saved."""
    from . import _require_run, _storage, log_params

    rec = describe(features, read=read, model_inputs=model_inputs, computed=computed)
    if rec is None:
        raise ValueError("log_features needs read=, model_inputs= or features=")
    run = _require_run()
    with tempfile.TemporaryDirectory() as tmp:
        save(rec, tmp)
        _storage.upload_tree(Path(tmp), _storage.join(run.info.artifact_uri, artifact_path))
    log_params(params(rec))
    return rec

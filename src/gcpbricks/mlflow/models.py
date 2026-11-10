"""mlflow.models: a model's input signature, in MLflow's own MLmodel format (so real MLflow
reads it too):

    signature:
      inputs: '[{"type": "double", "name": "cost", "required": true}, ...]'
      outputs: null
      params: null

    from gcpbricks import mlflow

    signature = mlflow.models.infer_signature(train_df[columns])
    mlflow.pyfunc.log_model("model", python_model=..., signature=signature)
    mlflow.pyfunc.load_model(uri).metadata.get_input_schema().input_names()

predict() checks a DataFrame has every required input, so a missing column fails up front,
naming it, instead of as a KeyError deep in the model. Unlike MLflow, it neither drops extra
columns nor casts types: a model may read columns its signature leaves out.

Model validation (MetricThreshold, make_metric, evaluate) is _evaluation.py's, re-exported here
where MLflow has it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ._evaluation import (  # noqa: F401  (re-exported as mlflow.models.*)
    EvaluationMetric,
    EvaluationResult,
    MetricThreshold,
    ModelValidationFailedException,
    evaluate,
    make_metric,
)

#: pandas dtype kind -> MLflow's ColSpec type.
_KIND_TYPES = {"f": "double", "i": "long", "u": "long", "b": "boolean", "M": "datetime",
               "O": "string", "U": "string", "S": "binary"}


@dataclass(frozen=True)
class ColSpec:
    type: str
    name: str
    required: bool = True


@dataclass(frozen=True)
class Schema:
    inputs: list[ColSpec]

    def input_names(self) -> list[str]:
        return [c.name for c in self.inputs]

    def required_input_names(self) -> list[str]:
        return [c.name for c in self.inputs if c.required]

    def to_json(self) -> str:
        return json.dumps([{"type": c.type, "name": c.name, "required": c.required} for c in self.inputs])

    @classmethod
    def from_json(cls, text: str) -> Schema:
        return cls([ColSpec(c["type"], c["name"], c.get("required", True)) for c in json.loads(text)])


@dataclass(frozen=True)
class ModelSignature:
    inputs: Schema

    def to_dict(self) -> dict:
        return {"inputs": self.inputs.to_json(), "outputs": None, "params": None}

    @classmethod
    def from_dict(cls, d: dict) -> ModelSignature:
        return cls(Schema.from_json(d["inputs"]))


def _type(dtype) -> str:
    if str(dtype) == "float32":
        return "float"
    if str(dtype) in ("int32", "int16", "int8"):
        return "integer"
    if str(dtype) in ("string", "category"):
        return "string"
    return _KIND_TYPES.get(dtype.kind, "string")


def infer_signature(model_input) -> ModelSignature:
    """The signature of a DataFrame's columns, each required, in its column order."""
    return ModelSignature(Schema([ColSpec(_type(t), str(n)) for n, t in model_input.dtypes.items()]))


def check_inputs(signature: ModelSignature | None, data) -> None:
    """Raises MlflowException when `data` (a DataFrame) lacks a required input."""
    if signature is None or not hasattr(data, "columns"):
        return
    missing = [n for n in signature.inputs.required_input_names() if n not in data.columns]
    if missing:
        from . import MlflowException

        raise MlflowException(f"Model is missing inputs {missing}.")

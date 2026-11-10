"""FeaturePipeline: the one object a model.py holds.

Built from the spec at fit time and pickled with the estimator, so serving runs the
fitted steps and never reads the spec again -- a version scored in a point-in-time
backfill keeps the features it was trained with.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils.validation import check_is_fitted

from .expr import evaluate
from .plan import Plan, compile_spec
from .steps import build, column


# auto_wrap_output_keys=None: sklearn's set_output would relabel the result with
# get_feature_names_out(), which names only the added features, not the input columns
# transform() passes through.
class FeaturePipeline(TransformerMixin, BaseEstimator, auto_wrap_output_keys=None):
    """fit(df, y=None) learns every `fit:` entry's statistic from df; transform(df)
    returns df plus the declared features. y is only read by entries with `of: "@target"`."""

    def __init__(self, spec: dict | None = None):
        self.spec = spec

    @classmethod
    def from_manifest(cls, manifest: dict) -> FeaturePipeline:
        """The pipeline a model manifest's `features` (its features.yaml) declares."""
        if not manifest.get("features"):
            raise ValueError(f"{manifest.get('name')}: the model has no features.yaml")
        return cls(manifest["features"])

    def plan(self) -> Plan:
        """The compiled plan, without fitting -- what a check needs (raw_columns, outputs)."""
        return compile_spec(self.spec)

    def fit(self, X, y=None):
        self._build()
        self.pipeline_.fit(X, y)
        return self

    def fit_transform(self, X, y=None, **fit_params):
        self._build()
        return self.pipeline_.fit_transform(X, y)

    def transform(self, X):
        check_is_fitted(self, "pipeline_")
        return self.pipeline_.transform(X)

    def inputs(self, name: str = "default") -> list[str]:
        """An `inputs` set's columns, from the fitted plan -- so a pickled model keeps the
        sets it was trained with."""
        check_is_fitted(self, "plan_")
        if name not in self.plan_.inputs:
            raise KeyError(f"no inputs set {name!r}; declared: {sorted(self.plan_.inputs)}")
        return list(self.plan_.inputs[name])

    def submodels(self) -> list[str]:
        """The declared submodels' names, in fit order."""
        check_is_fitted(self, "plan_")
        return list(self.plan_.submodels)

    def params(self, name: str) -> dict:
        """A submodel's estimator parameters, as declared."""
        check_is_fitted(self, "plan_")
        return dict(self.plan_.submodels[name].params)

    def route(self, X) -> pd.Series:
        """Which submodel owns each row of transform()'s output, by its `when`; None
        for a row no rule matches. A row two rules match raises."""
        check_is_fitted(self, "plan_")
        owner = pd.Series([None] * len(X), index=X.index, dtype=object)
        taken = np.zeros(len(X), dtype=bool)
        for name, sub in self.plan_.submodels.items():
            mask = np.asarray(evaluate(sub.when, lambda c: column(X, c), len(X)), dtype=bool)
            both = mask & taken
            if both.any():
                raise ValueError(f"{int(both.sum())} rows match submodel {name!r} and an earlier one")
            owner[mask] = name
            taken |= mask
        return owner

    def get_feature_names_out(self, input_features=None):
        check_is_fitted(self, "plan_")
        return np.asarray(self.plan_.outputs, dtype=object)

    def _build(self) -> None:
        self.plan_ = compile_spec(self.spec)
        self.pipeline_ = build(self.plan_)

"""feature_manager -- feature engineering declared in a model's features.yaml.

    pipe = FeaturePipeline.from_manifest(manifest)  # in fit()
    train = pipe.fit_transform(train_df, y)
    self.features_pipe = pipe                  # pickled with the estimator
    ...
    feat = self.features_pipe.transform(df)    # in predict()
    X = feat[self.features_pipe.inputs("up")]  # an `inputs` set, in its declared order

See README.md for the spec format.
"""
from .explain import explain, preview
from .expr import ExprError
from .plan import Plan, compile_spec, fetch_columns

__all__ = ["ExprError", "FeaturePipeline", "Plan", "compile_spec", "explain", "fetch_columns", "preview"]


def __getattr__(name: str):
    # Lazy: compiling a spec needs only numpy, so a spec can be checked (e.g. on a CI
    # runner) where scikit-learn is not installed.
    if name == "FeaturePipeline":
        from .pipeline import FeaturePipeline
        return FeaturePipeline
    raise AttributeError(f"module 'gcpbricks.feature_manager' has no attribute {name!r}")

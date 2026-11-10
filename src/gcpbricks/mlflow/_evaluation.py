"""mlflow.evaluate and mlflow.validate_evaluation_results, MLflow 3's model validation, for
model_type="regressor" (or None: extra_metrics only):

    from gcpbricks import mlflow
    from gcpbricks.mlflow.models import MetricThreshold, make_metric

    candidate = mlflow.evaluate(data=df, predictions="pred", targets="y", model_type="regressor",
                                extra_metrics=[make_metric(eval_fn=fn, greater_is_better=True)])
    baseline = mlflow.evaluate(model="models:/roas_d30@champion", data=features_and_y, targets="y",
                               model_type="regressor", predictions="predicted_roas_d30")
    mlflow.validate_evaluation_results(
        {"mean_absolute_error": MetricThreshold(threshold=1.0, greater_is_better=False)},
        candidate, baseline)

Metrics are MLflow's names and formulas, computed in process, and logged to the active run
(a run is started and ended around the call when none is active, as MLflow does). Unlike
MLflow, no eval_results_table artifact is written, and the evaluators, custom_artifacts,
sample_weights and env_manager options are not supported.
"""
from __future__ import annotations

import inspect
import logging
import operator
import os
from decimal import Decimal

from . import MlflowException

_logger = logging.getLogger(__name__)


# --- metrics ------------------------------------------------------------------------------

class MetricValue:
    """mlflow.metrics.MetricValue: per-row scores and/or aggregates of one metric."""

    def __init__(self, scores=None, justifications=None, aggregate_results: dict | None = None):
        self.scores, self.justifications, self.aggregate_results = scores, justifications, aggregate_results


class EvaluationMetric:
    def __init__(self, eval_fn, name, greater_is_better, long_name=None, version=None,
                 metric_details=None, metric_metadata=None, genai_metric_args=None):
        self.eval_fn, self.name, self.greater_is_better = eval_fn, name, greater_is_better
        self.long_name, self.version = long_name or name, version
        self.metric_details, self.metric_metadata = metric_details, metric_metadata

    def __str__(self):
        return f"EvaluationMetric(name={self.name}, greater_is_better={self.greater_is_better})"


def make_metric(*, eval_fn, greater_is_better, name=None, long_name=None, version=None,
                metric_details=None, metric_metadata=None, genai_metric_args=None) -> EvaluationMetric:
    """eval_fn(predictions, targets, metrics, <column or evaluator_config key>...) -> float or
    MetricValue. Parameters other than predictions/targets/metrics are filled, by name, from
    evaluator_config's col_mapping, then data's feature columns, then evaluator_config, then
    an already computed metric."""
    if name is None:
        name = getattr(eval_fn, "__name__", None)
        if not name or name == "<lambda>":
            raise MlflowException("`name` must be specified if `eval_fn` is a lambda function.")
    return EvaluationMetric(eval_fn, name, greater_is_better, long_name, version,
                            metric_details, metric_metadata, genai_metric_args)


class EvaluationResult:
    def __init__(self, metrics, artifacts, run_id=None):
        self._metrics, self._artifacts, self._run_id = metrics, artifacts, run_id

    @property
    def metrics(self) -> dict:
        return self._metrics

    @property
    def artifacts(self) -> dict:
        return self._artifacts

    @property
    def run_id(self) -> str | None:
        return self._run_id


def _classifier_metrics(y, p, w=None) -> dict:
    """mlflow.models.evaluation.evaluators.classifier's metrics for a binary target, from the
    predicted labels, sklearn's formulas without sklearn; precision, recall and f1 of the
    positive class (1), 0.0 where undefined, as sklearn's zero_division=0. `w` (evaluator_config's
    sample_weights) weighs the scores, as MLflow's; the counts and example_count are unweighted."""
    import numpy as np

    labels = set(np.unique(y).tolist()) | set(np.unique(p).tolist())
    if not labels <= {0.0, 1.0}:
        raise MlflowException(f"model_type 'classifier': only a binary 0/1 target and predicted "
                              f"labels are supported, got {sorted(labels)[:5]}")
    ones = np.ones(len(y)) if w is None else w

    def total(mask):
        return float(np.sum(ones[mask]))

    tp, fp = total((p == 1) & (y == 1)), total((p == 1) & (y == 0))
    fn, tn = total((p == 0) & (y == 1)), total((p == 0) & (y == 0))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    count = {name: float(np.sum(mask)) for name, mask in (
        ("true_positives", (p == 1) & (y == 1)), ("false_positives", (p == 1) & (y == 0)),
        ("true_negatives", (p == 0) & (y == 0)), ("false_negatives", (p == 0) & (y == 1)))}
    return {
        "example_count": len(y),
        "accuracy_score": (tp + tn) / (tp + tn + fp + fn),
        "precision_score": precision,
        "recall_score": recall,
        "f1_score": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        **count,
    }


def _regressor_metrics(y, p, w=None) -> dict:
    """mlflow.models.evaluation.evaluators.regressor's metrics, sklearn's formulas without
    sklearn. `w` (evaluator_config's sample_weights) weighs them as MLflow's does; max_error
    and example_count are unweighted, sum_on_target is sum(w * y), mean_on_target that / n."""
    import numpy as np

    err = y - p
    n = len(y)
    w = np.ones(n) if w is None else w
    mse = float(np.average(err ** 2, weights=w))
    ss_res = float(np.sum(w * err ** 2))
    ss_tot = float(np.sum(w * (y - np.average(y, weights=w)) ** 2))
    if n < 2:
        r2 = float("nan")
    elif ss_tot == 0:
        r2 = 1.0 if ss_res == 0 else 0.0
    else:
        r2 = 1 - ss_res / ss_tot
    total = float(np.sum(w * y))
    return {
        "example_count": n,
        "mean_absolute_error": float(np.average(np.abs(err), weights=w)),
        "mean_squared_error": mse,
        "root_mean_squared_error": float(np.sqrt(mse)),
        "sum_on_target": total,
        "mean_on_target": total / n,
        "r2_score": r2,
        "max_error": float(np.max(np.abs(err))),
        "mean_absolute_percentage_error": float(np.average(
            np.abs(err) / np.maximum(np.abs(y), np.finfo(np.float64).eps), weights=w)),
    }


def _model_predictions(model, data, predictions: str | None, inference_params):
    import numpy as np
    import pandas as pd

    if isinstance(model, str):
        from . import pyfunc
        model = pyfunc.load_model(model)
    out = (model.predict(data, params=inference_params) if hasattr(model, "predict")
           else model(data))
    if isinstance(out, pd.DataFrame):
        if predictions is not None:
            out = out[predictions]
        elif len(out.columns) == 1:
            out = out.iloc[:, 0]
        else:
            raise MlflowException("The model outputs multiple columns: name the one to evaluate with "
                                  "`predictions`.")
    out = np.asarray(out, dtype=np.float64).ravel()
    if len(out) != len(data):
        raise ValueError(f"Found input variables with inconsistent numbers of samples: "
                         f"[{len(data)}, {len(out)}]")
    return out


def _metric_args(metric: EvaluationMetric, pred, target, input_df, config: dict,
                 computed: dict) -> list:
    col_mapping = config.get("col_mapping", {})
    args, missing = [], []
    for pname, param in inspect.signature(metric.eval_fn).parameters.items():
        column = col_mapping.get(pname, pname)
        if column == "predictions":
            args.append(pred.copy())
        elif column == "targets":
            if target is not None:
                args.append(target.copy())
            elif param.default is inspect.Parameter.empty:
                missing.append(pname)
            else:
                args.append(param.default)
        elif column == "metrics":
            args.append({k: MetricValue(aggregate_results={k: v}) for k, v in computed.items()})
        elif not isinstance(column, str):
            args.append(column)
        elif column in input_df.columns:
            args.append(input_df[column])
        elif column in config:
            args.append(config[column])
        elif column in computed:
            args.append(MetricValue(aggregate_results={column: computed[column]}))
        elif param.default is inspect.Parameter.empty:
            missing.append(pname)
        else:
            args.append(param.default)
    if missing:
        raise MlflowException(f"Error: Metric calculation failed for the following metrics:\n"
                              f"Metric '{metric.name}' requires the following: {missing}")
    return args


def _flatten(name: str, value) -> dict:
    if not isinstance(value, MetricValue):
        return {name: value}
    return {(name if agg == name else f"{name}/{agg}"): v
            for agg, v in (value.aggregate_results or {}).items() if v is not None}


def evaluate(model=None, data=None, *, model_type=None, targets=None, predictions=None,
             dataset_path=None, feature_names=None, evaluators=None, evaluator_config=None,
             extra_metrics=None, custom_artifacts=None, env_manager="local", model_config=None,
             inference_params=None, model_id=None) -> EvaluationResult:
    """model: None (data's `predictions` column is the model output), a models:/, runs:/ or
    gs:// URI, a PyFuncModel, or a callable. Its output must be one value per row of data,
    in data's order, as MLflow's."""
    import numpy as np
    import pandas as pd

    from . import _require_run, active_run, end_run, log_metrics

    if model_type not in (None, "regressor", "classifier"):
        raise MlflowException(f"model_type {model_type!r}: only 'regressor', 'classifier' (binary) "
                              "or None is supported")
    if evaluators not in (None, "default") or custom_artifacts:
        raise MlflowException("evaluators and custom_artifacts are not supported")
    if not isinstance(data, pd.DataFrame):
        raise MlflowException("data must be a pandas DataFrame")
    config = dict(evaluator_config or {})
    weights = config.pop("sample_weights", None)
    if weights is not None:
        weights = np.asarray(weights, dtype=np.float64)
        if weights.shape != (len(data),):
            raise MlflowException(f"evaluator_config sample_weights: {len(data)} values, one per row, "
                                  f"got shape {weights.shape}")

    if model is None:
        if predictions is None:
            raise MlflowException("Either a model or set of predictions must be specified in order to "
                                  "use the default evaluator.")
        pred = data[predictions].to_numpy(dtype=np.float64)
    else:
        pred = _model_predictions(model, data, predictions, inference_params)
    target = None if targets is None else data[targets].to_numpy(dtype=np.float64)
    if np.isnan(pred).any() or (target is not None and np.isnan(target).any()):
        raise ValueError("Input contains NaN.")

    excluded = {targets, predictions} if model is None else {targets}
    input_df = (data[list(feature_names)] if feature_names is not None
                else data[[c for c in data.columns if c not in excluded]])

    metrics: dict = {}
    if model_type in ("regressor", "classifier"):
        if target is None:
            raise MlflowException(f"The targets argument must be specified for {model_type} models.")
        metrics.update((_regressor_metrics if model_type == "regressor" else _classifier_metrics)(
            target, pred, weights))
    pred_s = pd.Series(pred, index=data.index)
    target_s = None if target is None else pd.Series(target, index=data.index)
    for metric in extra_metrics or []:
        value = metric.eval_fn(*_metric_args(metric, pred_s, target_s, input_df, config, metrics))
        name = f"{metric.name}/{metric.version}" if metric.version else metric.name
        metrics.update(_flatten(name, value))
    if prefix := config.get("metric_prefix"):
        metrics = {f"{prefix}{k}": v for k, v in metrics.items()}

    started = active_run() is None
    run = _require_run()
    log_metrics(metrics)
    if started:
        end_run()
    return EvaluationResult(metrics, {}, run_id=run.info.run_id)


# --- validation ---------------------------------------------------------------------------

class MetricThresholdClassException(MlflowException):
    def __init__(self, _message, **kwargs):
        super().__init__(f"Could not instantiate MetricThreshold class: {_message}")


class ModelValidationFailedException(MlflowException):
    pass


class MetricThreshold:
    """threshold: an absolute bound. min_absolute_change (> 0) / min_relative_change (0..1):
    the margin the candidate must beat the baseline by, in greater_is_better's direction."""

    def __init__(self, threshold=None, min_absolute_change=None, min_relative_change=None,
                 greater_is_better=None):
        if threshold is not None and type(threshold) not in {int, float}:
            raise MetricThresholdClassException("`threshold` parameter must be a number.")
        if min_absolute_change is not None and (
                type(min_absolute_change) not in {int, float} or min_absolute_change <= 0):
            raise MetricThresholdClassException("`min_absolute_change` parameter must be a positive number.")
        if min_relative_change is not None:
            if not isinstance(min_relative_change, float):
                raise MetricThresholdClassException(
                    "`min_relative_change` parameter must be a floating point number.")
            if min_relative_change < 0 or min_relative_change > 1:
                raise MetricThresholdClassException("`min_relative_change` parameter must be between 0 and 1.")
        if greater_is_better is None:
            raise MetricThresholdClassException("`greater_is_better` parameter must be defined.")
        if not isinstance(greater_is_better, bool):
            raise MetricThresholdClassException("`greater_is_better` parameter must be a boolean.")
        if threshold is None and min_absolute_change is None and min_relative_change is None:
            raise MetricThresholdClassException("no threshold was specified.")
        self._threshold = threshold
        self._min_absolute_change = min_absolute_change
        self._min_relative_change = min_relative_change
        self._greater_is_better = greater_is_better

    @property
    def threshold(self):
        return self._threshold

    @property
    def min_absolute_change(self):
        return self._min_absolute_change

    @property
    def min_relative_change(self):
        return self._min_relative_change

    @property
    def greater_is_better(self):
        return self._greater_is_better

    def __str__(self):
        parts = []
        if self._threshold is not None:
            parts.append(f"Threshold: {self._threshold}.")
        if self._min_absolute_change is not None:
            parts.append(f"Minimum Absolute Change: {self._min_absolute_change}.")
        if self._min_relative_change is not None:
            parts.append(f"Minimum Relative Change: {self._min_relative_change}.")
        parts.append("Greater value is better." if self._greater_is_better else "Lower value is better.")
        return " ".join(parts)


class _MetricValidationResult:
    def __init__(self, metric_name, candidate_metric_value, metric_threshold, baseline_metric_value=None):
        self.metric_name = metric_name
        self.candidate_metric_value = candidate_metric_value
        self.baseline_metric_value = baseline_metric_value
        self.metric_threshold = metric_threshold
        self.missing_candidate = self.missing_baseline = False
        self.threshold_failed = self.min_absolute_change_failed = self.min_relative_change_failed = False

    def is_success(self):
        return not (self.missing_candidate or self.missing_baseline or self.threshold_failed
                    or self.min_absolute_change_failed or self.min_relative_change_failed)

    def __str__(self):
        name = self.metric_name
        if self.is_success():
            return f"Metric {name} passed the validation."
        if self.missing_candidate:
            return (f"Metric validation failed: metric {name} was missing from the "
                    f"evaluation result of the candidate model.")
        parts = []
        if self.threshold_failed:
            parts.append(f"Metric {name} value threshold check failed: candidate model {name} = "
                         f"{self.candidate_metric_value}, {name} threshold = {self.metric_threshold.threshold}.")
        if self.missing_baseline:
            parts.append(f"Model comparison failed: metric {name} was missing from the evaluation "
                         f"result of the baseline model.")
        else:
            if self.min_absolute_change_failed:
                parts.append(f"Metric {name} minimum absolute change check failed: candidate model "
                             f"{name} = {self.candidate_metric_value}, baseline model {name} = "
                             f"{self.baseline_metric_value}, {name} minimum absolute change threshold = "
                             f"{self.metric_threshold.min_absolute_change}.")
            if self.min_relative_change_failed:
                parts.append(f"Metric {name} minimum relative change check failed: candidate model "
                             f"{name} = {self.candidate_metric_value}, baseline model {name} = "
                             f"{self.baseline_metric_value}, {name} minimum relative change threshold = "
                             f"{self.metric_threshold.min_relative_change}.")
        return " ".join(parts)


def _validate(thresholds: dict, candidate: dict, baseline: dict) -> None:
    results = {name: _MetricValidationResult(name, candidate.get(name), t, baseline.get(name))
               for name, t in thresholds.items()}
    for name, t in thresholds.items():
        r = results[name]
        if name not in candidate:
            r.missing_candidate = True
            continue
        cand = candidate[name]
        base = baseline[name] if baseline else None
        compare = operator.ge if t.greater_is_better else operator.le
        shift = operator.add if t.greater_is_better else operator.sub
        if t.threshold is not None:
            r.threshold_failed = not compare(cand, t.threshold)
        if (t.min_relative_change or t.min_absolute_change) and name not in baseline:
            r.missing_baseline = True
            continue
        if t.min_absolute_change is not None:
            r.min_absolute_change_failed = not compare(Decimal(cand), Decimal(shift(base, t.min_absolute_change)))
        if t.min_relative_change is not None:
            if base == 0:
                _logger.warning(f"Cannot perform relative model comparison for metric {name} as baseline "
                                "metric value is 0. Falling back to simple comparison: verifying that "
                                "candidate metric value is better than the baseline metric value.")
                r.min_relative_change_failed = not compare(Decimal(cand), Decimal(shift(base, 1e-10)))
                continue
            change = (cand - base) / base if t.greater_is_better else (base - cand) / base
            r.min_relative_change_failed = change < t.min_relative_change
    failures = [str(r) for r in results.values() if not r.is_success()]
    if failures:
        raise ModelValidationFailedException(os.linesep.join(failures))


def validate_evaluation_results(validation_thresholds: dict, candidate_result: EvaluationResult,
                                baseline_result: EvaluationResult | None = None) -> None:
    """Raises ModelValidationFailedException naming every failed check; returns None on a pass."""
    if (type(validation_thresholds) is not dict
            or not all(type(k) is str for k in validation_thresholds)
            or not all(isinstance(t, MetricThreshold) for t in validation_thresholds.values())):
        raise MlflowException("The validation thresholds argument must be a dictionary that maps strings "
                              "to MetricThreshold objects.")
    _logger.info("Validating candidate model metrics against baseline")
    _validate(validation_thresholds, candidate_result.metrics,
              baseline_result.metrics if baseline_result else {})
    _logger.info("Model validation passed!")

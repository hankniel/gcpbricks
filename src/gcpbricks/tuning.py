"""Hyperparameter sweeps: Optuna's TPE search, each trial an MLflow child run of the sweep's
run, as a Databricks sweep logs nested runs; and the params a model trains with, which a
retrain takes from its @champion.

    from gcpbricks import mlflow, tuning

    mlflow.set_experiment("roas-d30-hpt")
    result = tuning.sweep(
        space={"learning_rate": {"type": "double", "min": 1e-3, "max": 0.3, "scale": "log"},
               "max_depth": {"type": "integer", "min": 3, "max": 10}},
        objective=train_and_score,          # params -> the metric; None: no score this trial
        metric="r2", goal="maximize", n_trials=20, run_name="sweep-123")
    result.best_params, result.best_value

    # A retrain: the champion's tuned params, else the defaults.
    params, source = tuning.champion_params("roas_d30", space)
    with mlflow.start_run(run_name=model_id):
        tuning.log_params(params, space, source)
        fit(defaults | params)

A spec is {"type": "double" | "integer", "min", "max", "scale": "log"?} or {"type":
"categorical", "values": [...]}. `space` may instead hold independent studies sharing each
trial, {study: {param: spec}}, when one fit trains parts scored apart (a model's submodels):
the objective then takes {study: params} and returns {study: score}, each study keeps its
own best, and a run's params name a study's as "<study>.<param>".

The objective runs inside the trial's child run, so whatever it logs lands there; the sweep
logs each trial's params and score there too, and the best params and score on the sweep's
run. A trial with no score is pruned, not failed: its study learns nothing from it.

Tuned params are the champion's, not the best score ever logged: scores from different
validation windows don't compare, and the champion's params are the ones that beat their
predecessor on the same window. They are reused only while the space they were searched in
is today's (space_hash), so changing the space resets a model to its defaults.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gcpbricks import mlflow
from gcpbricks.targets import PROD

GOALS = ("maximize", "minimize")
#: Tags of a run that trained with tuned params, or of a sweep's run.
SPACE_HASH_TAG = "mlops.tuning.space_hash"
PARAMS_SOURCE_TAG = "mlops.tuning.params_source"
DEFAULTS = "defaults"


@dataclass
class SweepResult:
    #: {param: value}, or {study: {param: value}} for studies; a study with no scored trial
    #: is absent.
    best_params: dict
    #: The best score, or {study: score}.
    best_value: Any
    #: [{"n", "run_id", "params", "score"}], in trial order.
    trials: list[dict] = field(default_factory=list)


def suggest(trial, name: str, spec: dict):
    """One param's value for an Optuna trial, from its spec."""
    kind, log = spec["type"], spec.get("scale") == "log"
    if kind == "double":
        return trial.suggest_float(name, spec["min"], spec["max"], log=log)
    if kind == "integer":
        return trial.suggest_int(name, spec["min"], spec["max"], log=log)
    if kind == "categorical":
        return trial.suggest_categorical(name, spec["values"])
    raise ValueError(f"param {name}: unknown type {kind!r} (double, integer or categorical)")


def space_hash(space: dict) -> str:
    """Identifies a search space: tuned params are reused only while it is unchanged."""
    return hashlib.sha256(json.dumps(space, sort_keys=True).encode()).hexdigest()[:12]


def _studies(space: dict) -> tuple[bool, dict]:
    """(single, {study: {param: spec}}): a plain space is one study named ""."""
    if not space:
        raise ValueError("an empty search space")
    single = all(isinstance(v, dict) and "type" in v for v in space.values())
    return single, ({"": space} if single else space)


def _flat(per_study: dict) -> dict:
    # A run's params and metrics are flat: a study's are prefixed "<study>.".
    return {(f"{s}.{k}" if s else k): v for s, kv in per_study.items() for k, v in kv.items()}


def sweep(space: dict, objective: Callable[[dict], Any], *, metric: str, n_trials: int,
          goal: str = "maximize", run_name: str | None = None, seed: int = 42) -> SweepResult:
    """n_trials trials of objective over space, in the active experiment; see the module."""
    import optuna

    if goal not in GOALS:
        raise ValueError(f"goal must be one of {GOALS}, got {goal!r}")
    single, spaces = _studies(space)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    studies = {name: optuna.create_study(direction=goal, study_name=name or None,
                                         sampler=optuna.samplers.TPESampler(seed=seed))
               for name in spaces}

    trials = []
    with mlflow.start_run(run_name=run_name) as parent:
        mlflow.log_params({"n_trials": n_trials, "metric": metric, "goal": goal})
        mlflow.set_tag(SPACE_HASH_TAG, space_hash(space))
        for n in range(n_trials):
            asked = {s: study.ask() for s, study in studies.items()}
            params = {s: {p: suggest(asked[s], p, spec) for p, spec in spaces[s].items()}
                      for s in spaces}
            with mlflow.start_run(run_name=f"{parent.info.run_name}-trial-{n}", nested=True) as child:
                mlflow.log_params(_flat(params))
                got = objective(params[""] if single else params)
                scores = {"": got} if single else dict(got or {})
                mlflow.log_metrics(_flat({s: {metric: v} for s, v in scores.items() if v is not None}))
            for s, study in studies.items():
                if scores.get(s) is None:
                    study.tell(asked[s], state=optuna.trial.TrialState.PRUNED)
                else:
                    study.tell(asked[s], scores[s])
            trials.append({"n": n, "run_id": child.info.run_id,
                           "params": params[""] if single else params, "score": got})

        best_params, best_value = {}, {}
        for s, study in studies.items():
            if any(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials):
                best_params[s], best_value[s] = study.best_params, study.best_value
        mlflow.log_params(_flat({s: {f"best_{k}": v for k, v in p.items()} for s, p in best_params.items()}))
        mlflow.log_metrics(_flat({s: {f"best_{metric}": v} for s, v in best_value.items()}))
    if single:
        return SweepResult(best_params.get("", {}), best_value.get(""), trials)
    return SweepResult(best_params, best_value, trials)


def log_params(params: dict, space: dict, source: str) -> None:
    """On the active run: the searched params it trains with (champion_params' or a sweep's
    best), the space they belong to, and where they came from, so that once this run's
    version is @champion, champion_params hands them to the next retrain."""
    single, _ = _studies(space)
    mlflow.log_params(_flat({"": params} if single else params))
    mlflow.set_tags({SPACE_HASH_TAG: space_hash(space), PARAMS_SOURCE_TAG: source})


def _cast(spec: dict, value):
    # Vertex hands params back as it stored them: numbers as floats, the rest as strings.
    if spec["type"] == "integer":
        return int(float(value))
    if spec["type"] == "double":
        return float(value)
    return next((v for v in spec["values"] if str(v) == str(value)), value)


def champion_params(model_name: str, space: dict, alias: str = "champion", *,
                    prod: bool = True) -> tuple[dict, str]:
    """(params, source) a retrain trains with: the searched params the `alias` version's run
    logged (log_params), as space's shape -- or ({}, "defaults") when there is no such
    version, its run logged none, or they were searched in a space other than today's.

    prod: on a non-prod target (MLOPS_TARGET), read prod's model (`model_name` without its
    "_<target>"), so a dev run trains with what prod will. The version's run is the one its
    mlflow_run_id names; a version registered outside this SDK is found by its model_id
    label, the run of that name in the model's experiment (its name with - for _)."""
    target = os.environ.get("MLOPS_TARGET", "")
    if prod and target and target != PROD:
        model_name = model_name.removesuffix(f"_{target}")
    single, spaces = _studies(space)
    client = mlflow.MlflowClient()
    try:
        mv = client.get_model_version_by_alias(model_name, alias)
    except mlflow.MlflowException:
        return {}, DEFAULTS
    run_id = mv.run_id or mv.tags.get("model_id")
    if not run_id:
        return {}, DEFAULTS
    experiment = mv.tags.get(mlflow.EXPERIMENT_LABEL) or model_name.replace("_", "-")
    try:
        run = client.get_run(run_id, experiment=experiment)
    except mlflow.MlflowException:
        return {}, DEFAULTS
    if run.data.tags.get(SPACE_HASH_TAG) != space_hash(space):
        return {}, DEFAULTS
    logged = run.data.params
    params = {s: {p: _cast(spec, logged[f"{s}.{p}" if s else p]) for p, spec in specs.items()
                  if (f"{s}.{p}" if s else p) in logged}
              for s, specs in spaces.items()}
    params = {s: p for s, p in params.items() if p}
    if not params:
        return {}, DEFAULTS
    return (params.get("", {}) if single else params), f"{alias} v{mv.version}"

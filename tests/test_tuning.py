"""gcpbricks.tuning: an Optuna sweep whose trials are child runs of its run."""
from __future__ import annotations

import pytest

pytest.importorskip("optuna")

from gcpbricks import mlflow, tuning

SPACE = {"x": {"type": "double", "min": 0.0, "max": 6.0},
         "depth": {"type": "integer", "min": 1, "max": 4},
         "loss": {"type": "categorical", "values": ["l1", "l2"]}}


class FakeVertex:
    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.models: dict[str, list[dict]] = {}

    def log_run(self, experiment, run_id, params, metrics):
        self.runs[run_id] = {"experiment": experiment, "params": params, "metrics": metrics}

    def get_run(self, experiment, run_id):
        r = self.runs.get(run_id)
        # Vertex stores numbers as floats.
        return r and r["experiment"] == experiment and {
            "params": {k: float(v) if isinstance(v, int) else v for k, v in r["params"].items()},
            "metrics": r["metrics"]} or None

    def model(self, name):
        return {"description": ""} if name in self.models else None

    def upload_version(self, name, uri, labels, description):
        vs = self.models.setdefault(name, [])
        vs.append({"version": str(len(vs) + 1), "aliases": [], "labels": dict(labels), "uri": uri,
                   "description": description, "created_ms": 0})
        return dict(vs[-1])

    def versions(self, name, labels=None):
        return [dict(v) for v in self.models.get(name, [])]

    def add_alias(self, name, version, alias):
        next(v for v in self.models[name] if v["version"] == version)["aliases"].append(alias)


@pytest.fixture
def vertex(tmp_path, monkeypatch):
    fake = FakeVertex()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": fake})
    monkeypatch.setattr(mlflow, "_runs", [])
    mlflow.set_experiment("forecast-30d-hpt")
    return fake


def test_each_trial_is_a_child_run_and_the_sweep_run_holds_the_best(vertex):
    def objective(p):
        mlflow.log_metric("own", 1.0)  # lands in the trial's run
        return -(p["x"] - 3) ** 2 - (p["loss"] == "l1")

    result = tuning.sweep(SPACE, objective, metric="r2", n_trials=12, run_name="sweep-1")
    children = {k: r for k, r in vertex.runs.items() if r["params"].get("tag_mlflow.parentRunId") == "sweep-1"}
    assert sorted(children) == sorted(f"sweep-1-trial-{n}" for n in range(12))
    best = max(result.trials, key=lambda t: t["score"])
    assert result.best_value == best["score"] and result.best_params == best["params"]
    trial = vertex.runs[best["run_id"]]
    assert trial["metrics"] == {"own": 1.0, "r2": best["score"]}
    assert trial["params"]["x"] == best["params"]["x"]
    parent = vertex.runs["sweep-1"]
    assert parent["metrics"] == {"best_r2": best["score"]}
    assert parent["params"]["best_x"] == best["params"]["x"] and parent["params"]["n_trials"] == 12
    assert parent["params"]["status"] == "FINISHED" and mlflow.active_run() is None


def test_studies_keep_their_own_best_and_one_never_scored_has_none(vertex):
    space = {"a": {"x": SPACE["x"]}, "b": {"depth": SPACE["depth"]}}
    result = tuning.sweep(space, lambda p: {"a": -abs(p["a"]["x"] - 2), "b": None},
                          metric="mape", goal="maximize", n_trials=5, run_name="s")
    assert set(result.best_params) == {"a"} and set(result.best_value) == {"a"}
    trial = vertex.runs["s-trial-0"]
    assert set(trial["metrics"]) == {"a.mape"} and {"a.x", "b.depth"} <= set(trial["params"])
    assert set(vertex.runs["s"]["metrics"]) == {"a.best_mape"}


def test_a_failing_trial_fails_the_sweep_and_both_runs_are_logged_failed(vertex):
    def objective(p):
        raise RuntimeError("fit failed")

    with pytest.raises(RuntimeError, match="fit failed"):
        tuning.sweep(SPACE, objective, metric="r2", n_trials=3, run_name="s")
    assert vertex.runs["s-trial-0"]["params"]["status"] == "FAILED"
    assert vertex.runs["s"]["params"]["status"] == "FAILED"
    assert mlflow.active_run() is None


def _champion(name, run_name, params, space, *, experiment="forecast-30d", tags=None, run_id=True):
    """A training run that logged its params, its version registered and made @champion."""
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_name):
        tuning.log_params(params, space, "sweep s best")
    client = mlflow.MlflowClient()
    mv = client.create_model_version(name, "gs://m", run_id=run_name if run_id else None, tags=tags)
    client.set_registered_model_alias(name, "champion", mv.version)


def test_a_retrain_takes_the_champions_tuned_params(vertex):
    _champion("forecast_30d", "v1", {"x": 2.5, "depth": 3, "loss": "l2"}, SPACE)
    params, source = tuning.champion_params("forecast_30d", SPACE)
    assert params == {"x": 2.5, "depth": 3, "loss": "l2"} and type(params["depth"]) is int
    assert source == "champion v1"


def test_defaults_when_no_champion_or_the_space_changed(vertex):
    assert tuning.champion_params("forecast_30d", SPACE) == ({}, "defaults")
    _champion("forecast_30d", "v1", {"x": 2.5, "depth": 3, "loss": "l2"}, SPACE)
    wider = SPACE | {"x": {"type": "double", "min": 0.0, "max": 9.0}}
    assert tuning.champion_params("forecast_30d", wider) == ({}, "defaults")


def test_a_version_registered_outside_the_sdk_is_found_by_its_model_id(vertex):
    # demo_model: a version labelled model_id, its run named so in the model's experiment.
    _champion("forecast_30d", "v7", {"x": 1.0, "depth": 2, "loss": "l1"}, SPACE, tags={"model_id": "v7"}, run_id=False)
    assert tuning.champion_params("forecast_30d", SPACE)[0] == {"x": 1.0, "depth": 2, "loss": "l1"}


def test_a_dev_run_reads_prods_champion(vertex, monkeypatch):
    _champion("forecast_30d", "v1", {"x": 2.5, "depth": 3, "loss": "l2"}, SPACE)
    monkeypatch.setenv("MLOPS_TARGET", "dev")
    assert tuning.champion_params("forecast_30d_dev", SPACE)[1] == "champion v1"
    assert tuning.champion_params("forecast_30d_dev", SPACE, prod=False) == ({}, "defaults")


def test_studies_round_trip_through_the_champion(vertex):
    space = {"a": {"x": SPACE["x"]}, "b": {"depth": SPACE["depth"]}}
    _champion("forecast_30d", "v1", {"a": {"x": 4.0}, "b": {"depth": 2}}, space)
    assert tuning.champion_params("forecast_30d", space)[0] == {"a": {"x": 4.0}, "b": {"depth": 2}}
    assert vertex.runs["v1"]["params"]["a.x"] == 4.0


def test_a_sweep_run_records_its_space(vertex):
    tuning.sweep(SPACE, lambda p: 0.0, metric="r2", n_trials=1, run_name="s")
    assert vertex.runs["s"]["params"]["tag_mlops.tuning.space_hash"] == tuning.space_hash(SPACE)


@pytest.mark.parametrize("kw, message", [({"goal": "max"}, "goal"), ({"space": {}}, "empty")])
def test_a_bad_sweep_is_refused(vertex, kw, message):
    args = {"space": SPACE, "goal": "maximize"} | kw
    with pytest.raises(ValueError, match=message):
        tuning.sweep(args["space"], lambda p: 0.0, metric="r2", n_trials=1, goal=args["goal"])

"""gcpbricks.mlflow: MLflow's API over a fake Vertex, artifacts in a local root."""
from __future__ import annotations

import numpy as np
import pytest
import yaml

from gcpbricks import mlflow


class FakeVertex:
    def __init__(self):
        self.runs: dict[tuple[str, str], dict] = {}
        self.models: dict[str, list[dict]] = {}

    def log_run(self, experiment, run_id, params, metrics):
        self.runs[(experiment, run_id)] = {"params": params, "metrics": metrics}

    def get_run(self, experiment, run_id):
        return self.runs.get((experiment, run_id))

    def model(self, name):
        return {"description": ""} if name in self.models else None

    def upload_version(self, name, uri, labels, description):
        versions = self.models.setdefault(name, [])
        v = {"version": str(len(versions) + 1), "aliases": [], "labels": dict(labels), "uri": uri,
             "description": description, "created_ms": len(versions)}
        versions.append(v)
        return dict(v)

    def versions(self, name, labels=None):
        return [dict(v) for v in self.models.get(name, [])
                if all(v["labels"].get(k) == val for k, val in (labels or {}).items())]

    def version(self, name, version):
        return next((dict(v) for v in self.models.get(name, []) if v["version"] == version), None)

    def _v(self, name, version):
        return next(v for v in self.models[name] if v["version"] == version)

    def set_labels(self, name, version, labels):
        self._v(name, version)["labels"] = dict(labels)

    def add_alias(self, name, version, alias):
        self._v(name, version)["aliases"].append(alias)

    def remove_alias(self, name, version, alias):
        self._v(name, version)["aliases"].remove(alias)


class Doubler(mlflow.pyfunc.PythonModel):
    def load_context(self, context):
        with open(context.artifacts["factor"]) as f:
            self.factor = int(f.read())

    def predict(self, context, model_input, params=None):
        return [x * self.factor for x in model_input]


@pytest.fixture
def vertex(tmp_path, monkeypatch):
    fake = FakeVertex()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("CI_COMMIT_SHA", "ABC123")
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": fake})
    monkeypatch.setattr(mlflow, "_runs", [])
    mlflow.set_experiment("forecast-30d")
    (tmp_path / "factor.txt").write_text("2")
    return fake


def _log(tmp_path, run_name="Run 1", **kw):
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params({"lr": 0.1, "skipped": None})
        mlflow.log_metrics({"mape": 0.2, "flag": True})
        mlflow.set_tag("trigger", "ci")
        info = mlflow.pyfunc.log_model("model", python_model=Doubler(),
                                       artifacts={"factor": str(tmp_path / "factor.txt")},
                                       registered_model_name="forecast_30d", **kw)
    return run, info


def test_a_run_logs_params_metrics_and_tags_to_its_experiment(vertex, tmp_path):
    run, _ = _log(tmp_path)
    logged = vertex.runs[("forecast-30d", "run-1")]
    assert run.info.run_id == "run-1"
    assert logged["metrics"] == {"mape": 0.2}
    assert logged["params"] == {"lr": 0.1, "tag_trigger": "ci",
                                "tag_mlflow.source.git.commit": "abc123", "status": "FINISHED"}


def test_a_failed_run_is_still_logged_as_failed(vertex):
    with pytest.raises(RuntimeError), mlflow.start_run(run_name="boom"):
        mlflow.log_metric("x", 1)
        raise RuntimeError("boom")
    assert vertex.runs[("forecast-30d", "boom")]["params"]["status"] == "FAILED"
    assert mlflow.active_run() is None


def test_get_run_reads_a_logged_run_back_with_its_tags(vertex, tmp_path):
    _log(tmp_path)
    run = mlflow.get_run("run-1")
    assert (run.info.run_id, run.info.status, run.info.experiment_id) == ("run-1", "FINISHED", "forecast-30d")
    assert run.data.params == {"lr": 0.1} and run.data.metrics == {"mape": 0.2}
    assert run.data.tags == {"trigger": "ci", "mlflow.source.git.commit": "abc123"}
    with pytest.raises(mlflow.MlflowException, match="not found"):
        mlflow.MlflowClient().get_run("run-1", experiment="other")


def test_a_nested_run_is_tagged_with_its_parent(vertex):
    with mlflow.start_run(run_name="sweep"):
        with mlflow.start_run(run_name="sweep-trial-0", nested=True):
            mlflow.log_metric("r2", 0.4)
        with mlflow.start_run(run_name="unrelated"):
            pass
    runs = {run_id: r["params"] for (_, run_id), r in vertex.runs.items()}
    assert runs["sweep-trial-0"]["tag_mlflow.parentRunId"] == "sweep"
    assert "tag_mlflow.parentRunId" not in runs["sweep"]
    assert "tag_mlflow.parentRunId" not in runs["unrelated"]


def test_log_model_saves_the_mlmodel_layout_and_registers_a_version(vertex, tmp_path):
    _, info = _log(tmp_path, tags={"model_id": "v42"})
    d = tmp_path / "root" / "forecast-30d" / "run-1" / "artifacts" / "model"
    mlmodel = yaml.safe_load((d / "MLmodel").read_text())
    assert mlmodel["flavors"]["python_function"]["artifacts"] == {"factor": {"path": "artifacts/factor.txt"}}
    assert mlmodel["run_id"] == "run-1" and (d / "python_model.pkl").is_file()
    assert info.model_uri == "runs:/run-1/model" and info.registered_model_version == "1"
    mv = mlflow.MlflowClient().get_model_version("forecast_30d", 1)
    assert mv.source == str(d) and mv.run_id == "run-1"
    assert mv.tags == {"model_id": "v42", "mlflow_run_id": "run-1", "mlflow_experiment": "forecast-30d",
                       "git_commit": "abc123"}


def test_load_model_by_alias_version_and_run(vertex, tmp_path):
    _, info = _log(tmp_path)
    client = mlflow.MlflowClient()
    client.set_registered_model_alias("forecast_30d", "champion", info.registered_model_version)
    for uri in ("models:/forecast_30d@champion", "models:/forecast_30d/1", "runs:/run-1/model"):
        model = mlflow.pyfunc.load_model(uri)
        assert model.predict([1, 2]) == [2, 4], uri
        assert model.metadata.run_id == "run-1"
    assert isinstance(model.unwrap_python_model(), Doubler)


def test_an_alias_names_exactly_one_version(vertex, tmp_path):
    _log(tmp_path, "a")
    _log(tmp_path, "b")
    client = mlflow.MlflowClient()
    client.set_registered_model_alias("forecast_30d", "champion", "1")
    client.set_registered_model_alias("forecast_30d", "champion", 2)
    assert client.get_model_version_by_alias("forecast_30d", "champion").version == "2"
    assert client.get_registered_model("forecast_30d").aliases == {"champion": "2"}
    client.delete_registered_model_alias("forecast_30d", "champion")
    with pytest.raises(mlflow.MlflowException):
        client.get_model_version_by_alias("forecast_30d", "champion")


def test_version_tags_set_delete_and_search(vertex, tmp_path):
    _log(tmp_path, "a", tags={"model_id": "v1"})
    _log(tmp_path, "b", tags={"model_id": "v2"})
    client = mlflow.MlflowClient()
    client.set_model_version_tag("forecast_30d", "1", "serve_eligible", "true")
    assert [mv.version for mv in client.search_model_versions("name='forecast_30d'")] == ["2", "1"]
    hits = client.search_model_versions("name='forecast_30d' and tags.serve_eligible='true'")
    assert [mv.version for mv in hits] == ["1"]
    client.delete_model_version_tag("forecast_30d", "1", "serve_eligible")
    assert client.search_model_versions("name='forecast_30d' AND tags.serve_eligible='true'") == []


@pytest.mark.parametrize("tags", [{"Model_ID": "v1"}, {"model_id": "V1"}, {"model_id": "a.b"}, {"k": "x" * 64}])
def test_version_tags_must_be_valid_vertex_labels(vertex, tmp_path, tags):
    with pytest.raises(ValueError, match="Vertex labels"):
        _log(tmp_path, tags=tags)


def test_search_rejects_filters_it_cannot_run(vertex):
    with pytest.raises(mlflow.MlflowException, match="only name"):
        mlflow.MlflowClient().search_model_versions("name='forecast_30d' and version_number > 3")
    with pytest.raises(mlflow.MlflowException, match="needs name"):
        mlflow.MlflowClient().search_model_versions("tags.a='b'")


def test_register_model_from_a_run(vertex, tmp_path):
    with mlflow.start_run(run_name="r"):
        mlflow.pyfunc.log_model("model", python_model=Doubler(),
                                artifacts={"factor": str(tmp_path / "factor.txt")})
    mv = mlflow.register_model("runs:/r/model", "other", tags={"stage": "x"})
    assert (mv.version, mv.run_id, mv.tags["stage"]) == ("1", "r", "x")
    assert mlflow.pyfunc.load_model("models:/other/1").predict([3]) == [6]


def test_configuration_errors_say_what_to_set(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.setattr(mlflow, "_tracking_uri", None)
    with pytest.raises(RuntimeError, match="MLFLOW_TRACKING_URI"):
        mlflow.MlflowClient().get_model_version("m", 1)
    mlflow.set_tracking_uri("https://not-vertex")
    with pytest.raises(ValueError, match="vertex://"):
        mlflow.MlflowClient().get_model_version("m", 1)


class Broken(mlflow.pyfunc.PythonModel):
    def load_context(self, context):
        raise AttributeError("module 'sys' has no attribute 'intervals'")


def test_a_model_that_fails_to_load_under_another_python_says_so(tmp_path):
    mlflow.pyfunc.save_model(tmp_path / "m", Broken())
    path = tmp_path / "m" / "MLmodel"
    meta = yaml.safe_load(path.read_text())
    meta["flavors"]["python_function"]["python_version"] = "3.99.0"
    path.write_text(yaml.safe_dump(meta))
    with pytest.raises(AttributeError) as got:
        mlflow.pyfunc.load_model(str(tmp_path / "m"))
    assert "saved by Python 3.99.0" in " ".join(got.value.__notes__)
    meta["flavors"]["python_function"].pop("python_version")
    path.write_text(yaml.safe_dump(meta))
    with pytest.raises(AttributeError) as got:
        mlflow.pyfunc.load_model(str(tmp_path / "m"))
    assert not getattr(got.value, "__notes__", [])


class Summer(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input, params=None):
        return (model_input["a"] + model_input["b"]).tolist()


def test_a_signature_is_saved_as_mlflows_and_checked_on_predict(vertex, tmp_path):
    pd = pytest.importorskip("pandas")
    from gcpbricks.features import FeatureService

    df = pd.DataFrame({"a": [1.0, 2.0], "b": [3, 4], "key": ["x", "y"]})
    spec = FeatureService("summer", ["fs_segment:a", "fs_segment:b"], spine_inputs=["key"])
    with mlflow.start_run(run_name="sig"):
        info = mlflow.pyfunc.log_model("model", python_model=Summer(), registered_model_name="summer",
                                       signature=mlflow.models.infer_signature(df), feature_spec=spec)
    d = tmp_path / "root" / "forecast-30d" / "sig" / "artifacts" / "model"
    sig = yaml.safe_load((d / "MLmodel").read_text())["signature"]
    assert sig["outputs"] is None and sig["params"] is None
    assert yaml.safe_load(sig["inputs"]) == [{"type": "double", "name": "a", "required": True},
                                             {"type": "long", "name": "b", "required": True},
                                             {"type": "string", "name": "key", "required": True}]

    model = mlflow.pyfunc.load_model(f"models:/summer/{info.registered_model_version}")
    assert model.metadata.get_input_schema().input_names() == ["a", "b", "key"]
    assert model.metadata.feature_spec == spec
    assert model.predict(df.assign(extra=0)) == [4.0, 6.0]
    with pytest.raises(mlflow.MlflowException, match=r"missing inputs \['b'\]"):
        model.predict(df.drop(columns="b"))


def test_a_model_saved_without_a_signature_or_spec_loads_as_before(vertex, tmp_path):
    _, info = _log(tmp_path)
    model = mlflow.pyfunc.load_model(f"models:/forecast_30d/{info.registered_model_version}")
    assert model.metadata.get_input_schema() is None and model.metadata.feature_spec is None
    assert model.predict([3]) == [6]


# --- evaluate / validate_evaluation_results -------------------------------------------------

def _eval_frame():
    import pandas as pd

    return pd.DataFrame({"y": [1.0, 2.0, 3.0, 4.0], "p": [1.1, 1.9, 3.5, 3.0], "cost": [1.0, 5.0, 1.0, 2.0]})


def _cost_weighted_hits(predictions, targets, cost):
    hit = (abs(predictions - targets) / targets) <= 0.2
    return float((hit * cost).sum() / cost.sum() * 100)


def _agg(predictions, targets):
    return mlflow.metrics.MetricValue(aggregate_results={"mean": 1.5, "p90": 2.0, "skipped": None})


class Constant(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input, params=None):
        import pandas as pd

        return pd.DataFrame({"pred": [2.5] * len(model_input), "other": 0})


class DropsARow(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input, params=None):
        return [2.5] * (len(model_input) - 1)


def test_evaluate_computes_mlflows_regressor_and_custom_metrics_and_logs_them(vertex):
    wacc = mlflow.models.make_metric(eval_fn=_cost_weighted_hits, greater_is_better=True, name="wacc")
    agg = mlflow.models.make_metric(eval_fn=_agg, greater_is_better=True, name="agg")
    with mlflow.start_run(run_name="gate"):
        r = mlflow.evaluate(data=_eval_frame(), predictions="p", targets="y", model_type="regressor",
                            extra_metrics=[wacc, agg])
    # Values are real MLflow 3.14's on this frame.
    expected = {"example_count": 4, "max_error": 1.0, "mean_absolute_error": 0.425,
                "mean_absolute_percentage_error": 0.14166666666666670, "mean_on_target": 2.5,
                "mean_squared_error": 0.3175, "r2_score": 0.746, "root_mean_squared_error": 0.5634713834792322,
                "sum_on_target": 10.0, "wacc": 77.77777777777779, "agg/mean": 1.5, "agg/p90": 2.0}
    assert r.metrics.keys() == expected.keys()
    assert all(r.metrics[k] == pytest.approx(v, abs=1e-12) for k, v in expected.items())
    assert r.run_id == "gate" and r.artifacts == {}
    assert vertex.runs[("forecast-30d", "gate")]["metrics"]["wacc"] == pytest.approx(77.77777777777779)


def test_evaluate_without_an_active_run_logs_one_and_ends_it(vertex):
    r = mlflow.evaluate(data=_eval_frame(), predictions="p", targets="y", model_type="regressor")
    assert mlflow.active_run() is None
    assert vertex.runs[("forecast-30d", r.run_id)]["metrics"]["example_count"] == 4


def test_evaluate_scores_a_registered_model_by_uri(vertex):
    mlflow.pyfunc.log_model("model", python_model=Constant(), registered_model_name="m")
    mlflow.end_run()
    data = _eval_frame()[["cost", "y"]]
    r = mlflow.evaluate(model="models:/m/1", data=data, targets="y", predictions="pred", model_type="regressor")
    assert r.metrics["mean_absolute_error"] == pytest.approx(1.0)
    with pytest.raises(mlflow.MlflowException, match="multiple columns"):
        mlflow.evaluate(model="models:/m/1", data=data, targets="y", model_type="regressor")


def test_evaluate_refuses_predictions_not_one_per_row_or_nan(vertex):
    with pytest.raises(ValueError, match="inconsistent numbers of samples"):
        mlflow.evaluate(model=mlflow.pyfunc.PyFuncModel(DropsARow(), None, mlflow.pyfunc.ModelMetadata(None, None)),
                        data=_eval_frame()[["cost", "y"]], targets="y", model_type="regressor")
    with pytest.raises(ValueError, match="NaN"):
        mlflow.evaluate(data=_eval_frame().assign(y=[1.0, None, 3.0, 4.0]), predictions="p", targets="y",
                        model_type="regressor")


def test_a_metric_whose_argument_cannot_be_found_fails_naming_it(vertex):
    m = mlflow.models.make_metric(eval_fn=lambda predictions, revenue: 0.0, greater_is_better=True, name="m")
    with pytest.raises(mlflow.MlflowException, match=r"'m' requires the following: \['revenue'\]"):
        mlflow.evaluate(data=_eval_frame(), predictions="p", targets="y", extra_metrics=[m])


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"min_absolute_change": 0, "greater_is_better": True}, "`min_absolute_change` parameter must be a positive number."),
    ({"min_absolute_change": -1.0, "greater_is_better": True}, "`min_absolute_change` parameter must be a positive number."),
    ({"min_relative_change": 1, "greater_is_better": True}, "`min_relative_change` parameter must be a floating point number."),
    ({"min_relative_change": 1.5, "greater_is_better": True}, "`min_relative_change` parameter must be between 0 and 1."),
    ({"threshold": "1", "greater_is_better": True}, "`threshold` parameter must be a number."),
    ({"threshold": 1}, "`greater_is_better` parameter must be defined."),
    ({"greater_is_better": True}, "no threshold was specified."),
])
def test_metric_threshold_refuses_what_mlflow_refuses(kwargs, message):
    with pytest.raises(mlflow.MlflowException) as e:
        mlflow.models.MetricThreshold(**kwargs)
    assert str(e.value) == f"Could not instantiate MetricThreshold class: {message}"


def _results(**metrics):
    return mlflow.models.EvaluationResult(metrics, {})


def test_validate_passes_and_fails_with_mlflows_messages():
    T = mlflow.models.MetricThreshold
    cand, base = _results(wacc=77.5, mae=0.4), _results(wacc=100.0, mae=0.0)
    assert mlflow.validate_evaluation_results({"wacc": T(threshold=50, greater_is_better=True)}, cand) is None
    cases = [
        ({"wacc": T(threshold=90, greater_is_better=True)}, None,
         "Metric wacc value threshold check failed: candidate model wacc = 77.5, wacc threshold = 90."),
        ({"wacc": T(min_absolute_change=1, greater_is_better=True)}, base,
         ("Metric wacc minimum absolute change check failed: candidate model wacc = 77.5, baseline model "
         "wacc = 100.0, wacc minimum absolute change threshold = 1.")),
        ({"wacc": T(min_absolute_change=1, greater_is_better=True)}, None,
         "Model comparison failed: metric wacc was missing from the evaluation result of the baseline model."),
        ({"nope": T(threshold=1, greater_is_better=True)}, None,
         "Metric validation failed: metric nope was missing from the evaluation result of the candidate model."),
        ({"wacc": T(min_relative_change=0.1, greater_is_better=True), "mae": T(threshold=0.01, greater_is_better=False)},
         base, ("Metric wacc minimum relative change check failed: candidate model wacc = 77.5, baseline model "
         "wacc = 100.0, wacc minimum relative change threshold = 0.1.\nMetric mae value threshold check failed: "
         "candidate model mae = 0.4, mae threshold = 0.01.")),
    ]
    for thresholds, baseline, message in cases:
        with pytest.raises(mlflow.models.ModelValidationFailedException) as e:
            mlflow.validate_evaluation_results(thresholds, cand, baseline)
        assert str(e.value) == message
    # A zero baseline falls back to "strictly better", as MLflow's.
    with pytest.raises(mlflow.models.ModelValidationFailedException):
        mlflow.validate_evaluation_results({"mae": T(min_relative_change=0.1, greater_is_better=False)},
                                           _results(mae=0.0), _results(mae=0.0))


def test_matches_real_mlflow(tmp_path, monkeypatch):
    """The same frame and thresholds through real MLflow, where it is installed."""
    import logging
    import warnings

    real = pytest.importorskip("mlflow")
    pytest.importorskip("sklearn")
    from mlflow.models import MetricThreshold as RealT
    from mlflow.models import make_metric as real_make

    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    real.set_tracking_uri(f"sqlite:///{tmp_path}/m.db")
    frame = _eval_frame()
    theirs = real.evaluate(data=frame, predictions="p", targets="y", model_type="regressor",
                           extra_metrics=[real_make(eval_fn=_cost_weighted_hits, greater_is_better=True, name="wacc")])

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": FakeVertex()})
    monkeypatch.setattr(mlflow, "_runs", [])
    mlflow.set_experiment("parity")
    ours = mlflow.evaluate(data=frame, predictions="p", targets="y", model_type="regressor",
                           extra_metrics=[mlflow.models.make_metric(eval_fn=_cost_weighted_hits,
                                                                    greater_is_better=True, name="wacc")])
    assert ours.metrics.keys() == theirs.metrics.keys()
    assert all(float(ours.metrics[k]) == pytest.approx(float(v), abs=1e-12) for k, v in theirs.metrics.items())

    for kw in [{"threshold": 90}, {"threshold": 50}, {"min_absolute_change": 0.5}, {"min_relative_change": 0.01}]:
        outcomes = []
        for T, validate, cand, base in [
            (RealT, real.validate_evaluation_results, theirs, theirs),
            (mlflow.models.MetricThreshold, mlflow.validate_evaluation_results, ours, ours),
        ]:
            try:
                validate({"wacc": T(greater_is_better=True, **kw)}, cand, base)
                outcomes.append("PASS")
            except Exception as e:  # noqa: BLE001 -- the message is what's compared
                outcomes.append(str(e))
        assert outcomes[0] == outcomes[1], kw


# --- what a model reads and is fit on: features.json -------------------------------------------

def _features(tmp_path, run):
    import json
    return json.loads((tmp_path / "root" / "forecast-30d" / run / "artifacts" / "model" / "features.json").read_text())


def test_a_signature_and_a_feature_spec_say_what_a_model_reads_and_is_fit_on(vertex, tmp_path):
    pd = pytest.importorskip("pandas")
    from gcpbricks.features import FeatureService

    df = pd.DataFrame({"a": [1.0], "b": [3]})
    spec = FeatureService("summer", ["fs_segment:a", "fs_segment:b", "fs_segment:c"], spine_inputs=["key"])
    with mlflow.start_run(run_name="feats"):
        mlflow.pyfunc.log_model("model", python_model=Summer(), signature=mlflow.models.infer_signature(df),
                                feature_spec=spec)
    got = _features(tmp_path, "feats")
    assert got["read"] == ["a", "b", "c", "key"] and got["model_inputs"] == {"model": ["a", "b"]}
    assert got["counts"] == {"read": 4, "model_inputs": {"model": 2}, "computed": None}
    assert got["sources"] == {"read": "feature service summer", "model_inputs": "MLmodel signature"}
    params = vertex.runs[("forecast-30d", "feats")]["params"]
    assert (params["n_features_read"], params["n_model_inputs"]) == (4, 2)


def test_a_feature_pipeline_says_what_it_fetches_computes_and_feeds_each_input_set(vertex, tmp_path):
    pytest.importorskip("sklearn")
    from gcpbricks.feature_manager import FeaturePipeline

    pipe = FeaturePipeline({"define": [{"name": "spend_x2", "expr": "spend * 2"}],
                            "inputs": {"default": ["spend_x2", "signups"]}})
    with mlflow.start_run(run_name="pipe"):
        mlflow.pyfunc.log_model("model", python_model=Summer(), features=pipe)
    got = _features(tmp_path, "pipe")
    assert set(got["read"]) == {"spend", "signups"} and got["computed"] == ["spend_x2"]
    assert got["model_inputs"] == {"default": ["spend_x2", "signups"]}
    assert vertex.runs[("forecast-30d", "pipe")]["params"]["n_features_computed"] == 1


def test_a_model_logged_by_hand_logs_its_features_beside_it(vertex, tmp_path):
    with mlflow.start_run(run_name="torch"):
        mlflow.log_features(read=["level", "colors"], model_inputs={"policy": ["tokens"], "value": ["tokens", "level"]})
    got = _features(tmp_path, "torch")
    assert got["counts"]["model_inputs"] == {"policy": 1, "value": 2} and got["sources"]["read"] == "logged"
    assert vertex.runs[("forecast-30d", "torch")]["params"]["n_model_inputs"] == 2
    with pytest.raises(ValueError, match="needs read"), mlflow.start_run(run_name="empty"):
        mlflow.log_features()


# --- what a run trained on: training_summary.json ----------------------------------------------

def _frames():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({"spend": rng.uniform(0, 10, n), "country": rng.choice(["US", "JP", "VN"], n),
                       "snapshot_date": pd.date_range("2026-10-01", periods=n, freq="h")})
    df.loc[::10, "spend"] = None
    df["paid"] = (df["spend"].fillna(0) > 5).astype(int)
    return df.iloc[:300], df.iloc[300:]


def test_a_training_summary_holds_the_splits_feature_stats_label_and_importances():
    import json
    sklearn_tree = pytest.importorskip("sklearn.tree")
    train, valid = _frames()
    model = sklearn_tree.DecisionTreeClassifier(max_depth=2, random_state=0).fit(train[["spend"]].fillna(0), train["paid"])
    s = mlflow.training_summary(train=train, valid=valid, label="paid", label_definition="paid within 7 days",
                                time_column="snapshot_date", features=["spend", "country"], model=model,
                                query="SELECT *  FROM t", snapshot="gs://b/snap")
    json.dumps(s, allow_nan=False)  # no NaN reaches the file
    assert s["data"]["splits"]["train"]["rows"] == 300 and s["data"]["splits"]["valid"]["from"].startswith("2026-10-13")
    assert s["data"]["source"]["snapshot"] == "gs://b/snap" and len(s["data"]["source"]["query_sha256"]) == 16
    spend, country = s["features"]["columns"]["spend"], s["features"]["columns"]["country"]
    assert spend["kind"] == "numeric" and spend["null_rate"] == pytest.approx(0.1) and spend["min"] >= 0
    assert country["kind"] == "categorical" and country["distinct"] == 3 and len(country["top"]) == 3
    assert s["label"]["kind"] == "binary" and 0 < s["label"]["splits"]["valid"]["positive_rate"] < 1
    assert s["importance"]["top"][0] == {"name": "spend", "value": pytest.approx(1.0), "share": pytest.approx(1.0)}
    assert s["execution"] is None


def test_a_large_frame_is_described_from_a_sample():
    train, _ = _frames()
    s = mlflow.training_summary(train=train, features=["spend"], max_rows=50)
    assert s["features"]["sampled"] and s["features"]["rows_used"] == 50 and s["label"] is None


def test_log_model_saves_the_training_summary_and_counts_rows_as_params(vertex, tmp_path):
    import json
    train, valid = _frames()
    s = mlflow.training_summary(train=train, valid=valid, label="paid", features=["spend"])
    with mlflow.start_run(run_name="trained"):
        mlflow.pyfunc.log_model("model", python_model=Summer(), training=s)
    saved = json.loads((tmp_path / "root" / "forecast-30d" / "trained" / "artifacts" / "model" / "training_summary.json").read_text())
    assert saved["data"]["splits"]["valid"]["rows"] == 100
    params = vertex.runs[("forecast-30d", "trained")]["params"]
    assert (params["n_train_rows"], params["n_valid_rows"]) == (300, 100) and 0 < params["label_positive_rate"] < 1
    with mlflow.start_run(run_name="by-hand"):
        mlflow.log_training(s)
    assert vertex.runs[("forecast-30d", "by-hand")]["params"]["n_train_rows"] == 300


def _clf_frame():
    import pandas as pd
    return pd.DataFrame({"p": [1, 0, 1, 1, 0, 0, 1, 0], "y": [1, 0, 0, 1, 1, 0, 1, 0]})


def test_a_binary_classifier_gets_mlflows_label_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": FakeVertex()})
    monkeypatch.setattr(mlflow, "_runs", [])
    mlflow.set_experiment("clf")
    m = mlflow.evaluate(data=_clf_frame(), predictions="p", targets="y", model_type="classifier").metrics
    # tp 3, fp 1, fn 1, tn 3
    assert (m["true_positives"], m["false_positives"], m["false_negatives"], m["true_negatives"]) == (3, 1, 1, 3)
    assert m["accuracy_score"] == 0.75 and m["precision_score"] == 0.75 and m["recall_score"] == 0.75
    assert m["f1_score"] == pytest.approx(0.75) and m["example_count"] == 8


def test_a_classifier_refuses_a_non_binary_target(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": FakeVertex()})
    monkeypatch.setattr(mlflow, "_runs", [])
    with pytest.raises(mlflow.MlflowException, match="binary"):
        mlflow.evaluate(data=_clf_frame().assign(y=[2, 0, 1, 1, 0, 0, 1, 0]), predictions="p", targets="y",
                        model_type="classifier")


def test_classifier_matches_real_mlflow(tmp_path):
    import logging
    import warnings

    real = pytest.importorskip("mlflow")
    pytest.importorskip("sklearn")
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    real.set_tracking_uri(f"sqlite:///{tmp_path}/m.db")
    theirs = real.evaluate(data=_clf_frame(), predictions="p", targets="y", model_type="classifier").metrics
    from gcpbricks.mlflow._evaluation import _classifier_metrics
    frame = _clf_frame()
    ours = _classifier_metrics(frame["y"].to_numpy(float), frame["p"].to_numpy(float))
    shared = set(ours) & set(theirs)
    assert {"accuracy_score", "precision_score", "recall_score", "f1_score", "example_count"} <= shared
    assert all(float(ours[k]) == pytest.approx(float(theirs[k])) for k in shared)


def _weighted_frame():
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(0)
    y = rng.normal(10, 3, 50)
    p = y + rng.normal(0, 1, 50)
    w = rng.uniform(0, 3, 50)
    return pd.DataFrame({"p": p, "y": y}), w


def test_sample_weights_weigh_what_mlflow_weighs():
    """The numbers real MLflow 3.1.4 gives on this frame and weights: max_error and
    example_count stay unweighted, sum_on_target is sum(w * y) and mean_on_target that / n."""
    from gcpbricks.mlflow._evaluation import _classifier_metrics, _regressor_metrics
    frame, w = _weighted_frame()
    m = _regressor_metrics(frame["y"].to_numpy(float), frame["p"].to_numpy(float), w)
    expected = {"example_count": 50, "max_error": 2.20351, "mean_absolute_error": 0.833856,
                "mean_absolute_percentage_error": 0.093219, "mean_on_target": 15.51187,
                "mean_squared_error": 1.006655, "r2_score": 0.86701, "root_mean_squared_error": 1.003322,
                "sum_on_target": 775.593513}
    assert {k: round(v, 6) for k, v in m.items()} == pytest.approx(expected, abs=1e-6)
    c = _clf_frame()
    mc = _classifier_metrics(c["y"].to_numpy(float), c["p"].to_numpy(float),
                             np.array([1, 2, 1, 1, 3, 1, 1, 1.0]))
    assert (round(mc["accuracy_score"], 4), mc["precision_score"], mc["recall_score"], round(mc["f1_score"], 4)) == \
        (0.6364, 0.75, 0.5, 0.6)
    assert (mc["true_positives"], mc["false_negatives"]) == (3, 1)


def test_sample_weights_need_one_value_per_row(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "vertex://p/us-central1")
    monkeypatch.setattr(mlflow, "_backends", {"vertex://p/us-central1": FakeVertex()})
    monkeypatch.setattr(mlflow, "_runs", [])
    with pytest.raises(mlflow.MlflowException, match="one per row"):
        mlflow.evaluate(data=_clf_frame(), predictions="p", targets="y", model_type="classifier",
                        evaluator_config={"sample_weights": [1.0, 2.0]})

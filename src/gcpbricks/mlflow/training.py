"""What a training run trained on, summarized where the data already is (the training machine,
in the training process) and saved beside the model as training_summary.json, so the console's
model version page shows it and later checks (drift, a retrain's diff) have a baseline:

    data        each split's rows and time range, and where it came from (a snapshot, a
                query's hash)
    features    each feature's null rate and distribution at training time (on a sample of a
                large frame): the baseline a serving drift check compares to
    label       its kind and, per split, its positive rate or distribution
    importance  the fitted model's feature importances, top first
    execution   the Cloud Run execution or Vertex AI custom job the run ran in: the console
                prices it (what training cost, how long it took), the machine computes nothing

    summary = mlflow.training_summary(train=train_df, valid=valid_df, label="forecast_90d",
                                      time_column="snapshot_date", model=estimator, snapshot=uri)
    mlflow.pyfunc.log_model("model", python_model=..., training=summary)
    mlflow.log_training(summary)        # a model logged by hand
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from pathlib import Path

FILE = "training_summary.json"
#: Rows a feature's statistics are read from; a larger frame is sampled (seeded).
STATS_ROWS = 200_000
#: Importances kept, top first.
TOP = 50
_QUANTILES = (0.01, 0.25, 0.5, 0.75, 0.99)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _iso(v) -> str | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)


def _split(df, time_column: str | None) -> dict:
    out: dict = {"rows": len(df)}
    if time_column and time_column in df.columns and len(df):
        out["from"], out["to"] = _iso(df[time_column].min()), _iso(df[time_column].max())
    return out


def feature_stats(df, columns: list[str], max_rows: int = STATS_ROWS, seed: int = 0) -> dict:
    """Each column's null rate and distribution: numbers' mean, std, min, quantiles, max; times'
    range; anything else its distinct count and top values with their shares."""
    import pandas as pd

    sample = df.sample(max_rows, random_state=seed) if len(df) > max_rows else df
    out = {}
    for c in columns:
        s = sample[c]
        null = _num(s.isna().mean()) or 0.0
        if pd.api.types.is_bool_dtype(s):
            out[c] = {"kind": "boolean", "null_rate": null, "true_rate": _num(s.dropna().astype(float).mean())}
        elif pd.api.types.is_numeric_dtype(s):
            q = s.quantile(list(_QUANTILES)) if s.notna().any() else pd.Series([None] * len(_QUANTILES), index=_QUANTILES)
            out[c] = {"kind": "numeric", "null_rate": null, "mean": _num(s.mean()), "std": _num(s.std()),
                      "min": _num(s.min()), **{f"p{int(k * 100):02d}": _num(q[k]) for k in _QUANTILES}, "max": _num(s.max())}
        elif pd.api.types.is_datetime64_any_dtype(s):
            out[c] = {"kind": "datetime", "null_rate": null, "min": _iso(s.min()), "max": _iso(s.max())}
        else:
            vc = s.astype("string").value_counts(normalize=True, dropna=True)
            out[c] = {"kind": "categorical", "null_rate": null, "distinct": int(s.nunique(dropna=True)),
                      "top": [[str(k), _num(v)] for k, v in vc.head(5).items()]}
    return {"rows_used": len(sample), "sampled": len(sample) < len(df), "columns": out}


def label_summary(splits: dict, label: str, definition: str | None = None) -> dict:
    """The label's kind and, per split, its positive rate (binary), distribution (numeric) or top
    classes (anything else)."""
    import pandas as pd

    first = next(iter(splits.values()))[label]
    values = set(first.dropna().unique().tolist()[:3])
    kind = ("binary" if len(values) <= 2 and values <= {0, 1}
            else "numeric" if pd.api.types.is_numeric_dtype(first) else "categorical")
    per = {}
    for name, df in splits.items():
        y = df[label]
        if kind == "binary":
            per[name] = {"positive_rate": _num(y.astype(float).mean()), "null_rate": _num(y.isna().mean())}
        elif kind == "numeric":
            per[name] = {"mean": _num(y.mean()), "std": _num(y.std()), "min": _num(y.min()), "p50": _num(y.median()),
                         "max": _num(y.max()), "zero_rate": _num((y == 0).mean()), "null_rate": _num(y.isna().mean())}
        else:
            vc = y.astype("string").value_counts(normalize=True)
            per[name] = {"top": [[str(k), _num(v)] for k, v in vc.head(10).items()]}
    return {"name": label, "kind": kind, "definition": definition, "splits": per}


def importance(model, names: list[str] | None = None, top: int = TOP) -> dict | None:
    """A fitted model's feature importances, top first, as shares of their sum: sklearn's
    feature_importances_ (LightGBM's and XGBoost's sklearn models too), a LightGBM Booster's gain,
    an XGBoost Booster's gain, or a linear model's |coef_|. None for anything else."""
    import numpy as np

    kind, vals, got = None, None, None
    if hasattr(model, "feature_importances_"):
        kind, vals = "feature_importances_", np.asarray(model.feature_importances_, dtype=float)
        got = getattr(model, "feature_names_in_", None)
        got = list(got) if got is not None else getattr(model, "feature_name_", None)
    elif hasattr(model, "feature_importance") and hasattr(model, "feature_name"):
        kind, vals, got = "gain", np.asarray(model.feature_importance(importance_type="gain"), dtype=float), model.feature_name()
    elif hasattr(model, "get_score"):
        score = model.get_score(importance_type="gain")
        kind, got, vals = "gain", list(score), np.asarray(list(score.values()), dtype=float)
    elif hasattr(model, "coef_"):
        coef = np.abs(np.asarray(model.coef_, dtype=float))
        kind, vals = "|coef_|", coef.mean(0) if coef.ndim > 1 else coef  # a multiclass model: over its classes
        got = getattr(model, "feature_names_in_", None)
        got = list(got) if got is not None else None
    if vals is None:
        return None
    labels = list(got) if got is not None else (names or [f"f{i}" for i in range(len(vals))])
    if len(labels) != len(vals):
        return None
    total = float(vals.sum()) or 1.0
    order = sorted(range(len(vals)), key=lambda i: -vals[i])[:top]
    return {"kind": kind, "features": len(vals),
            "top": [{"name": str(labels[i]), "value": _num(vals[i]), "share": _num(vals[i] / total)} for i in order]}


def summarize(*, train, valid=None, test=None, label: str | None = None, label_definition: str | None = None,
              time_column: str | None = None, features: list[str] | None = None, model=None,
              snapshot: str | None = None, query: str | None = None, max_rows: int = STATS_ROWS) -> dict:
    """training_summary.json's content, from the DataFrames the run trained on. `features`:
    the columns to describe (default: train's, less the label and the time column)."""
    from gcpbricks.job_labels import execution

    splits = {k: v for k, v in (("train", train), ("valid", valid), ("test", test)) if v is not None}
    cols = features or [c for c in train.columns if c not in (label, time_column)]
    source = {k: v for k, v in {"snapshot": snapshot,
                                "query_sha256": hashlib.sha256(" ".join(query.split()).encode()).hexdigest()[:16]
                                if query else None}.items() if v}
    return {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "execution": execution(),
            "data": {"splits": {k: _split(v, time_column) for k, v in splits.items()}, "time_column": time_column,
                     "source": source},
            "features": feature_stats(train, cols, max_rows),
            "label": label_summary(splits, label, label_definition) if label and label in train.columns else None,
            "importance": importance(model, cols) if model is not None else None}


def save(summary: dict, model_dir: str | Path) -> Path:
    path = Path(model_dir) / FILE
    path.write_text(json.dumps(summary, indent=1))
    return path


def params(summary: dict) -> dict:
    """The run params an experiment's runs compare on: rows per split, the label's positive rate."""
    out = {f"n_{k}_rows": v["rows"] for k, v in summary["data"]["splits"].items()}
    lab = summary.get("label") or {}
    if lab.get("kind") == "binary" and "train" in lab.get("splits", {}):
        out["label_positive_rate"] = lab["splits"]["train"]["positive_rate"]
    return out


def log_training(summary: dict, artifact_path: str = "model") -> None:
    """For a model logged without pyfunc.log_model: saves training_summary.json under the active
    run's artifacts at `artifact_path` (next to the model) and logs its params."""
    from . import _require_run, _storage, log_params

    run = _require_run()
    with tempfile.TemporaryDirectory() as tmp:
        save(summary, tmp)
        _storage.upload_tree(Path(tmp), _storage.join(run.info.artifact_uri, artifact_path))
    log_params(params(summary))

"""A vertex_pipeline_task's pipeline, written in the bundle and compiled by `mlops bundle
deploy` (the `[pipelines]` extra: kfp 2):

    vertex_pipeline_task:
      python_file: training/pipeline.py     # a function decorated with @dsl.pipeline
      function: pipeline                    # its name, default "pipeline"
      base_parameters: {model: forecast_30d}    # the published parameter values

Steps run the task's environment image and import the project's code from the bundle:

    from kfp import dsl
    from gcpbricks import pipelines

    @dsl.component(base_image=pipelines.image())
    def train(model: str) -> str:
        from training.train import fit          # a bundle file, on sys.path
        return fit(model)

Deploy compiles with MLOPS_PIPELINE_IMAGE set to the environment's image, then sets
MLOPS_BUNDLE_FILES and the MLflow env on every step container that runs it. The runtime
image's sitecustomize then puts the run's deploy's files on sys.path when Python starts
(use_bundle), as Databricks runs a job's code from the bundle. Which deploy is the run's
label mlops_bundle_version (workflows/vertex_pipeline.yaml), not the spec's env: a step's
spec is its cache key, so a repair after a deploy reuses what succeeded (a ParallelFor's
iterations included) and runs the rest on the new code, as Databricks repairs. A step that
is not a container of that image (a CustomJob's worker pool) takes step_env() into its own env.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

#: The env step_env() hands on: where the bundle's files are and where MLflow tracks. Not
#: which deploy (MLOPS_BUNDLE_VERSION): that is the run's label, outside the cache key.
STEP_ENV = ("MLOPS_BUNDLE_FILES", "MLFLOW_TRACKING_URI", "MLFLOW_ARTIFACT_ROOT")
#: The run label naming the deploy a pipeline run's steps read (workflows/vertex_pipeline.yaml).
VERSION_LABEL = "mlops_bundle_version"


def image() -> str:
    """The task environment's image, the base_image of the pipeline's components."""
    img = os.environ.get("MLOPS_PIPELINE_IMAGE")
    if not img:
        raise RuntimeError("MLOPS_PIPELINE_IMAGE is unset: pipelines compile under `mlops bundle "
                           "deploy`, which sets it to the task's environment image")
    return img


def step_env() -> dict[str, str]:
    """The env a step needs to run bundle code, for a step deploy can't set it on itself."""
    return {k: os.environ[k] for k in STEP_ENV if os.environ.get(k)}


def run_label(key: str) -> str | None:
    """A label of the Vertex pipeline run this step belongs to (Vertex hands every step,
    custom jobs included, its run's labels as VERTEX_AI_PIPELINES_RUN_LABELS)."""
    try:
        return json.loads(os.environ.get("VERTEX_AI_PIPELINES_RUN_LABELS") or "{}").get(key) or None
    except ValueError:
        return None


def use_bundle() -> str | None:
    """With MLOPS_BUNDLE_FILES set: fetch that deploy's files once per container and put
    them first on sys.path (and PYTHONPATH, for child processes). Returns the root."""
    uri = os.environ.get("MLOPS_BUNDLE_FILES")
    if not uri:
        return None
    from gcpbricks import files

    version = os.environ.get("MLOPS_BUNDLE_VERSION") or run_label(VERSION_LABEL)
    root = Path(tempfile.gettempdir()) / "mlops-bundle" / (version or "current")
    if not (root / ".fetched").exists():
        root.mkdir(parents=True, exist_ok=True)
        files.fetch(uri, root, version=version)
        (root / ".fetched").write_text(version or "")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if str(root) not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), *paths]))
    return str(root)


def _load(root: Path, python_file: str, function: str):
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("mlops_bundle_pipeline", root / python_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, function, None)
    if fn is None:
        raise SystemExit(f"{python_file} has no {function!r}")
    return fn


def inject_env(spec: dict, image_ref: str, env: dict[str, str]) -> int:
    """Adds env to every executor container running image_ref (its own entries win).
    Returns how many."""
    n = 0
    for executor in spec.get("deploymentSpec", {}).get("executors", {}).values():
        container = executor.get("container")
        if not container or container.get("image") != image_ref:
            continue
        own = {e["name"] for e in container.get("env", [])}
        container["env"] = [*container.get("env", []),
                            *({"name": k, "value": v} for k, v in env.items() if k not in own)]
        n += 1
    return n


def compile_pipeline(root: Path, python_file: str, function: str, out: Path) -> dict:
    """Compiles the bundle's pipeline function to out (a PipelineSpec JSON) with its steps'
    env set; returns the spec."""
    from kfp import compiler

    fn = _load(root, python_file, function)
    compiler.Compiler().compile(fn, str(out))
    spec = json.loads(out.read_text())
    if not inject_env(spec, image(), step_env()):
        print(f"warning: no step of {python_file}:{function} runs {image()}; steps can't import "
              "the bundle's code without pipelines.step_env()", file=sys.stderr)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True))
    return spec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gcpbricks.pipelines")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile")
    c.add_argument("--root", type=Path, required=True)
    c.add_argument("--python-file", required=True)
    c.add_argument("--function", default="pipeline")
    c.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    compile_pipeline(a.root, a.python_file, a.function, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

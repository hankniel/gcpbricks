"""Publish a job's built pipelines where its runs read them:

    gs://<bucket>/<base>/<version>/<name>_pipeline.json + <name>_params.json
    gs://<bucket>/<base>/current.json -> {"version", "image", "<name>": {"spec", "params"}}

current.json is written last, so a run that reads it never pairs one version's spec with
another's parameters.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


def pointer(version: str, image: str, uri_base: str, names: list[str]) -> dict:
    """current.json's body. `uri_base` is gs://<bucket>/<base>."""
    return {"version": version, "image": image,
            **{n: {"spec": f"{uri_base}/{version}/{n}_pipeline.json",
                   "params": f"{uri_base}/{version}/{n}_params.json"} for n in names}}


def built_names(build_dir: Path) -> list[str]:
    """Pipelines a build wrote: every <name>_pipeline.json that has its <name>_params.json."""
    names = sorted(p.name.removesuffix("_pipeline.json") for p in build_dir.glob("*_pipeline.json"))
    missing = [n for n in names if not (build_dir / f"{n}_params.json").is_file()]
    if missing:
        raise FileNotFoundError(f"{build_dir}: no params file for {missing}")
    if not names:
        raise FileNotFoundError(f"{build_dir}: the build wrote no <name>_pipeline.json")
    return names


def publish(build_dir: Path, *, bucket: str, base: str, version: str, image: str,
            out_dir: Path | None = None) -> dict:
    """Upload a build's files, then current.json; with `out_dir`, write the same tree there
    instead of to GCS. Returns the pointer."""
    names = built_names(build_dir)
    body = pointer(version, image, f"gs://{bucket}/{base}", names)
    files = [(build_dir / f"{n}_{kind}.json", f"{base}/{version}/{n}_{kind}.json")
             for n in names for kind in ("pipeline", "params")]
    current = Path(tempfile.mkdtemp()) / "current.json"
    current.write_text(json.dumps(body, indent=2, sort_keys=True))
    files.append((current, f"{base}/current.json"))

    if out_dir:
        for src, dest in files:
            target = out_dir / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, target)
        return body

    from google.cloud import storage

    blobs = storage.Client().bucket(bucket)
    for src, dest in files:
        blobs.blob(dest).upload_from_filename(str(src), content_type="application/json")
    return body

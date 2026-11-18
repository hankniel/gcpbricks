"""Publish a job's built pipelines where its runs read them:

    gs://<bucket>/<base>/<version>/<name>_pipeline.json + <name>_params.json
    gs://<bucket>/<base>/current.json -> {"version", "bundle_version", "image", "<name>": {"spec", "params"}}

bundle_version is the bundle files' deploy the steps run: the runner labels each run with it
(pipelines.use_bundle reads it back), so it stays the build's through a rollout's re-point.

current.json is written last, so a run that reads it never pairs one version's spec with
another's parameters.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

#: current.json's keys that are not a pipeline's name.
POINTER_KEYS = ("version", "bundle_version", "image")


def pointer(version: str, image: str, uri_base: str, names: list[str], bundle_version: str | None = None) -> dict:
    """current.json's body. `uri_base` is gs://<bucket>/<base>."""
    return {"version": version, "bundle_version": bundle_version or version, "image": image,
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


def repoint(read, *, bucket: str, base: str, version: str, image: str,
            out_dir: Path | None = None) -> dict | None:
    """The published pipelines under `base` re-published to run `image`: each spec with its
    current image swapped for it, under <version>_<image tag>, the files before unchanged.
    A rollout's environment reaches the pipeline's steps so, with no recompile. `read(object)`
    returns an object's bytes and raises FileNotFoundError for a missing one. None when
    nothing is published there or it already runs `image`."""
    try:
        current = json.loads(read(f"{base}/current.json"))
    except FileNotFoundError:
        return None
    old = current.get("image")
    if not old or old == image:
        return None
    build_dir, gs = Path(tempfile.mkdtemp()), f"gs://{bucket}/"
    for name in [k for k in current if k not in POINTER_KEYS]:
        spec = read(current[name]["spec"].removeprefix(gs)).decode()
        (build_dir / f"{name}_pipeline.json").write_text(spec.replace(json.dumps(old), json.dumps(image)))
        (build_dir / f"{name}_params.json").write_bytes(read(current[name]["params"].removeprefix(gs)))
    # A pointer from before bundle_version: its version is a deploy's files version, or "<that>_<tag>".
    built = current.get("bundle_version") or current["version"].split("_", 1)[0]
    return publish(build_dir, bucket=bucket, base=base, version=f"{version}_{image.rsplit(':', 1)[-1]}",
                   image=image, out_dir=out_dir, bundle_version=built)


def publish(build_dir: Path, *, bucket: str, base: str, version: str, image: str,
            out_dir: Path | None = None, bundle_version: str | None = None) -> dict:
    """Upload a build's files, then current.json; with `out_dir`, write the same tree there
    instead of to GCS. Returns the pointer."""
    names = built_names(build_dir)
    body = pointer(version, image, f"gs://{bucket}/{base}", names, bundle_version)
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

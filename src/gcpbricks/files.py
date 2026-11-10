"""The bundle's files, as `mlops bundle deploy` uploads them and a dbt_task reads them.

    <files_uri>/<version>.tar.gz     the files git tracks or doesn't ignore, plus
                                     sync.include, minus sync.exclude
    <files_uri>/current.json         {"version", "archive"}: written last, read by each run

So a run always reads one whole deploy, and a deploy in flight never shows half its files.
"""
from __future__ import annotations

import fnmatch
import io
import json
import subprocess
import tarfile
from pathlib import Path


def bundle_files(root: Path, include: list[str], exclude: list[str]) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                            cwd=root, capture_output=True, text=True, check=True).stdout.split("\n")
    files = {f for f in listed if f and (root / f).is_file()}
    for pattern in include:
        files |= {str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file()}
    return sorted(f for f in files if not any(fnmatch.fnmatch(f, x) for x in exclude))


def archive(root: Path, files: list[str]) -> bytes:
    """A tar.gz of `files` whose bytes depend only on their paths and contents."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for f in files:
            data = (root / f).read_bytes()
            info = tarfile.TarInfo(f)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _split(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri.removeprefix("gs://").partition("/")
    return bucket, prefix


def upload(uri: str, version: str, data: bytes, out_dir: Path | None = None) -> dict:
    """Writes <uri>/<version>.tar.gz, then current.json. out_dir: write the tree under it
    instead of GCS, at <out_dir>/<bucket>/<prefix>/..."""
    bucket, prefix = _split(uri)
    pointer = {"version": version, "archive": f"{uri}/{version}.tar.gz"}
    objects = [(f"{prefix}/{version}.tar.gz", data, "application/gzip"),
               (f"{prefix}/current.json", json.dumps(pointer, indent=2).encode(), "application/json")]
    if out_dir is not None:
        for name, body, _ in objects:
            path = out_dir / bucket / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return pointer
    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    for name, body, content_type in objects:
        b.blob(name).upload_from_string(body, content_type=content_type)
    return pointer


def runs_uri(files_uri: str, execution: str) -> str:
    """Where a task's execution keeps what the console reads back about it (a dbt_task's
    run_results): beside the bundle's files, under runs/<execution>."""
    return f"{files_uri.removesuffix('/files')}/runs/{execution}"


def write_json(uri: str, obj: dict, local_root: Path | None = None) -> None:
    """Writes obj at gs://bucket/name, or under local_root as upload(out_dir=) lays it out."""
    bucket, name = _split(uri)
    body = json.dumps(obj).encode()
    if local_root is not None:
        path = local_root / bucket / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return
    from google.cloud import storage

    storage.Client().bucket(bucket).blob(name).upload_from_string(body, content_type="application/json")


def put_file(uri: str, path: Path | None, local_root: Path | None = None) -> None:
    """Writes the file at path to gs://bucket/name; path None deletes that object."""
    bucket, name = _split(uri)
    if local_root is not None:
        dest = local_root / bucket / name
        if path is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
        else:
            dest.unlink(missing_ok=True)
        return
    from google.cloud import storage

    blob = storage.Client().bucket(bucket).blob(name)
    if path is not None:
        blob.upload_from_filename(str(path))
    elif blob.exists():
        blob.delete()


def get_file(uri: str, path: Path, local_root: Path | None = None) -> bool:
    """Copies gs://bucket/name to path; False, writing nothing, when there is no such object."""
    bucket, name = _split(uri)
    if local_root is not None:
        source = local_root / bucket / name
        if not source.exists():
            return False
        data = source.read_bytes()
    else:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(name)
        if not blob.exists():
            return False
        data = blob.download_as_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def fetch(uri: str, dest: Path, local_root: Path | None = None, version: str | None = None) -> str:
    """Extracts the current deploy under `uri` (or that `version` of it) into `dest`; returns
    its version. local_root: read the tree `upload(out_dir=local_root)` wrote, not GCS."""
    bucket, prefix = _split(uri)
    if local_root is not None:
        def read(name: str) -> bytes:
            return (local_root / bucket / name).read_bytes()
    else:
        from google.cloud import storage

        b = storage.Client().bucket(bucket)

        def read(name: str) -> bytes:
            return b.blob(name).download_as_bytes()
    version = version or json.loads(read(f"{prefix}/current.json"))["version"]
    with tarfile.open(fileobj=io.BytesIO(read(f"{prefix}/{version}.tar.gz"))) as tar:
        tar.extractall(dest, filter="data")
    return version

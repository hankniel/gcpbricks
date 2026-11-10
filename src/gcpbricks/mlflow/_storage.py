"""Copy a directory tree to or from a gs:// URI (or a local path, which tests use)."""
from __future__ import annotations

import shutil
from pathlib import Path


def _split(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri[len("gs://"):].partition("/")
    return bucket, prefix.rstrip("/")


def _bucket(name: str):
    from google.cloud import storage

    return storage.Client().bucket(name)


def join(base: str, *parts: str) -> str:
    return "/".join([base.rstrip("/"), *(p.strip("/") for p in parts)])


def upload_tree(local_dir: Path, dest: str) -> None:
    if not dest.startswith("gs://"):
        shutil.copytree(local_dir, dest, dirs_exist_ok=True)
        return
    bucket_name, prefix = _split(dest)
    bucket = _bucket(bucket_name)
    for f in sorted(p for p in local_dir.rglob("*") if p.is_file()):
        bucket.blob(f"{prefix}/{f.relative_to(local_dir).as_posix()}").upload_from_filename(str(f))


def download_tree(src: str, local_dir: Path) -> None:
    if not src.startswith("gs://"):
        shutil.copytree(src, local_dir, dirs_exist_ok=True)
        return
    bucket_name, prefix = _split(src)
    blobs = [b for b in _bucket(bucket_name).client.list_blobs(bucket_name, prefix=f"{prefix}/")
             if not b.name.endswith("/")]
    if not blobs:
        raise FileNotFoundError(f"nothing under {src}")
    for b in blobs:
        target = local_dir / b.name[len(prefix) + 1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        b.download_to_filename(str(target))

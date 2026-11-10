"""A job's environments, built as images, as Databricks installs a job's environment:

    jobs.<job>.environments[].spec.dependencies     pip lines: pins, or -r / -c files
                                                    relative to the bundle's root

Each builds FROM the platform's runtime image at the bundle's platform version (images/runtime:
python, the system libraries ML wheels link, gcpbricks[ml,features]) with its dependencies
pip-installed on top. The image is <repository>/<bundle>-<key>:<tag>, the tag a hash of the
build context (the dependency lines, every file they reference, the runtime), so it is
rebuilt only when what it installs changes. `mlops bundle deploy` builds a missing tag with
Cloud Build before anything that runs it is applied.
"""
from __future__ import annotations

import hashlib
import io
import re
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gcpbricks import bundle as B
from gcpbricks.targets import storage_prefix

#: Where the release pushes images/runtime (.gitlab-ci.yml IMAGE_REPO), beside the dbt image.
RUNTIME_REPO = "us-central1-docker.pkg.dev/my-gcp-project/mlops"
_MANIFESTS = ("application/vnd.docker.distribution.manifest.v2+json, "
              "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json")
_FILE_OPTION = re.compile(r"^(-r|--requirement|-c|--constraint)[ =]\s*(\S+)\s*$")


def runtime_image(lb: B.Loaded) -> str:
    return f"{RUNTIME_REPO}/runtime:{B.platform_version(lb)}"


def repository(lb: B.Loaded) -> str:
    g = lb.bundle.gcp
    return g.image_repository or f"{g.region}-docker.pkg.dev/{g.project}/mlops"


def _files(root: Path, path: Path, seen: dict[str, bytes]) -> None:
    """path, and every -r / -c file it names (relative to it), into seen by bundle path."""
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    if rel in seen:
        return
    data = path.read_bytes()
    seen[rel] = data
    for line in data.decode().splitlines():
        m = _FILE_OPTION.match(line.strip())
        if m:
            _files(root, path.parent / m.group(2), seen)


def context(lb: B.Loaded, env: B.JobEnvironment) -> dict[str, bytes]:
    """The build context: Dockerfile, requirements.txt (the dependency lines, files named by
    their path under files/) and files/<bundle path> for each file referenced."""
    files: dict[str, bytes] = {}
    lines = []
    for dep in env.spec.dependencies:
        m = _FILE_OPTION.match(dep.strip())
        if m:
            target = lb.root / m.group(2)
            if not target.is_file():
                raise ValueError(f"environment {env.environment_key!r}: {dep!r} names no file in the bundle")
            _files(lb.root, target, files)
            dep = f"{m.group(1)} files/{target.resolve().relative_to(lb.root.resolve()).as_posix()}"
        lines.append(dep)
    dockerfile = (f"FROM {runtime_image(lb)}\n"
                  "COPY . /tmp/env\n"
                  "RUN pip install --no-cache-dir -r /tmp/env/requirements.txt && rm -rf /tmp/env\n")
    return {"Dockerfile": dockerfile.encode(), "requirements.txt": ("\n".join(lines) + "\n").encode(),
            **{f"files/{k}": v for k, v in files.items()}}


def tag(ctx: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(ctx):
        h.update(name.encode() + b"\0" + hashlib.sha256(ctx[name]).digest())
    return h.hexdigest()[:12]


def image(lb: B.Loaded, env: B.JobEnvironment) -> str:
    return f"{repository(lb)}/{lb.bundle.bundle.name}-{env.environment_key}:{tag(context(lb, env))}"


def used(lb: B.Loaded) -> dict[str, tuple[B.JobEnvironment, dict[str, bytes]]]:
    """image -> (environment, context), for every environment a cluster or task names."""
    out = {}
    for job in lb.bundle.resources.jobs.values():
        keys = {c.new_cluster.environment_key for c in job.job_clusters} | {
            t.environment_key for t in B._all_tasks(job)}
        for env in job.environments:
            if env.environment_key in keys:
                ctx = context(lb, env)
                out[f"{repository(lb)}/{lb.bundle.bundle.name}-{env.environment_key}:{tag(ctx)}"] = (env, ctx)
    return out


def archive(ctx: dict[str, bytes]) -> bytes:
    """The context as a tar.gz whose bytes depend only on its contents."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(ctx):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(ctx[name]), 0o644, 0
            tar.addfile(info, io.BytesIO(ctx[name]))
    return buf.getvalue()


# --- building ------------------------------------------------------------------------------

def exists(session, image_ref: str) -> bool:
    """Whether the registry holds image_ref (host/project/repo/name:tag)."""
    host, _, rest = image_ref.partition("/")
    path, _, ref = rest.rpartition(":")
    r = session.head(f"https://{host}/v2/{path}/manifests/{ref}", headers={"Accept": _MANIFESTS})
    if r.status_code not in (200, 404):
        raise RuntimeError(f"checking {image_ref}: HTTP {r.status_code}")
    return r.status_code == 200


def build(session, lb: B.Loaded, image_ref: str, ctx: dict[str, bytes], log=print) -> None:
    """Upload the context and build + push image_ref with Cloud Build, waiting for it."""
    b = lb.bundle
    obj = (f"{storage_prefix(lb.target)}.bundle/{b.bundle.name}/environments/"
           f"{image_ref.rsplit('/', 1)[1].replace(':', '-')}.tar.gz")
    r = session.post(f"https://storage.googleapis.com/upload/storage/v1/b/{b.gcp.bucket}/o",
                     params={"uploadType": "media", "name": obj}, data=archive(ctx),
                     headers={"Content-Type": "application/gzip"})
    r.raise_for_status()
    body = {"source": {"storageSource": {"bucket": b.gcp.bucket, "object": obj}},
            "steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "-t", image_ref, "."]}],
            "images": [image_ref], "timeout": "3600s", "options": {"logging": "CLOUD_LOGGING_ONLY"}}
    builds = f"https://cloudbuild.googleapis.com/v1/projects/{b.gcp.project}/locations/{b.gcp.region}/builds"
    r = session.post(builds, json=body)
    r.raise_for_status()
    build_id = r.json()["metadata"]["build"]["id"]
    log(f"..   environment {image_ref}: Cloud Build {build_id}")
    status = "QUEUED"
    while status in ("PENDING", "QUEUED", "WORKING", "STATUS_UNKNOWN"):
        time.sleep(15)
        r = session.get(f"{builds}/{build_id}")
        r.raise_for_status()
        status = r.json()["status"]
    if status != "SUCCESS":
        raise RuntimeError(f"environment {image_ref}: Cloud Build {build_id} ended {status}; its log: "
                           f"gcloud builds log {build_id} --region={b.gcp.region}")


def ensure(lb: B.Loaded, out_dir: Path | None, session=None, log=print) -> None:
    """Builds every used environment the registry lacks; with out_dir, writes each context
    there instead (a dry run)."""
    envs = used(lb)
    if out_dir is not None:
        for ref, (env, ctx) in envs.items():
            d = out_dir / "environments" / env.environment_key
            for name, data in ctx.items():
                (d / name).parent.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(data)
            log(f"ok   environment {ref}: context written to {d}, not built")
        return
    missing = {}
    for ref, (_, ctx) in envs.items():
        if exists(session, ref):
            log(f"ok   environment {ref}: already built")
        else:
            missing[ref] = ctx
    # Each is its own Cloud Build: they build side by side, the deploy waiting on the slowest.
    with ThreadPoolExecutor(max_workers=max(len(missing), 1)) as pool:
        for ref, f in [(ref, pool.submit(build, session, lb, ref, ctx, log)) for ref, ctx in missing.items()]:
            f.result()
            log(f"ok   environment {ref}: built")

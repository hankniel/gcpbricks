"""A bundle's infrastructure: its Workflows, Cloud Run jobs, schedulers, triggers and alerts,
applied by `mlops bundle deploy` with terraform/modules/bundle, as Databricks' CLI applies a
bundle with its own terraform. A project keeps no terraform root, state or CI job of its own.

Each deploy writes a throwaway root next to a copy of the module this package ships, so the
module is always the CLI's own version. State lives with the bundle's files,

    gs://<bucket>/<target prefix>.bundle/<bundle>/terraform/

locked by terraform's GCS backend: two deploys of a target never interleave.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from gcpbricks import bundle as B
from gcpbricks.targets import storage_prefix

#: Where the release pushes images/dbt (.gitlab-ci.yml IMAGE_REPO); the tag is the bundle's
#: platform version (bundle.platform_version).
DBT_IMAGE_REPO = "us-central1-docker.pkg.dev/my-gcp-project/mlops"


def module_dir() -> Path:
    """The bundle module: inside the installed package, or in this repo's checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "terraform" / "bundle", here.parents[1] / "terraform" / "modules" / "bundle"):
        if (candidate / "main.tf").is_file():
            return candidate
    raise RuntimeError("gcpbricks is installed without its terraform module")


def state_prefix(lb: B.Loaded) -> str:
    return f"{storage_prefix(lb.target)}.bundle/{lb.bundle.bundle.name}/terraform"


def dbt_image(lb: B.Loaded) -> str:
    return f"{DBT_IMAGE_REPO}/dbt:{B.platform_version(lb)}"


def root(lb: B.Loaded, existing: list[str] = ()) -> dict:
    """The root's main.tf.json: the GCS backend and the module over the rendered plan, plus
    an import of each schema whose dataset already exists, so a deploy adopts it instead of
    failing to create it (a no-op once it is in state)."""
    main = {"terraform": {"required_version": ">= 1.6", "backend": {"gcs": {}}},
            "module": {"bundle": {"source": "./modules/bundle", "gcp_project": lb.bundle.gcp.project,
                                  "plan": '${jsondecode(file("${path.module}/plan.json"))}',
                                  "dbt_image": dbt_image(lb)}}}
    schemas = B.schemas(lb)
    imports = [{"to": f'module.bundle.google_bigquery_dataset.this["{k}"]',
                "id": f"projects/{schemas[k]['project']}/datasets/{schemas[k]['dataset_id']}"}
               for k in sorted(existing) if k in schemas]
    if imports:
        main["import"] = imports
    return main


def existing_schemas(lb: B.Loaded, session=None) -> list[str]:
    """The keys of the bundle's schemas whose dataset already exists. ValueError when one
    lives in another location than declared: terraform would replace it, deleting its
    tables."""
    schemas = B.schemas(lb)
    if not schemas:
        return []
    if session is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(creds)
    found = []
    for k, s in schemas.items():
        r = session.get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{s['project']}/datasets/{s['dataset_id']}")
        if r.status_code == 200:
            live = r.json()["location"]
            if live.lower() != s["location"].lower():
                raise ValueError(f"schema {k!r}: dataset {s['dataset_id']} is in {live}, declared in "
                                 f"{s['location']}; declare location: {live}")
            found.append(k)
        elif r.status_code != 404:
            r.raise_for_status()
    return found


def write_root(lb: B.Loaded, where: Path, existing: list[str] = ()) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    (where / "main.tf.json").write_text(json.dumps(root(lb, existing), indent=2))
    (where / "plan.json").write_text(json.dumps(B.render(lb), indent=2, sort_keys=True))
    shutil.copytree(module_dir(), where / "modules" / "bundle", dirs_exist_ok=True)
    return where


def apply(lb: B.Loaded, destroy: bool = False, out_dir: Path | None = None,
          runner=subprocess.run) -> int:
    """terraform init + apply (or destroy) of the bundle for its target; with out_dir, only
    write the root there."""
    if out_dir is not None:
        write_root(lb, out_dir / "terraform")
        print(f"ok   infrastructure: terraform root written to {out_dir / 'terraform'}, not applied")
        return 0
    if shutil.which("terraform") is None:
        print("FAIL infrastructure: terraform (>= 1.6) is not on PATH")
        return 1
    try:
        existing = [] if destroy else existing_schemas(lb)
    except ValueError as e:
        print(f"FAIL infrastructure: {e}")
        return 1
    work = write_root(lb, Path(tempfile.mkdtemp(prefix="mlops-terraform-")), existing)
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    steps = [["terraform", "init", "-no-color", f"-backend-config=bucket={lb.bundle.gcp.bucket}",
              f"-backend-config=prefix={state_prefix(lb)}"],
             ["terraform", "destroy" if destroy else "apply", "-no-color", "-auto-approve"]]
    for argv in steps:
        print(f"+ {' '.join(argv)}", flush=True)
        code = runner(argv, cwd=work, env=env, check=False).returncode
        if code:
            print(f"FAIL infrastructure: {argv[1]} exited {code}")
            return code
    where = f"gs://{lb.bundle.gcp.bucket}/{state_prefix(lb)}"
    print(f"ok   infrastructure: {'destroyed' if destroy else 'applied'} (state {where})")
    return 0

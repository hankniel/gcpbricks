"""`mlops bundle deploy`'s infrastructure step: the terraform root it writes and runs."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gcpbricks import bundle as B
from gcpbricks import infra

FIXTURE = Path(__file__).parent / "fixtures" / "bundle"


@pytest.fixture()
def lb() -> B.Loaded:
    return B.load(FIXTURE / "mlops.yaml", "dev")


def test_the_root_runs_this_versions_module_over_the_rendered_plan(lb, tmp_path):
    where = infra.write_root(lb, tmp_path)
    main = json.loads((where / "main.tf.json").read_text())
    module = main["module"]["bundle"]
    assert module["source"] == "./modules/bundle" and module["gcp_project"] == "my-gcp-project"
    assert module["dbt_image"] == f"{infra.DBT_IMAGE_REPO}/dbt:0.4.0"
    assert main["terraform"]["backend"] == {"gcs": {}}
    assert json.loads((where / "plan.json").read_text()) == B.render(lb)
    assert (where / "modules" / "bundle" / "workflows" / "job.yaml").is_file()


def test_state_sits_with_the_bundles_files_per_target(lb):
    assert infra.state_prefix(lb) == "dev/.bundle/demo-app/terraform"
    assert infra.state_prefix(B.load(FIXTURE / "mlops.yaml", "prod")) == ".bundle/demo-app/terraform"


def test_apply_inits_the_bundles_backend_then_applies(lb, monkeypatch):
    monkeypatch.setattr(infra.shutil, "which", lambda _: "/usr/bin/terraform")
    calls = []

    def runner(argv, cwd, env, check):
        calls.append((argv, Path(cwd)))
        assert env["TF_IN_AUTOMATION"] == "1"
        assert (Path(cwd) / "main.tf.json").is_file()
        return subprocess.CompletedProcess(argv, 0)
    assert infra.apply(lb, runner=runner) == 0
    assert [c[0][:2] for c in calls] == [["terraform", "init"], ["terraform", "apply"]]
    assert "-backend-config=bucket=my-ml-bucket" in calls[0][0]
    assert "-backend-config=prefix=dev/.bundle/demo-app/terraform" in calls[0][0]
    assert "-auto-approve" in calls[1][0]


def test_destroy_destroys_and_a_failed_init_stops_there(lb, monkeypatch):
    monkeypatch.setattr(infra.shutil, "which", lambda _: "/usr/bin/terraform")
    seen = []
    assert infra.apply(lb, destroy=True, runner=lambda a, **k: seen.append(a[1]) or
                       subprocess.CompletedProcess(a, 0)) == 0
    assert seen == ["init", "destroy"]
    seen.clear()
    assert infra.apply(lb, runner=lambda a, **k: seen.append(a[1]) or subprocess.CompletedProcess(a, 3)) == 3
    assert seen == ["init"]


def test_without_terraform_the_deploy_fails_and_with_out_dir_only_writes(lb, tmp_path, monkeypatch):
    monkeypatch.setattr(infra.shutil, "which", lambda _: None)
    assert infra.apply(lb) == 1
    assert infra.apply(lb, out_dir=tmp_path) == 0
    assert (tmp_path / "terraform" / "main.tf.json").is_file()

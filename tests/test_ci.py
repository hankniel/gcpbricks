"""gcpbricks.ci: a deploy's commit, branch, links and actor from any CI's variables."""
from __future__ import annotations

import pytest

from gcpbricks import ci

_VARS = ["MLOPS_COMMIT", "MLOPS_BRANCH", "MLOPS_COMMIT_URL", "MLOPS_PIPELINE_URL", "MLOPS_ACTOR",
         "CI_COMMIT_SHA", "CI_COMMIT_REF_NAME", "CI_PROJECT_URL", "CI_PIPELINE_URL", "GITLAB_USER_EMAIL",
         "GITHUB_SHA", "GITHUB_REF_NAME", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
         "GITHUB_ACTOR", "USER"]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_outside_ci_only_the_local_user_is_known(monkeypatch):
    monkeypatch.setenv("USER", "me")
    assert (ci.commit(), ci.branch(), ci.commit_url("abc"), ci.pipeline_url(), ci.actor()) == ("", "", "", "", "me")


def test_gitlab_ci(monkeypatch):
    for k, v in {"CI_COMMIT_SHA": "abc", "CI_COMMIT_REF_NAME": "main", "CI_PROJECT_URL": "https://git.example/g/p",
                 "CI_PIPELINE_URL": "https://git.example/g/p/-/pipelines/7", "GITLAB_USER_EMAIL": "a@example.com",
                 "USER": "root"}.items():
        monkeypatch.setenv(k, v)
    assert (ci.commit(), ci.branch(), ci.commit_url("abc"), ci.pipeline_url(), ci.actor()) == (
        "abc", "main", "https://git.example/g/p/-/commit/abc", "https://git.example/g/p/-/pipelines/7", "a@example.com")


def test_github_actions(monkeypatch):
    for k, v in {"GITHUB_SHA": "abc", "GITHUB_REF_NAME": "main", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9", "GITHUB_ACTOR": "octo", "USER": "runner"}.items():
        monkeypatch.setenv(k, v)
    assert (ci.commit(), ci.branch(), ci.commit_url("abc"), ci.pipeline_url(), ci.actor()) == (
        "abc", "main", "https://github.com/o/r/commit/abc", "https://github.com/o/r/actions/runs/9", "octo")


def test_mlops_variables_win_for_any_other_ci(monkeypatch):
    for k, v in {"MLOPS_COMMIT": "def", "MLOPS_BRANCH": "rel", "MLOPS_COMMIT_URL": "https://x/c/def",
                 "MLOPS_PIPELINE_URL": "https://x/b/1", "MLOPS_ACTOR": "bot", "GITHUB_SHA": "abc",
                 "GITHUB_REPOSITORY": "o/r", "GITHUB_ACTOR": "octo"}.items():
        monkeypatch.setenv(k, v)
    assert (ci.commit(), ci.branch(), ci.commit_url("def"), ci.pipeline_url(), ci.actor()) == (
        "def", "rel", "https://x/c/def", "https://x/b/1", "bot")

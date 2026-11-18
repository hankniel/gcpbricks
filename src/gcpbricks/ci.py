"""What a deploy or a repair records about where it ran: its commit, branch, pipeline and who
started it. Read from the CI system's own variables, so any CI works:

  MLOPS_COMMIT, MLOPS_BRANCH, MLOPS_COMMIT_URL, MLOPS_PIPELINE_URL, MLOPS_ACTOR
      set them in any CI (or by hand); each wins over what follows
  GitLab CI       CI_COMMIT_SHA, CI_COMMIT_REF_NAME, CI_PROJECT_URL, CI_PIPELINE_URL, GITLAB_USER_EMAIL
  GitHub Actions  GITHUB_SHA, GITHUB_REF_NAME, GITHUB_SERVER_URL + GITHUB_REPOSITORY, GITHUB_RUN_ID,
                  GITHUB_ACTOR

Outside CI every value is empty, and the caller falls back to git and $USER."""
from __future__ import annotations

import os


def _env(*names: str) -> str:
    return next((os.environ[n] for n in names if os.environ.get(n)), "")


def _github_repo_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}" if repo else ""


def commit() -> str:
    """The commit CI builds, or ""."""
    return _env("MLOPS_COMMIT", "CI_COMMIT_SHA", "GITHUB_SHA")


def branch() -> str:
    return _env("MLOPS_BRANCH", "CI_COMMIT_REF_NAME", "GITHUB_REF_NAME")


def commit_url(sha: str) -> str:
    """A link to the commit on the CI's forge, or ""."""
    if os.environ.get("MLOPS_COMMIT_URL"):
        return os.environ["MLOPS_COMMIT_URL"]
    if os.environ.get("CI_PROJECT_URL"):
        return f"{os.environ['CI_PROJECT_URL']}/-/commit/{sha}"
    repo = _github_repo_url()
    return f"{repo}/commit/{sha}" if repo else ""


def pipeline_url() -> str:
    if url := _env("MLOPS_PIPELINE_URL", "CI_PIPELINE_URL"):
        return url
    repo, run = _github_repo_url(), os.environ.get("GITHUB_RUN_ID")
    return f"{repo}/actions/runs/{run}" if repo and run else ""


def actor() -> str:
    """Who started it: CI's user, else the local one."""
    return _env("MLOPS_ACTOR", "GITLAB_USER_EMAIL", "GITHUB_ACTOR", "USER")

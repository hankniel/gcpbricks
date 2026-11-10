"""Target namespacing: prod at the root, every other target under its own name, so a dev
run's objects form one listable, deletable tree and its resources never collide with
prod's."""
from __future__ import annotations

PROD = "prod"


def storage_prefix(target: str) -> str:
    """Object path prefix: "" for prod, "<target>/" otherwise."""
    return "" if target == PROD else f"{target}/"


def name_suffix(target: str, sep: str = "-") -> str:
    """Resource name suffix: "" for prod, "-<target>" otherwise. Cloud Run and Scheduler
    names are DNS-style, hence "-" by default."""
    return "" if target == PROD else f"{sep}{target}"

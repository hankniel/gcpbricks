"""A features.yaml's SQL features in words, one sentence each: what DS checks against
what they meant before training, and the lookup table a guide prints.

  python -m gcpbricks.feature_manager.explain FEATURES_YAML [--dbt-features DIR] [--markdown]

Days are counted from the spine row's timestamp (D0: e.g. its install day); with
landmarks, `a` is the row's decision day and `H` its horizon.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .plan import compile_spec

_FUNCTION = {"sum": "sum of {x}", "count": "number of rows with {x}", "mean": "mean of {x}",
             "min": "min of {x}", "max": "max of {x}", "stddev_samp": "sample std dev of {x}",
             "stddev_pop": "population std dev of {x}", "var_samp": "sample variance of {x}",
             "var_pop": "population variance of {x}", "count_distinct": "distinct values of {x}",
             "first": "earliest {x}", "last": "latest {x}"}
_PREFIX = re.compile(r"^__sql_[0-9a-f]{12}__")


def explain(spec: dict, groups: dict | None = None) -> list[tuple[str, str]]:
    """(name, sentence) per row shape, aggregation, transform and label of `spec`.
    `groups` (feature_config's, by view name) names the keys each aggregation joins on."""
    from .spec import FeaturesConfig

    cfg = FeaturesConfig(**spec)
    plan = compile_spec(spec, resolve_custom=False)
    lm = plan.landmarks
    a = "a" if lm else plan.at_days
    out = [("rows", _rows(plan, groups))]
    for n in plan.nodes:
        if n.kind == "aggregate":
            out.append((n.name, _aggregation(n, a, lm, plan.spine, groups)))
    for t in cfg.transforms:
        out.append((t.name, f"{t.formula}, per row" + ("" if t.keep else " (intermediate, dropped)")))
    for n in plan.labels:
        out.append((n.name, "label: " + _aggregation(n, a, lm, plan.spine, groups)))
    return out


def _rows(plan, groups) -> str:
    spine = plan.spine or "the spine"
    keys = groups[plan.spine].keys if groups and plan.spine in (groups or {}) else None
    per = f"one per {spine} row" + (f" ({', '.join(keys)})" if keys else "")
    d0 = f"; D0 is the row's {groups[plan.spine].timestamp}" if keys else ""
    if plan.landmarks:
        lm = plan.landmarks
        per += f" x decision day a in {_days(lm['at'])}"
        if lm["horizons"]:
            per += f" x horizon H in {_days(lm['horizons'])}"
        return per + d0 + "; a decision day the data hasn't reached has no row, a horizon no label"
    if plan.at_days is not None:
        return per + d0 + f"; features as of D{plan.at_days}, that day included"
    return per + d0 + "; the decision day is set where the feature view is made (at_days)"


def _days(days: list[int]) -> str:
    if len(days) > 3 and days == list(range(days[0], days[-1] + 1, days[1] - days[0])):
        return f"{days[0]}..{days[-1]} every {days[1] - days[0]}"
    return str(days)


def _aggregation(n, a, lm, spine, groups) -> str:
    from gcpbricks.features import _windows

    p = n.params
    x = p["input_column"] if p["input_formula"] is None else f"{p['input_column']} (its formula)"
    what = _FUNCTION[p["function"]].format(x=x)
    point = "H" if p.get("label") else a
    windows = _windows(n, {"anchored": bool(lm)})
    if p.get("since_decision"):
        windows = [(("decision", 0), hi) for _, hi in windows]
    spans = [_span(lo, hi, point, a) for lo, hi in windows]
    when = spans[0] if len(spans) == 1 else "each of " + "; ".join(spans) + " (a matrix)"
    source = groups.get(p["source"]) if groups else None
    spine_keys = set(groups[spine].keys) if groups and spine in groups else None
    if source is not None and spine_keys is not None:
        on = [k for k in source.keys if k in spine_keys and k not in p["over"]]
        grain = ("per spine row" if spine_keys <= set(on) | set(p["over"])
                 else f"per {', '.join(on)}")
    else:
        grain = "per the spine's keys"
    if p["over"]:
        grain += f", across {', '.join(p['over'])}"
    empty = "0" if p["function"] in ("sum", "count") else "NULL"
    return f"{what} in {p['source']}, {when}, {grain}; none there: {empty}"


def _span(lo, hi, point, a) -> str:
    def day(bound, last):
        base, d = bound
        shift = -1 if last else 0
        if base == "anchor":
            return f"D{d + shift}"
        return f"D{_add(point if base == 'horizon' else a, d + 1 + shift)}"
    end = day(hi, True)
    if lo is None:
        return f"every day up to {end}"
    start = day(lo, False)
    return start if start == end else f"{start}..{end}"


def _add(day, d: int) -> str:
    if day is None:
        day = "a"
    if isinstance(day, int):
        return str(day + d)
    return day if d == 0 else f"({day}{d:+d})"


def preview(df, n: int | None = 10):
    """df's first n rows (all with None), the SQL stage's columns under their own names
    (no __sql_<hash>__ prefix) where that leaves names unique."""
    head = df if n is None else df.head(n)
    short = [_PREFIX.sub("", c) for c in head.columns]
    clash = {c for c in short if short.count(c) > 1}
    return head.set_axis([s if s not in clash else c for c, s in zip(head.columns, short)], axis=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gcpbricks.feature_manager.explain")
    ap.add_argument("features_yaml")
    ap.add_argument("--dbt-features", help="a dbt-features checkout: the keys each aggregation joins on")
    ap.add_argument("--markdown", action="store_true", help="a | feature | meaning | table")
    a = ap.parse_args(argv)
    groups = None
    if a.dbt_ml:
        from gcpbricks.feature_config import from_checkout
        groups = from_checkout(a.dbt_ml)
    lines = explain(yaml.safe_load(Path(a.features_yaml).read_text()), groups)
    if a.markdown:
        print("| feature | meaning |\n|---|---|")
        for name, text in lines:
            print(f"| `{name}` | {text.replace('|', '/')} |")
    else:
        for name, text in lines:
            print(f"{name}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

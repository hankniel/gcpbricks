"""Move a features.yaml from one `define` list to the blocks, keeping its text.

  python -m gcpbricks.feature_manager.migrate FEATURES_YAML --dbt-features DIR --view NAME

An entry goes to `transforms` when every node it expands to is an expr whose ops SQL
states and which reads only the view's numeric columns and transforms before it; every
other entry goes to `preprocessing`, in its order. Each entry moves with the comment lines
above it; `expr:` becomes `formula:`, `custom:` `udf:`. The result compiles to the same
outputs and inputs, which this checks before writing.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .plan import EXPR, _expand, compile_spec
from .spec import FeatureDefine
from .sql import unsupported

_TOP = re.compile(r"^[a-z_]+:")
_NUMERIC = {"Int64", "Float64", "Bool"}


def _chunks(lines: list[str]) -> list[list[str]]:
    """The `define` block's items, each with the comment and blank lines above it."""
    chunks, pending, current = [], [], None
    for line in lines:
        if line.startswith("  - "):
            current = [*pending, line]
            chunks.append(current)
            pending = []
        elif not line.strip() or (line.startswith("  #") and not line.startswith("   ")):
            pending.append(line)
        elif current is None:
            raise ValueError(f"migrate: a define line before any entry: {line!r}")
        else:
            current.append(line)
    if pending and chunks:
        chunks[-1] += pending
    return chunks


def _entry(chunk: list[str]) -> dict:
    body = [line for line in chunk if line.strip() and not line.lstrip().startswith("#")]
    return yaml.safe_load("\n".join(line[2:] for line in body))[0]


def _rename(chunk: list[str], python: bool) -> list[str]:
    keys = [("expr", "formula")] + ([("custom", "udf")] if python else [])
    out = []
    for line in chunk:
        if not line.lstrip().startswith("#"):
            for old, new in keys:
                line = re.sub(rf"(^\s*-\s+|^\s+|[{{,]\s*){old}:", rf"\1{new}:", line)
        out.append(line)
    return out


def migrate(text: str, numeric: set[str]) -> str:
    """`text`, a features.yaml with `define`, with blocks instead."""
    spec = yaml.safe_load(text)
    if "define" not in spec:
        raise ValueError("migrate: no `define` block")
    legacy = compile_spec(spec, resolve_custom=False)
    by_name = {n.name: n for n in legacy.nodes}
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("define:"))
    end = next((i for i in range(start + 1, len(lines)) if _TOP.match(lines[i])), len(lines))
    sql_names: set[str] = set()
    sql, python = [], []
    for chunk in _chunks(lines[start + 1:end]):
        entry = _entry(chunk)
        names = [e["name"] for e in _expand(FeatureDefine(**entry).model_dump(), spec.get("vars", {}))]
        nodes = [by_name[n] for n in names]
        if all(n.kind == EXPR and not unsupported(n.expr)
               and all(r in sql_names or r in numeric for r in n.reads) for n in nodes):
            sql_names.update(names)
            sql.append(_rename(chunk, python=False))
        else:
            python.append(_rename(chunk, python=True))
    body = []
    for key, chunks in (("transforms", sql), ("preprocessing", python)):
        if chunks:
            if body:
                body.append("")
            body.append(f"{key}:")
            first = chunks[0]
            while first and not first[0].strip():
                first = first[1:]
            for chunk in [first, *chunks[1:]]:
                body += chunk
    while body and not body[-1].strip():
        body.pop()
    rest = lines[end:]
    out = "\n".join([*lines[:start], *body, *([""] if rest else []), *rest]) + "\n"
    blocks = compile_spec(yaml.safe_load(out), resolve_custom=False)
    same = (set(blocks.outputs) == set(legacy.outputs)
            and blocks.inputs.keys() == legacy.inputs.keys()
            and all(set(blocks.inputs[k]) == set(v) for k, v in legacy.inputs.items())
            and set(blocks.fetch_columns) == set(legacy.fetch_columns))
    if not same:
        raise ValueError("migrate: the blocks compile to other outputs, inputs or fetch columns")
    return out


def reordered(before: str, after: str) -> list[str]:
    """The inputs sets whose column order moved (+outputs and +group: follow the blocks'
    order now): a model trained on them sees its columns in another order."""
    a = compile_spec(yaml.safe_load(before), resolve_custom=False)
    b = compile_spec(yaml.safe_load(after), resolve_custom=False)
    return [k for k in a.inputs if a.inputs[k] != b.inputs[k]]


def main(argv: list[str] | None = None) -> int:
    from gcpbricks.feature_config import from_checkout

    ap = argparse.ArgumentParser(prog="python -m gcpbricks.feature_manager.migrate")
    ap.add_argument("features_yaml")
    ap.add_argument("--dbt-features", required=True, help="a dbt-features checkout: the view's column types")
    ap.add_argument("--view", required=True, help="the feature view the spec's columns come from")
    a = ap.parse_args(argv)
    g = from_checkout(a.dbt_ml)[a.view]
    numeric = {f.name for f in g.features if f.dtype in _NUMERIC}
    path = Path(a.features_yaml)
    before = path.read_text()
    after = migrate(before, numeric)
    path.write_text(after)
    moved = reordered(before, after)
    print(f"wrote {path}" + (f"; column order moved in inputs {moved}" if moved else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

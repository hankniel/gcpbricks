"""Feature groups, as dbt-features declares them in its _models.yml.

  mlops-features render [DBT_ML_DIR] [--out FILE|gs://...]
      every model with meta.feature_store in a dbt-features checkout, as JSON (stdout by default)

A dbt model is a feature group when its meta has a `feature_store` block; its columns say
the rest, with the meta dbt-features already carries:

    models:
      - name: fs_demo_features
        meta:
          feature_store:
            offline_table: ml_feature_store.fs_demo_features   # what the engine reads
            display_name: Predict ROAS cohort features   # optional: the console's name for it
            kind: feature                   # or label: a training target, not a model input
            families:                       # optional: family -> column globs, first match
              acquisition: [cost, installs, impressions, clicks]
              revenue: ["*_rev_day_*"]
        columns:
          - name: install_date
            data_type: String
            meta: {entity: true}            # a join key
          - name: feature_timestamp
            meta: {feature_timestamp: true} # the as-of time
          - name: cost
            data_type: Float64
            meta: {family: acquisition}     # wins over `families`

Every other column with a data_type is a feature; one no family matches is in "default".
The JSON render writes is what dbt-features's CI publishes and FeatureStore reads.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

DEFAULT_FAMILY = "default"
KIND = Literal["feature", "label"]
#: dbt-features's data_type -> feature type name, matched case-insensitively. data_type is the
#: adapter's own: BigQuery (INT64, FLOAT64, NUMERIC(10,2), STRING, DATE, TIMESTAMP), Spark
#: (bigint, double, decimal(10,2), string, date, timestamp) or, while dbt-features still builds
#: there, ClickHouse (UInt32, Float64, LowCardinality(String), DateTime64(3)).
_TYPES = [
    (r"u?int\d*|integer|bigint|smallint|tinyint|byteint|long|short", "Int64"),
    (r"float\d*|double|real|(big)?numeric(\(.*\))?|(big)?decimal(\(.*\))?", "Float64"),
    (r"bool(ean)?", "Bool"),
    (r"(lowcardinality\()?(string(\(\d+\))?|fixedstring.*|uuid|(var)?char(\(\d+\))?)\)?", "String"),
    (r"date(32)?|datetime.*|timestamp(_ntz|_ltz)?(\(\d+\))?", "UnixTimestamp"),
]
#: Keys are strings or integers: a DATE install_date is a 'YYYY-MM-DD' STRING.
_KEY_TYPES = {"Int64", "String"}


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: str
    family: str = DEFAULT_FAMILY
    description: str = ""


class FeatureGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    offline_table: str
    keys: list[str]
    #: key -> feature type name, as for a feature.
    key_types: dict[str, str]
    timestamp: str
    features: list[Feature]
    #: What the console calls the group; empty: its name.
    display_name: str = ""
    #: "label": its columns are training targets, listed apart from feature tables.
    kind: KIND = "feature"
    description: str = ""
    #: The dbt project directory, for finding where a group is declared.
    project: str = ""

    def family(self, name: str) -> list[str]:
        names = [f.name for f in self.features if f.family == name]
        if not names:
            raise KeyError(f"{self.name} has no family {name!r}; it has "
                           f"{sorted({f.family for f in self.features})}")
        return names


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_table: str
    display_name: str = ""
    kind: KIND = "feature"
    families: dict[str, list[str]] = {}


def feature_type(data_type: str) -> str:
    t = re.sub(r"^nullable\((.*)\)$", r"\1", data_type.strip().lower())
    for pattern, name in _TYPES:
        if re.fullmatch(pattern, t):
            return name
    raise ValueError(f"no feature type for data_type {data_type!r} (arrays, structs and "
                     f"maps are not features)")


def _meta(node: dict) -> dict:
    # dbt >= 1.10 and the v2 parser want meta under config; older files keep it top-level
    return (node.get("meta") or {}) | ((node.get("config") or {}).get("meta") or {})


def from_models_yml(docs: dict[str, dict]) -> dict[str, FeatureGroup]:
    """name -> FeatureGroup, for every model with meta.feature_store in these documents
    (keyed by their path)."""
    groups: dict[str, FeatureGroup] = {}
    for path, doc in docs.items():
        for m in (doc or {}).get("models", []):
            block = _meta(m).get("feature_store")
            if not block:
                continue
            if m["name"] in groups:
                raise ValueError(f"{m['name']} is declared twice ({path})")
            groups[m["name"]] = _group(m, _Block.model_validate(block), path)
    return groups


def _group(m: dict, block: _Block, path: str) -> FeatureGroup:
    where = f"{m['name']} ({path})"
    keys, key_types, stamps, features = [], {}, [], []
    for c in m.get("columns", []):
        meta = _meta(c)
        if meta.get("entity"):
            keys.append(c["name"])
            try:
                key_types[c["name"]] = feature_type(c.get("data_type") or "")
            except ValueError as e:
                raise ValueError(f"{where}, key {c['name']}: {e}") from None
            if key_types[c["name"]] not in _KEY_TYPES:
                raise ValueError(f"{where}, key {c['name']}: a key is a string or an integer, "
                                 f"not {c.get('data_type')}; cast a date key to STRING "
                                 f"('YYYY-MM-DD')")
        elif meta.get("feature_timestamp"):
            stamps.append(c["name"])
        elif c.get("data_type"):
            family = meta.get("family") or next(
                (fam for fam, globs in block.families.items()
                 if any(fnmatch.fnmatchcase(c["name"], g) for g in globs)), DEFAULT_FAMILY)
            try:
                dtype = feature_type(c["data_type"])
            except ValueError as e:
                raise ValueError(f"{where}, column {c['name']}: {e}") from None
            features.append(Feature(name=c["name"], dtype=dtype, family=family,
                                    description=(c.get("description") or "").strip()))
    if not keys:
        raise ValueError(f"{where}: no column has meta.entity")
    if len(stamps) != 1:
        raise ValueError(f"{where}: needs exactly one meta.feature_timestamp column, has {stamps}")
    if not features:
        raise ValueError(f"{where}: no feature columns (a column needs a data_type)")
    return FeatureGroup(name=m["name"], offline_table=block.offline_table, keys=keys,
                        key_types=key_types, timestamp=stamps[0], features=features,
                        display_name=block.display_name.strip(), kind=block.kind, description=(m.get("description") or "").strip(),
                        project=str(Path(path).parts[0]) if Path(path).parts else "")


def from_checkout(root: str | Path) -> dict[str, FeatureGroup]:
    """The groups every <project>/models/**/_models.yml of a dbt-features checkout declares."""
    import yaml

    root = Path(root)
    return from_models_yml({str(p.relative_to(root)): yaml.safe_load(p.read_text())
                            for p in sorted(root.glob("*/models/**/_models.yml"))})


def render(groups: dict[str, FeatureGroup]) -> str:
    return json.dumps({"groups": [g.model_dump() for _, g in sorted(groups.items())]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mlops-features")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="a dbt-features checkout's feature groups, as JSON")
    r.add_argument("root", nargs="?", default=".")
    r.add_argument("--out", help="a file or gs:// URI instead of stdout")
    a = ap.parse_args(argv)
    groups = from_checkout(a.root)
    text = render(groups)
    if not a.out:
        print(text)
    elif a.out.startswith("gs://"):
        from google.cloud import storage

        bucket, _, blob = a.out[len("gs://"):].partition("/")
        storage.Client().bucket(bucket).blob(blob).upload_from_string(
            text, content_type="application/json")
        print(f"{len(groups)} feature group(s) -> {a.out}", file=sys.stderr)
    else:
        Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The sklearn transformers a Plan compiles into, and build() that chains them.

Consecutive expr entries share one ExprBlock, which adds all its columns in a single
concat -- one step per feature would copy the frame once per feature. Each fit entry is
its own step, so it learns from the frame as every earlier step left it. Matrix values
are never written to the frame: a step that reads one declared in another step
recomputes it.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from .expr import Expr, evaluate
from .ops import MATRIX, VECTOR
from .plan import AGG, CUSTOM, EXPR, LOOKUP, PYTHON, Node, Plan

#: Joins multi-column group keys; a control character no key value carries.
_KEY_SEP = "\x1f"


def column(X: pd.DataFrame, name: str) -> np.ndarray:
    """A frame column as float64 with NaN for missing; a datetime column as days since
    1970-01-01, so date arithmetic (e.g. weekday = (d + 3) % 7) stays an expression."""
    if name not in X.columns:
        raise KeyError(f"feature_manager: column {name!r} is not in the input frame")
    s = X[name]
    if str(s.dtype) == "dbdate":  # what BigQuery's to_dataframe() gives a DATE column
        s = pd.to_datetime(s)
    if pd.api.types.is_datetime64_any_dtype(s):
        days = s.to_numpy(dtype="datetime64[ns]").astype("datetime64[D]")
        return np.where(np.isnat(days), np.nan, days.astype(np.int64)).astype(np.float64)
    try:
        return s.to_numpy(dtype=np.float64, na_value=np.nan)
    except (TypeError, ValueError):
        raise TypeError(f"feature_manager: column {name!r} is {s.dtype}, not numeric") from None


def with_columns(X: pd.DataFrame, new: dict[str, np.ndarray]) -> pd.DataFrame:
    clash = sorted(set(new) & set(X.columns))
    if clash:
        raise ValueError(f"feature_manager: features {clash} would overwrite input columns of the "
                         f"same name; rename the features")
    if not new:
        return X
    return pd.concat([X, pd.DataFrame(new, index=X.index)], axis=1)


def group_keys(X: pd.DataFrame, by: tuple[str, ...], missing_key: str = "default") -> pd.Series:
    """One key per row. missing_key="default": missing when any key column is, so the row
    is left out of fitting and gets the step's default. "group": a missing value is the
    text "nan", a group like any other (what astype(str) keys made of it)."""
    absent = [b for b in by if b not in X.columns]
    if absent:
        raise KeyError(f"feature_manager: group columns {absent} are not in the input frame")
    as_text = _key_text if missing_key == "group" else (lambda s: s.astype("string"))
    key = as_text(X[by[0]])
    for b in by[1:]:
        key = key + _KEY_SEP + as_text(X[b])
    return key


def _key_text(s: pd.Series) -> pd.Series:
    """s as pandas 2's astype(str) made it, which fitted tables are keyed by. pandas 3
    reads a string column's nulls as NaN where pandas 2 read None ("None", not "nan")."""
    if isinstance(s.dtype, pd.StringDtype) and s.dtype.na_value is not pd.NA:
        s = s.astype(object).where(s.notna(), None)
    return s.astype(object).map(str)


class _Scope:
    """What an expression can read while a step runs: frame columns, each converted once,
    and the nodes this step has computed so far."""

    def __init__(self, X: pd.DataFrame):
        self.X, self.values = X, {}

    def lookup(self, name: str) -> np.ndarray:
        if name not in self.values:
            self.values[name] = column(self.X, name)
        value = self.values[name]
        if isinstance(value, _Absent):
            raise KeyError(value.message)
        if isinstance(value, _Pending):
            value = self.values[name] = self.eval(value.node.expr)
        return value

    def eval(self, expr: Expr) -> np.ndarray:
        return evaluate(expr, self.lookup, len(self.X))

    def run(self, nodes) -> None:
        for node in nodes:
            done = self.computed(node)
            if done is not None:
                self.values[node.name] = done
            elif node.stage != PYTHON:
                # Only when read: a matrix the feature store computed from it needs none
                # of its inputs here, and those raw columns may have stayed in SQL.
                self.values[node.name] = _Pending(node)
            else:
                self.values[node.name] = self.eval(node.expr)

    def has(self, name: str) -> bool:
        """Its value can be had: computed, computable here, or not an absent aggregation."""
        return not isinstance(self.values.get(name), _Absent)

    def computed(self, node: Node):
        """A SQL node's value as the feature store computed it (its sql_column, a matrix
        as sql_column__0, __1, ...), or None when the frame lacks it: then a transform is
        computed here, as numpy states it; an aggregation cannot be."""
        if node.stage == PYTHON:
            return None
        col = node.params["sql_column"]
        if node.shape == VECTOR:
            if col in self.X.columns:
                return column(self.X, col)
        else:
            cells = sorted((c for c in self.X.columns if c.startswith(col + "__")),
                           key=lambda c: int(c.rsplit("__", 1)[1]))
            if cells:
                return np.column_stack([column(self.X, c) for c in cells])
        if node.kind == AGG:
            # Only an error once something reads it: what only SQL read never comes back.
            return _Absent(f"feature_manager: aggregation {node.name!r} is computed by the "
                           f"feature store, and the frame lacks it ({col})")
        return None


class _Pending:
    def __init__(self, node: Node):
        self.node = node


class _Absent:
    def __init__(self, message: str):
        self.message = message


class ExprBlock(TransformerMixin, BaseEstimator):
    """Stateless: evaluates its nodes in order on each frame it is given."""

    def __init__(self, nodes=()):
        self.nodes = nodes

    def fit(self, X, y=None):
        self.fitted_ = True
        return self

    def transform(self, X):
        check_is_fitted(self, "fitted_")
        scope = _Scope(X)
        scope.run(self.nodes)
        out = {}
        for n in self.nodes:
            if n.shape != VECTOR or not (n.keep or scope.has(n.name)):
                continue
            try:
                out[n.name] = scope.lookup(n.name)
            except KeyError:
                # An intermediate the feature store computed past: its raw columns stayed
                # in SQL, and what reads it got its own value from there.
                if n.keep or n.stage == PYTHON:
                    raise
        return with_columns(X, out)


class _FitStep(TransformerMixin, BaseEstimator):
    """Base of the fit steps: evaluates `of` (after the matrix nodes it needs) or takes y."""

    def _values(self, X, y=None) -> np.ndarray:
        if self.of is None:
            if y is None:
                raise ValueError(f"feature_manager: {self.name!r} fits on @target but fit() got no y")
            values = np.asarray(y, dtype=np.float64)
            if len(values) != len(X):
                raise ValueError(f"feature_manager: {self.name!r}: y has {len(values)} rows, X {len(X)}")
            return values
        scope = _Scope(X)
        scope.run(self.prefix)
        return scope.eval(self.of)


class GroupStat(_FitStep):
    """fit: median/mean/iqr of `of` per `by` group (or overall), frozen. transform: a
    lookup; a group the table lacks gets `default`."""

    def __init__(self, name="", stat="median", of=None, by=(), prefix=(), smoothing=None,
                 min_count=None, default="overall", missing_key="default"):
        self.name, self.stat, self.of, self.by, self.prefix = name, stat, of, by, prefix
        self.smoothing, self.min_count, self.default = smoothing, min_count, default
        self.missing_key = missing_key

    def fit(self, X, y=None):
        values = pd.Series(self._values(X, y), index=X.index)
        overall = _stat(values, self.stat)
        if not self.by:
            self.table_, self.default_ = {}, overall
            return self
        groups = values.groupby(group_keys(X, self.by, self.missing_key))
        table, counts = _stat(groups, self.stat), groups.count()
        if self.smoothing:
            # The alpha form, not (n*mean + s*overall)/(n + s): equal in exact arithmetic,
            # but the last bit differs, and a fitted transform downstream can amplify it.
            alpha = counts / (counts + self.smoothing)
            table = alpha * table + (1 - alpha) * overall
        if self.min_count:
            table = table[counts >= self.min_count]
        table = table.dropna()
        self.table_ = table.to_dict()
        self.default_ = {"overall": overall, "nan": np.nan,
                         "table_mean": float(table.mean())}[self.default]
        return self

    def transform(self, X):
        check_is_fitted(self, "table_")
        if not self.by:
            out = np.full(len(X), self.default_)
        else:
            out = (group_keys(X, self.by, self.missing_key).map(self.table_).astype("Float64")
                   .fillna(self.default_).to_numpy(np.float64, na_value=np.nan))
        return with_columns(X, {self.name: out})


class Rank(_FitStep):
    """fit: the sorted values of `of`, per `by` group and overall. transform: the share of
    a row's group's fit values below it (side="left") or at-or-below it ("right"); a group
    fit never saw ranks against the overall values."""

    def __init__(self, name="", of=None, by=(), prefix=(), side="right", missing_key="default"):
        self.name, self.of, self.by, self.prefix = name, of, by, prefix
        self.side, self.missing_key = side, missing_key

    def fit(self, X, y=None):
        values = self._values(X, y)
        present = ~np.isnan(values)  # a missing value is not ranked against
        self.overall_ = np.sort(values[present])
        self.grids_ = {}
        if self.by:
            keys = group_keys(X, self.by, self.missing_key)
            self.grids_ = {k: np.sort(values[(keys == k).to_numpy(bool, na_value=False) & present])
                           for k in keys.dropna().unique()}
        return self

    def transform(self, X):
        check_is_fitted(self, "overall_")
        values = self._values(X)
        out = np.empty(len(X))
        keys = (group_keys(X, self.by, self.missing_key) if self.by
                else pd.Series(pd.NA, index=X.index, dtype="string"))
        for key in keys.unique():
            rows = (keys.isna() if pd.isna(key) else keys == key).to_numpy(bool, na_value=False)
            grid = self.overall_ if pd.isna(key) else self.grids_.get(key, self.overall_)
            out[rows] = np.searchsorted(grid, values[rows], side=self.side) / max(len(grid), 1)
        out[np.isnan(values)] = np.nan
        return with_columns(X, {self.name: out})


class CategoryCode(TransformerMixin, BaseEstimator):
    """fit: the sorted distinct values of `column` (any type). transform: each row's index
    among them; -1 for a value fit never saw, or a missing one."""

    def __init__(self, name="", column=""):
        self.name, self.column = name, column

    def fit(self, X, y=None):
        self.categories_ = pd.Categorical(X[self.column]).categories
        return self

    def transform(self, X):
        check_is_fitted(self, "categories_")
        codes = pd.Categorical(X[self.column], categories=self.categories_).codes
        return with_columns(X, {self.name: codes.astype(np.float64)})


class YeoJohnson(_FitStep):
    """fit: a Yeo-Johnson power transform of `of` (unstandardized). transform: applies it."""

    def __init__(self, name="", of=None, prefix=()):
        self.name, self.of, self.prefix = name, of, prefix

    def fit(self, X, y=None):
        from sklearn.preprocessing import PowerTransformer
        self.transformer_ = PowerTransformer(method="yeo-johnson", standardize=False)
        self.transformer_.fit(self._values(X, y)[:, None])
        return self

    def transform(self, X):
        check_is_fitted(self, "transformer_")
        return with_columns(X, {self.name: self.transformer_.transform(self._values(X)[:, None])[:, 0]})


class Lookup(TransformerMixin, BaseEstimator):
    """Stateless: each row's `column` value, as text, mapped through `table`; `otherwise`
    (None = NaN) for a value it lacks or a missing one."""

    def __init__(self, name="", column="", table=None, otherwise=None):
        self.name, self.column, self.table, self.otherwise = name, column, table, otherwise

    def fit(self, X, y=None):
        self.fitted_ = True
        return self

    def transform(self, X):
        check_is_fitted(self, "fitted_")
        if self.column not in X.columns:
            raise KeyError(f"feature_manager: column {self.column!r} is not in the input frame")
        mapped = X[self.column].astype("string").map(self.table).astype("Float64")
        fill = np.nan if self.otherwise is None else self.otherwise
        return with_columns(X, {self.name: mapped.fillna(fill).to_numpy(np.float64, na_value=np.nan)})


def _stat(values, stat: str):
    """For a Series, a float; for a GroupBy, a Series per group."""
    if stat == "iqr":
        return values.quantile(0.75) - values.quantile(0.25)
    result = getattr(values, stat)()
    return float(result) if np.ndim(result) == 0 else result


class Quantile(_FitStep):
    """fit: the q-quantile over every non-NaN cell of `of` (a column or a whole matrix),
    frozen. transform: that value on every row."""

    def __init__(self, name="", of=None, q=0.5, prefix=()):
        self.name, self.of, self.q, self.prefix = name, of, q, prefix

    def fit(self, X, y=None):
        with warnings.catch_warnings():  # an all-NaN `of` is a NaN quantile, not a warning
            warnings.simplefilter("ignore", RuntimeWarning)
            self.value_ = float(np.nanquantile(np.ravel(self._values(X, y)), self.q))
        return self

    def transform(self, X):
        check_is_fitted(self, "value_")
        return with_columns(X, {self.name: np.full(len(X), self.value_)})


class QuantileBin(_FitStep):
    """fit: the values of `of` at quantiles `q` become bin edges, the outermost two
    widened to -inf/+inf so a later value outside the fit range lands in an end bin.
    transform: bin number 1..k, NaN -> 0."""

    def __init__(self, name="", of=None, q=(), prefix=()):
        self.name, self.of, self.q, self.prefix = name, of, q, prefix

    def fit(self, X, y=None):
        edges = np.unique(pd.Series(self._values(X, y)).quantile(list(self.q)).to_numpy())
        edges = edges[np.isfinite(edges)].astype(np.float64)
        if len(edges) < 2:
            edges = np.array([-np.inf, np.inf])
        edges[0], edges[-1] = -np.inf, np.inf
        self.edges_ = edges
        return self

    def transform(self, X):
        check_is_fitted(self, "edges_")
        codes = pd.cut(self._values(X), bins=self.edges_, labels=False, include_lowest=True)
        out = np.where(np.isnan(codes), 0, codes + 1).astype(np.int64)
        return with_columns(X, {self.name: out})


class CustomStep(TransformerMixin, BaseEstimator):
    """Wraps a model.py function fn(frame) -> frame carrying `outputs` on the same index.
    Stateless; anything it learns would have to live in model.py."""

    def __init__(self, name="", fn=None, outputs=(), batch_dependent=False):
        self.name, self.fn, self.outputs, self.batch_dependent = name, fn, outputs, batch_dependent

    def fit(self, X, y=None):
        self.fitted_ = True
        return self

    def transform(self, X):
        check_is_fitted(self, "fitted_")
        result = self.fn(X)
        missing = [c for c in self.outputs if c not in result.columns]
        if missing or not result.index.equals(X.index):
            raise ValueError(f"feature_manager: custom {self.name!r} must return the input's index "
                             f"with columns {list(self.outputs)}; missing {missing}")
        return with_columns(X, {c: result[c].to_numpy() for c in self.outputs})


class DropColumns(TransformerMixin, BaseEstimator):
    def __init__(self, columns=()):
        self.columns = columns

    def fit(self, X, y=None):
        self.fitted_ = True
        return self

    def transform(self, X):
        check_is_fitted(self, "fitted_")
        # An aggregation only SQL read never reached the frame.
        return X.drop(columns=[c for c in self.columns if c in X.columns])


class DropPrefixed(TransformerMixin, BaseEstimator):
    """Drops the feature store's columns for the spec's SQL values once they are read."""

    def __init__(self, prefix=""):
        self.prefix = prefix

    def fit(self, X, y=None):
        self.fitted_ = True
        return self

    def transform(self, X):
        check_is_fitted(self, "fitted_")
        return X.drop(columns=[c for c in X.columns if c.startswith(self.prefix)])


def build(plan: Plan) -> Pipeline:
    by_name = {n.name: n for n in plan.nodes}
    order = {name: i for i, name in enumerate(by_name)}
    steps, block = [], []

    def flush():
        if block:
            nodes = _matrix_deps(block, by_name, order) + block
            steps.append(("exprs", ExprBlock(nodes=tuple(nodes))))
            block.clear()

    for node in plan.nodes:
        if node.kind in (EXPR, AGG):
            block.append(node)
            continue
        flush()
        p = node.params
        if node.kind == CUSTOM:
            steps.append((node.name, CustomStep(node.name, p["fn"], node.produces, p["batch_dependent"])))
            continue
        if node.kind == LOOKUP:
            steps.append((node.name, Lookup(node.name, p["column"], p["table"], p["otherwise"])))
            continue
        if p["stat"] == "code":
            steps.append((node.name, CategoryCode(node.name, p["column"])))
            continue
        prefix = tuple(_matrix_deps([node], by_name, order))
        if p["stat"] == "quantile_bin":
            step = QuantileBin(node.name, node.expr, p["q"], prefix)
        elif p["stat"] == "quantile":
            step = Quantile(node.name, node.expr, p["q"], prefix)
        elif p["stat"] == "rank":
            step = Rank(node.name, node.expr, p["by"], prefix, p["side"], p["missing_key"])
        elif p["stat"] == "yeo_johnson":
            step = YeoJohnson(node.name, node.expr, prefix)
        else:
            step = GroupStat(node.name, p["stat"], node.expr, p["by"], prefix, p["smoothing"],
                             p["min_count"], p["default"], p["missing_key"])
        steps.append((node.name, step))
    flush()
    if plan.intermediates:
        steps.append(("drop_intermediates", DropColumns(plan.intermediates)))
    if plan.sql_prefix:
        steps.append(("drop_sql_columns", DropPrefixed(plan.sql_prefix)))
    # Pipeline step names must be unique and free of "__" (its param separator).
    return Pipeline([(f"{i:02d}_{label.replace('__', '_')}", step)
                     for i, (label, step) in enumerate(steps)])


def _matrix_deps(nodes: list[Node], by_name: dict, order: dict) -> list[Node]:
    """The matrix nodes `nodes` read that are declared outside them, in declaration order;
    those were never written to the frame, so the reading step recomputes them."""
    have = {n.name for n in nodes}
    needed: list[Node] = []

    def visit(node):
        for read in node.reads:
            dep = by_name.get(read)
            if dep is not None and dep.shape == MATRIX and dep.name not in have:
                have.add(dep.name)
                visit(dep)
                needed.append(dep)

    for node in nodes:
        visit(node)
    return sorted(needed, key=lambda n: order[n.name])

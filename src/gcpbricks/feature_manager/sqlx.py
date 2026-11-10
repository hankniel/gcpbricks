"""A small SQL expression builder over sqlglot's AST, for sql.py's programs: the handful
of operations its numpy rules are written in, rendered in each engine's dialect.

Not Ibis: a program's layers are `SELECT *, <new columns> FROM (<the layer before>)`,
which carry every earlier column at no cost in the query's text; Ibis spells each one out
in every layer, and a few hundred columns over dozens of layers is past BigQuery's query
length limit.
"""
from __future__ import annotations

import math

from sqlglot import exp

_DOUBLE = exp.DataType.build("double")


class Q:
    """One SQL scalar expression."""

    __slots__ = ("e",)

    def __init__(self, e: exp.Expression):
        self.e = e

    __hash__ = object.__hash__

    def _bin(self, cls, other, reverse=False):
        a, b = (q(other), self.e) if reverse else (self.e, q(other))
        return Q(cls(this=_paren(a), expression=_paren(b)))

    def __add__(self, o): return self._bin(exp.Add, o)
    def __radd__(self, o): return self._bin(exp.Add, o, True)
    def __sub__(self, o): return self._bin(exp.Sub, o)
    def __rsub__(self, o): return self._bin(exp.Sub, o, True)
    def __mul__(self, o): return self._bin(exp.Mul, o)
    def __rmul__(self, o): return self._bin(exp.Mul, o, True)
    def __truediv__(self, o): return self._bin(exp.Div, o)
    def __rtruediv__(self, o): return self._bin(exp.Div, o, True)
    def __lt__(self, o): return self._bin(exp.LT, o)
    def __le__(self, o): return self._bin(exp.LTE, o)
    def __gt__(self, o): return self._bin(exp.GT, o)
    def __ge__(self, o): return self._bin(exp.GTE, o)
    def __eq__(self, o): return self._bin(exp.EQ, o)  # SQL =, not identity
    def __ne__(self, o): return self._bin(exp.NEQ, o)
    def __and__(self, o): return self._bin(exp.And, o)
    def __rand__(self, o): return self._bin(exp.And, o, True)
    def __or__(self, o): return self._bin(exp.Or, o)
    def __ror__(self, o): return self._bin(exp.Or, o, True)
    def __invert__(self): return Q(exp.Not(this=_paren(self.e)))
    def __neg__(self): return Q(exp.Neg(this=_paren(self.e)))

    def isnan(self): return Q(exp.IsNan(this=self.e))
    def abs(self): return Q(exp.Abs(this=self.e))
    def ln(self): return Q(exp.Ln(this=self.e))
    def exp(self): return Q(exp.Exp(this=self.e))
    def sqrt(self): return Q(exp.Sqrt(this=self.e))
    def atan2(self, x): return Q(exp.Anonymous(this="ATAN2", expressions=[self.e, q(x)]))
    def cast(self, _type="float64"): return Q(exp.Cast(this=self.e, to=_DOUBLE.copy()))


def _paren(e: exp.Expression) -> exp.Expression:
    # Not by isinstance(e, exp.Func): sqlglot's AND and OR are Funcs too.
    if isinstance(e, (exp.Binary, exp.Connector, exp.Not, exp.Neg)):
        return exp.Paren(this=e)
    return e


def q(v) -> exp.Expression:
    """A value's expression: a Q's own, or a Python constant's literal."""
    if isinstance(v, Q):
        return v.e
    if isinstance(v, bool):
        return exp.true() if v else exp.false()
    v = float(v)
    if math.isnan(v) or math.isinf(v):
        text = "NaN" if math.isnan(v) else ("Inf" if v > 0 else "-Inf")
        return exp.Cast(this=exp.Literal.string(text), to=_DOUBLE.copy())
    # A bare number: DuckDB and Spark read 0.5 as a DECIMAL, which a DOUBLE operand turns
    # back into the same double; an exponent form is a double everywhere.
    return exp.Literal.number(repr(v))


def overflowing(op: str, a, b) -> Q:
    """a + b or a * b, which BigQuery raises on when it overflows a double and the other
    engines make +-inf (render() spells it per dialect)."""
    return Q(exp.Anonymous(this=f"__OVERFLOWING_{op.upper()}", expressions=[q(a), q(b)]))


def _spell_overflowing(n: exp.Expression, dialect: str) -> exp.Expression:
    if not (isinstance(n, exp.Anonymous) and str(n.this).startswith("__OVERFLOWING_")):
        return n
    a, b = n.expressions
    op = str(n.this).removeprefix("__OVERFLOWING_")
    if dialect != "bigquery":
        return exp.Paren(this=(exp.Add if op == "ADD" else exp.Mul)(this=_paren(a), expression=_paren(b)))
    # SAFE_ADD/SAFE_MULTIPLY are NULL on an overflow; neither side is NULL (a NULL reads as
    # NaN), so a NULL is one, signed as the result would have been.
    pos = exp.GT(this=a.copy(), expression=exp.Literal.number(0))
    sign = (pos if op == "ADD" else exp.EQ(this=exp.Paren(this=pos),
                                           expression=exp.Paren(this=exp.GT(this=b.copy(), expression=exp.Literal.number(0)))))
    inf = exp.If(this=sign, true=q(float("inf")), false=q(float("-inf")))
    safe = exp.Anonymous(this="SAFE_ADD" if op == "ADD" else "SAFE_MULTIPLY", expressions=[a, b])
    return exp.Coalesce(this=safe, expressions=[inf])


def col(name: str) -> Q:
    return Q(exp.column(name, quoted=True))


def ifelse(cond, a, b) -> Q:
    return Q(exp.If(this=q(cond), true=q(a), false=q(b)))


def coalesce(a, b) -> Q:
    return Q(exp.Coalesce(this=q(a), expressions=[q(b)]))


def greatest(*values) -> Q:
    return Q(exp.Greatest(this=q(values[0]), expressions=[q(v) for v in values[1:]]))


def least(*values) -> Q:
    return Q(exp.Least(this=q(values[0]), expressions=[q(v) for v in values[1:]]))


def render(tree: exp.Expression, dialect: str) -> str:
    """tree in `dialect` (bigquery, spark or duckdb). BigQuery raises on a double
    overflowing a division; its IEEE_DIVIDE gives +-inf, as numpy does."""
    # Deepest first: a node spelled is not walked again, and its arguments may need it.
    for node in reversed(list(tree.walk(bfs=True))):
        new = _spell_overflowing(node, dialect)
        if dialect == "bigquery" and isinstance(new, exp.Div):
            new = exp.Anonymous(this="IEEE_DIVIDE", expressions=[new.this, new.expression])
        if new is not node:
            node.replace(new)
            if node is tree:
                tree = new
    return tree.sql(dialect=dialect)

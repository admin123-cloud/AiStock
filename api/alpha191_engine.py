from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

import numpy as np
import pandas as pd


class Alpha191FormulaError(ValueError):
    pass


class Alpha191UnsupportedError(ValueError):
    pass


@dataclass
class Token:
    kind: str
    value: str


@dataclass
class Expr:
    kind: str
    value: Any = None
    args: Optional[List["Expr"]] = None


_TOKEN_RE = re.compile(
    r"""
    (?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<op>\|\||&&|==|!=|>=|<=|[+\-*/^<>=?:(),&|])
    """,
    re.VERBOSE,
)

_FIELD_ALIASES = {
    "OPEN": "open",
    "HIGH": "high",
    "LOW": "low",
    "L": "low",
    "CLOSE": "close",
    "VOLUME": "volume",
    "VOL": "volume",
    "AMOUNT": "amount",
    "VWAP": "vwap",
    "RET": "ret",
    "RETURNS": "ret",
    "DTM": "dtm",
    "DBM": "dbm",
    "HD": "hd",
    "LD": "ld",
    "TR": "tr",
    "BENCHMARKINDEXCLOSE": "benchmark_close",
    "BENCHMARKINDEXOPEN": "benchmark_open",
}

_UNSUPPORTED_FIELDS = {"MKT", "SMB", "HML", "SELF"}


def required_external_fields(formula: str) -> List[str]:
    cleaned = _clean_formula(formula)
    names = set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", cleaned))
    return sorted(names & _UNSUPPORTED_FIELDS)


def normalize_formula(formula: str) -> str:
    return _clean_formula(formula)


def evaluate_alpha191_formula(df: pd.DataFrame, formula: str) -> pd.Series:
    ctx = _EvalContext(df)
    parser = _Parser(_tokenize(_clean_formula(formula)))
    expr = parser.parse()
    result = ctx.eval(expr)
    if isinstance(result, pd.Series):
        return pd.to_numeric(result, errors="coerce").reindex(df.index)
    return pd.Series(float(result), index=df.index, dtype=float)


def enrich_alpha191_fields(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values(["code", "date"]).copy()
    d["vwap"] = d["amount"] / d["volume"].replace(0, np.nan)
    invalid = ~np.isfinite(d["vwap"]) | (d["vwap"] <= 0)
    d.loc[invalid, "vwap"] = (d.loc[invalid, "open"] + d.loc[invalid, "high"] + d.loc[invalid, "low"] + d.loc[invalid, "close"]) / 4.0
    g = d.groupby("code", group_keys=False)
    prev_close = g["close"].shift(1)
    prev_open = g["open"].shift(1)
    d["ret"] = d["close"] / prev_close - 1.0
    d["dtm"] = np.where(
        d["open"] <= prev_open,
        0.0,
        np.maximum(d["high"] - d["open"], d["open"] - prev_open),
    )
    d["dbm"] = np.where(
        d["open"] >= prev_open,
        0.0,
        np.maximum(d["open"] - d["low"], prev_open - d["open"]),
    )
    d["hd"] = d["high"] - g["high"].shift(1)
    d["ld"] = g["low"].shift(1) - d["low"]
    d["tr"] = np.maximum.reduce(
        [
            (d["high"] - d["low"]).to_numpy(dtype=float),
            (d["high"] - prev_close).abs().to_numpy(dtype=float),
            (d["low"] - prev_close).abs().to_numpy(dtype=float),
        ]
    )
    if "benchmark_close" not in d.columns:
        d["benchmark_close"] = np.nan
    if "benchmark_open" not in d.columns:
        d["benchmark_open"] = np.nan
    return d


def _clean_formula(formula: str) -> str:
    text = str(formula or "").strip().rstrip(";")
    if text.startswith("-20*"):
        return (
            "-20*(20-1)^1.5*SUM(CLOSE/DELAY(CLOSE, 1)-1-MEAN(CLOSE/DELAY(CLOSE, 1)-1, 20), 20)"
            "/((20-1)*(20-2)*(SUM((CLOSE/DELAY(CLOSE, 1)-1-MEAN(CLOSE/DELAY(CLOSE, 1)-1, 20))^2, 20))^1.5)"
        )
    replacements = {
        "？": "?",
        "：": ":",
        "（": "(",
        "）": ")",
        "，": ",",
        "–": "-",
        "—": "-",
        ". /": "/",
        "./": "/",
        "./": "/",
        ".*": "*",
        ". *": "*",
        "DELA Y": "DELAY",
        "DE LAY": "DELAY",
        "DELAT": "DELTA",
        "SM A": "SMA",
        "VOL UME": "VOLUME",
        "BANCHMARK": "BENCHMARK",
        "BENCHMARKIN DEX": "BENCHMARKINDEX",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("STD(CLOSE:20), 0", "STD(CLOSE, 20):0")
    text = text.replace("SIGN(DELTA(CLOSE, 7)) : (-1 * VOLUME)", "SIGN(DELTA(CLOSE, 7))) : (-1 * VOLUME)")
    text = re.sub(r"STD\(\s*CLOSE\s*:\s*(\d+)\s*\)", r"STD(CLOSE, \1)", text)
    text = re.sub(r"\bOR\b", "||", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\)(?=\()", ")*", text)
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip()
    return text


def _tokenize(formula: str) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    while pos < len(formula):
        if formula[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(formula, pos)
        if not match:
            raise Alpha191FormulaError(f"Cannot parse formula near: {formula[pos:pos + 40]}")
        kind = "number" if match.group("number") else "ident" if match.group("ident") else "op"
        tokens.append(Token(kind, match.group(kind)))
        pos = match.end()
    tokens.append(Token("eof", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Expr:
        expr = self._ternary()
        if self._peek().kind != "eof":
            raise Alpha191FormulaError(f"Unexpected token: {self._peek().value}")
        return expr

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _take(self, value: Optional[str] = None) -> Token:
        token = self._peek()
        if value is not None and token.value != value:
            raise Alpha191FormulaError(f"Expected {value}, got {token.value}")
        self.pos += 1
        return token

    def _match(self, *values: str) -> bool:
        if self._peek().value in values:
            self.pos += 1
            return True
        return False

    def _ternary(self) -> Expr:
        expr = self._logical_or()
        if self._match("?"):
            yes = self._ternary()
            self._take(":")
            no = self._ternary()
            return Expr("ternary", args=[expr, yes, no])
        return expr

    def _logical_or(self) -> Expr:
        expr = self._logical_and()
        while self._match("||", "|"):
            expr = Expr("binary", "or", [expr, self._logical_and()])
        return expr

    def _logical_and(self) -> Expr:
        expr = self._comparison()
        while self._match("&&", "&"):
            expr = Expr("binary", "and", [expr, self._comparison()])
        return expr

    def _comparison(self) -> Expr:
        expr = self._additive()
        while self._peek().value in {"<", ">", "<=", ">=", "=", "==", "!="}:
            op = self._take().value
            expr = Expr("binary", "==" if op == "=" else op, [expr, self._additive()])
        return expr

    def _additive(self) -> Expr:
        expr = self._multiplicative()
        while self._peek().value in {"+", "-"}:
            expr = Expr("binary", self._take().value, [expr, self._multiplicative()])
        return expr

    def _multiplicative(self) -> Expr:
        expr = self._power()
        while self._peek().value in {"*", "/"}:
            expr = Expr("binary", self._take().value, [expr, self._power()])
        return expr

    def _power(self) -> Expr:
        expr = self._unary()
        if self._match("^"):
            expr = Expr("binary", "^", [expr, self._power()])
        return expr

    def _unary(self) -> Expr:
        if self._match("+"):
            return self._unary()
        if self._match("-"):
            return Expr("unary", "-", [self._unary()])
        return self._primary()

    def _primary(self) -> Expr:
        token = self._peek()
        if token.kind == "number":
            self._take()
            return Expr("number", float(token.value))
        if token.kind == "ident":
            name = self._take().value.upper()
            if self._match("("):
                args: List[Expr] = []
                if not self._match(")"):
                    while True:
                        args.append(self._ternary())
                        if self._match(")"):
                            break
                        self._take(",")
                return Expr("call", name, args)
            return Expr("ident", name)
        if self._match("("):
            expr = self._ternary()
            self._take(")")
            return expr
        raise Alpha191FormulaError(f"Unexpected token: {token.value}")


class _EvalContext:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df
        self.index = df.index

    def eval(self, expr: Expr) -> Any:
        if expr.kind == "number":
            return expr.value
        if expr.kind == "ident":
            return self._ident(str(expr.value))
        if expr.kind == "unary":
            return -self.eval(expr.args[0])
        if expr.kind == "binary":
            left = self.eval(expr.args[0])
            right = self.eval(expr.args[1])
            return self._binary(str(expr.value), left, right)
        if expr.kind == "ternary":
            cond = self._as_bool(self.eval(expr.args[0]))
            yes = self._series_like(self.eval(expr.args[1]))
            no = self._series_like(self.eval(expr.args[2]))
            return pd.Series(np.where(cond, yes, no), index=self.index)
        if expr.kind == "call":
            return self._call(str(expr.value), expr.args or [])
        raise Alpha191FormulaError(f"Unknown expression kind: {expr.kind}")

    def _ident(self, name: str) -> Any:
        if name in _UNSUPPORTED_FIELDS:
            raise Alpha191UnsupportedError(f"Missing external Alpha191 field: {name}")
        if name == "SEQUENCE":
            return ("sequence", None)
        col = _FIELD_ALIASES.get(name)
        if col and col in self.df.columns:
            return self.df[col]
        raise Alpha191FormulaError(f"Unknown identifier: {name}")

    def _call(self, name: str, arg_exprs: List[Expr]) -> Any:
        if name == "SEQUENCE":
            n = int(self.eval(arg_exprs[0])) if arg_exprs else None
            return ("sequence", n)
        args = [self.eval(arg) for arg in arg_exprs]
        if name in {"SUM", "MEAN", "MA", "STD", "PROD", "TSMAX", "TSMIN", "COUNT", "TSRANK", "DELAY", "DELTA", "DECAYLINEAR", "WMA", "HIGHDAY", "LOWDAY", "SUMAC"}:
            return self._time_func(name, args)
        if name == "SMEAN":
            name = "SMA"
        if name == "SMA":
            return self._sma(args)
        if name == "RANK":
            return self._rank(self._series_like(args[0]))
        if name in {"ABS", "LOG", "SIGN"}:
            s = self._series_like(args[0])
            if name == "ABS":
                return s.abs()
            if name == "LOG":
                return np.log(s.replace(0, np.nan))
            return np.sign(s)
        if name in {"MAX", "MIN"}:
            return self._max_min(name, args)
        if name in {"CORR", "COVIANCE"}:
            return self._rolling_pair(args, "corr" if name == "CORR" else "cov")
        if name == "REGBETA":
            return self._regbeta(args)
        if name == "REGRESI":
            raise Alpha191UnsupportedError("REGRESI with MKT/SMB/HML is not available yet")
        if name == "SUMIF":
            value, n, cond = args[0], int(args[1]), self._as_bool(args[2])
            return self._rolling(self._series_like(value).where(cond), n, "sum")
        if name == "FILTER":
            return self._series_like(args[0]).where(self._as_bool(args[1]))
        raise Alpha191FormulaError(f"Unsupported function: {name}")

    def _binary(self, op: str, left: Any, right: Any) -> Any:
        l = self._series_like(left)
        r = self._series_like(right)
        if op == "+":
            return l + r
        if op == "-":
            return l - r
        if op == "*":
            return l * r
        if op == "/":
            return l / r.replace(0, np.nan)
        if op == "^":
            return np.power(l, r)
        if op == "<":
            return l < r
        if op == ">":
            return l > r
        if op == "<=":
            return l <= r
        if op == ">=":
            return l >= r
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        if op == "and":
            return self._as_bool(l) & self._as_bool(r)
        if op == "or":
            return self._as_bool(l) | self._as_bool(r)
        raise Alpha191FormulaError(f"Unsupported operator: {op}")

    def _series_like(self, value: Any) -> pd.Series:
        if isinstance(value, pd.Series):
            return value.reindex(self.index)
        if isinstance(value, tuple) and value[0] == "sequence":
            raise Alpha191FormulaError("SEQUENCE can only be used inside REGBETA")
        return pd.Series(value, index=self.index)

    def _as_bool(self, value: Any) -> pd.Series:
        if isinstance(value, pd.Series):
            return value.reindex(self.index).fillna(False).astype(bool)
        return pd.Series(bool(value), index=self.index)

    def _by_code(self, s: pd.Series) -> pd.core.groupby.SeriesGroupBy:
        return s.groupby(self.df["code"], group_keys=False)

    def _rolling(self, s: pd.Series, n: int, method: str) -> pd.Series:
        n = max(1, int(n))
        g = self._by_code(s)
        if method == "sum":
            return g.transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).sum())
        if method == "mean":
            return g.transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).mean())
        if method == "std":
            return g.transform(lambda x: x.rolling(n, min_periods=min(n, max(1, min(n, 3)))).std())
        if method == "prod":
            return g.transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).apply(np.prod, raw=True))
        if method == "max":
            return g.transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).max())
        if method == "min":
            return g.transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).min())
        raise Alpha191FormulaError(f"Unsupported rolling method: {method}")

    def _time_func(self, name: str, args: List[Any]) -> pd.Series:
        s = self._series_like(args[0])
        if name == "STD" and len(args) == 1:
            return s.groupby(self.df["date"], group_keys=False).transform("std")
        n = int(args[1]) if len(args) > 1 else 1
        if name == "DELAY":
            return self._by_code(s).shift(n)
        if name == "DELTA":
            return self._by_code(s).diff(n)
        if name in {"SUM", "COUNT"}:
            return self._rolling(s.astype(float), n, "sum")
        if name in {"MEAN", "MA"}:
            return self._rolling(s, n, "mean")
        if name == "STD":
            return self._rolling(s, n, "std")
        if name == "PROD":
            return self._rolling(s, n, "prod")
        if name == "TSMAX":
            return self._rolling(s, n, "max")
        if name == "TSMIN":
            return self._rolling(s, n, "min")
        if name == "TSRANK":
            return self._by_code(s).transform(lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).rank(method="average", pct=True))
        if name in {"DECAYLINEAR", "WMA"}:
            weights = np.arange(1, n + 1, dtype=float)
            weights = weights / weights.sum()
            return self._rolling_apply(s, n, lambda a: float(np.dot(a, weights[-len(a):])))
        if name == "HIGHDAY":
            return self._rolling_apply(s, n, lambda a: float(len(a) - 1 - np.nanargmax(a)) if np.isfinite(a).any() else np.nan)
        if name == "LOWDAY":
            return self._rolling_apply(s, n, lambda a: float(len(a) - 1 - np.nanargmin(a)) if np.isfinite(a).any() else np.nan)
        if name == "SUMAC":
            return self._by_code(s).cumsum()
        raise Alpha191FormulaError(f"Unsupported time function: {name}")

    def _rolling_apply(self, s: pd.Series, n: int, func: Any) -> pd.Series:
        n = max(1, int(n))
        return self._by_code(s).transform(
            lambda x: x.rolling(n, min_periods=max(1, min(n, 2))).apply(lambda a: func(np.asarray(a, dtype=float)), raw=True)
        )

    def _rank(self, s: pd.Series) -> pd.Series:
        return s.groupby(self.df["date"], group_keys=False).rank(method="average", pct=True)

    def _max_min(self, name: str, args: List[Any]) -> pd.Series:
        if len(args) == 1:
            return self._series_like(args[0])
        left = self._series_like(args[0])
        right = args[1]
        if np.isscalar(right) and not isinstance(right, pd.Series):
            n = int(right)
            return self._rolling(left, n, "max" if name == "MAX" else "min")
        r = self._series_like(right)
        return pd.Series(np.maximum(left, r) if name == "MAX" else np.minimum(left, r), index=self.index)

    def _rolling_pair(self, args: List[Any], method: str) -> pd.Series:
        x = self._series_like(args[0])
        y = self._series_like(args[1])
        n = int(args[2]) if len(args) > 2 else 5
        tmp = pd.DataFrame({"code": self.df["code"], "x": x, "y": y}, index=self.index)

        def calc(part: pd.DataFrame) -> pd.Series:
            roll = part["x"].rolling(n, min_periods=max(2, min(n, 3)))
            return roll.corr(part["y"]) if method == "corr" else roll.cov(part["y"])

        out = tmp.groupby("code", group_keys=False).apply(calc)
        if isinstance(out.index, pd.MultiIndex):
            out = out.reset_index(level=0, drop=True)
        return out.reindex(self.index)

    def _sma(self, args: List[Any]) -> pd.Series:
        s = self._series_like(args[0])
        n = max(1, int(args[1]))
        m = float(args[2]) if len(args) > 2 else 1.0
        alpha = max(0.0, min(1.0, m / n))
        return self._by_code(s).transform(lambda x: x.ewm(alpha=alpha, adjust=False, ignore_na=False).mean())

    def _regbeta(self, args: List[Any]) -> pd.Series:
        y = self._series_like(args[0])
        x_arg = args[1]
        if isinstance(x_arg, tuple) and x_arg[0] == "sequence":
            n = int(args[2]) if len(args) > 2 else int(x_arg[1] or 5)
            seq = np.arange(1, n + 1, dtype=float)
            seq_mean = seq.mean()
            denom = float(((seq - seq_mean) ** 2).sum())
            return self._rolling_apply(y, n, lambda a: float(np.dot(a - np.nanmean(a), seq[-len(a):] - seq[-len(a):].mean()) / denom) if len(a) == n and np.isfinite(a).all() and denom else np.nan)
        x = self._series_like(x_arg)
        n = int(args[2]) if len(args) > 2 else 5
        cov = self._rolling_pair([y, x, n], "cov")
        var = self._rolling_pair([x, x, n], "cov")
        return cov / var.replace(0, np.nan)

"""Muat & validasi spesifikasi (counter seragam, KPI, mapping vendor) dan terjemahkan ekspresi mapping ke SQL yang aman.

Ekspresi mapping hanya boleh berisi nama counter vendor, angka, + - * / dan kurung. Ia diurai (bukan disisipkan mentah ke SQL),
setiap nama counter dikutip sebagai identifier, dan setiap pembagi dibungkus NULLIF(x, 0): DuckDB menghasilkan inf, bukan NULL,
saat membagi dengan nol, dan satu inf merusak agregasi seluruh jaringan.
Operator minus harus diapit spasi, karena tanda hubung sah di dalam nama counter (mis. L.E-RAB.AbnormRel).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z](?:[\w.\-]*\w)?)|([+\-*/()]))")


class SpecError(ValueError):
    pass


def tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise SpecError(f"cannot parse expression at {expr[pos:pos + 20]!r}: {expr!r}")
        num, ident, op = m.groups()
        out.append(("num", num) if num else ("id", ident) if ident else ("op", op))
        pos = m.end()
    return out


def to_sql(expr: str, known: set[str] | None = None) -> tuple[str, set[str]]:
    """Ekspresi mapping -> (SQL, counter vendor yang dipakai). Grammar: e := t (('+'|'-') t)*; t := f (('*'|'/') f)*; f := num|id|'(' e ')'."""
    toks, i, used = tokenize(expr), 0, set()

    def peek():
        return toks[i] if i < len(toks) else ("end", "")

    def factor():
        nonlocal i
        kind, val = peek()
        if kind == "num":
            i += 1; return val
        if kind == "id":
            if known is not None and val not in known:
                raise SpecError(f"unknown vendor counter {val!r} in {expr!r} (is a minus sign missing its spaces?)")
            i += 1; used.add(val); return '"' + val.replace('"', '""') + '"'
        if (kind, val) == ("op", "("):
            i += 1; inner = sum_()
            if peek() != ("op", ")"):
                raise SpecError(f"missing ')' in {expr!r}")
            i += 1; return f"({inner})"
        raise SpecError(f"unexpected {val or 'end'!r} in {expr!r}")

    def term():
        nonlocal i
        sql = factor()
        while peek() in (("op", "*"), ("op", "/")):
            op = peek()[1]; i += 1; rhs = factor()
            sql = f"({sql} * {rhs})" if op == "*" else f"({sql} / NULLIF({rhs}, 0))"
        return sql

    def sum_():
        nonlocal i
        sql = term()
        while peek() in (("op", "+"), ("op", "-")):
            op = peek()[1]; i += 1; sql = f"({sql} {op} {term()})"
        return sql

    sql = sum_()
    if i != len(toks):
        raise SpecError(f"trailing tokens in {expr!r}")
    return sql, used


@dataclass
class Spec:
    counters: dict
    kpis: dict
    mappings: dict

    def required_by(self, kpi: str) -> set[str]:
        k = self.kpis[kpi]
        if "product_of" in k:
            return set().union(*(self.required_by(x) for x in k["product_of"]))
        names = []
        for f in ("num", "den"):
            v = k.get(f); names += v if isinstance(v, list) else [v] if v else []
        for f in ("num_expr",):
            if k.get(f):
                names += [t for _, t in tokenize(k[f]) if t in self.counters]
        return {n for n in names if n != "period_s"}

    def mapped(self, vendor: str) -> set[str]:
        return {c for c, m in self.mappings[vendor]["counters"].items() if m.get("expr")}

    def ready_vendors(self) -> list[str]:
        """Vendor dengan ekspor terdefinisi dan minimal satu counter terpetakan."""
        return [v for v, m in self.mappings.items() if m["export"].get("time_column") and self.mapped(v)]


def load(root: Path = ROOT) -> Spec:
    counters = yaml.safe_load((root / "spec/canonical_counters.yaml").read_text())["counters"]
    kpis = yaml.safe_load((root / "spec/kpis.yaml").read_text())["kpis"]
    mappings = {}
    for f in sorted((root / "mappings").glob("*.yaml")):
        m = yaml.safe_load(f.read_text())
        bad = set(m["counters"]) - set(counters)
        if bad:
            raise SpecError(f"{f.name}: counters not in the canonical model: {sorted(bad)}")
        for c, d in m["counters"].items():
            if d.get("expr"):
                to_sql(d["expr"])                                   # validasi sintaks lebih awal
        mappings[m["vendor"]] = m
    for k, d in kpis.items():
        for c in Spec(counters, kpis, {}).required_by(k):
            if c not in counters:
                raise SpecError(f"KPI {k} uses unknown counter {c}")
    return Spec(counters, kpis, mappings)

"""契约 `formula_sql` 的解析与重写——**唯一权威实现**（基于 sqlglot AST）。

`materializer` 与 `semantic/lineage` 都从这里 import，禁止各自实现正则/子串解析。
为什么必须用 AST 而不是正则：

1. 注释里的 `metric('x')` 会被正则误判成真实引用（加载期误拒契约）；
2. 字符串字面量里的 `fact_observation.` 前缀会被改写掉（静默改文案）；
3. 注释里的 `fact_event.spending` 会被当成来源引用（骨架多 UNION 一个来源）；
4. `"over(" in ...` 子串启发式会把字面量里的 `over(` 误判成窗口函数。

用 AST 后，注释与字符串字面量天然不是 Column / 函数调用节点，以上四类误判全部消失。

依赖方向：本模块只依赖 `sqlglot`（与 `contract_hash.py` 同层、同风格），
不依赖 `materializer` / `semantic`，两层均可安全 import。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import sqlglot
from sqlglot import exp

__all__ = [
    "column_refs",
    "metric_calls",
    "rewrite",
    "strip_metric_calls",
    "strip_string_literals",
    "touches",
    "uses_window",
]

#: metric('x') 调用的函数名（大小写敏感，与契约书写约定一致）。
_METRIC_FN = "metric"


def _parse(formula: str) -> exp.Expr:
    """解析契约公式（DuckDB 方言）。空文本由调用方保证不出现。"""
    return sqlglot.parse_one(formula, read="duckdb")


def _metric_call_arg(node: exp.Anonymous) -> str | None:
    """`metric('x')` 的字符串参数；不是该形态时返回 None。"""
    args = node.expressions
    if len(args) == 1 and isinstance(args[0], exp.Literal) and args[0].is_string:
        return args[0].this
    return None


def metric_calls(formula: str) -> list[str]:
    """抽取 `metric('x')` 引用的 metric_id（去重保序）。

    注释与字符串字面量里的同名文本不会命中（AST 语义）。
    """
    if not formula or not formula.strip():
        return []
    seen: dict[str, None] = {}
    for node in _parse(formula).find_all(exp.Anonymous):
        if node.this == _METRIC_FN:
            arg = _metric_call_arg(node)
            if arg is not None:
                seen.setdefault(arg, None)
    return list(seen)


def column_refs(formula: str) -> list[tuple[str, str]]:
    """抽取 `表.列` 引用（去重保序），返回 `(限定名, 列名)`。

    只认真正的 Column 节点：`metric('x')` 的参数是字符串字面量，
    不会被误判成 `表.列`；纯数字（如 `1.5`）是 Literal，也不会命中。
    """
    if not formula or not formula.strip():
        return []
    seen: dict[tuple[str, str], None] = {}
    for col in _parse(formula).find_all(exp.Column):
        if col.table:
            seen.setdefault((col.table, col.name), None)
    return list(seen)


def rewrite(
    formula: str,
    *,
    table_aliases: Mapping[str, str],
    metric_alias: Callable[[str], str] | None = None,
) -> str:
    """把契约公式改写成"可在物化求值作用域里直接求值"的裸列表达式。

    三步归一（与旧正则实现的对外语义一致，但走 AST）：

    1. `物理表名.列` → `别名.列`（如 `fact_observation.x` → `o.x`）；
    2. `metric('x')` → 裸列 `metric_alias(x)`（不传 `metric_alias` 时原样保留）；
    3. 去掉所有来源别名的列限定（`o.x` → `x`）——外层求值作用域没有表别名。

    只有限定名命中 `table_aliases`（物理名或别名）的列才会被改写；
    其他限定列原样保留，到执行时 Binder Error（fail-closed），与旧实现一致。
    """
    if not formula or not formula.strip():
        return formula
    tree = _parse(formula)
    aliases = frozenset(table_aliases.values())

    def transform(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Column) and node.table:
            if node.table in table_aliases:
                node.set("table", exp.to_identifier(table_aliases[node.table]))
        elif isinstance(node, exp.Anonymous) and node.this == _METRIC_FN and metric_alias is not None:
            arg = _metric_call_arg(node)
            if arg is not None:
                return exp.column(metric_alias(arg))
        return node

    # `replace_tree` 不访问根节点：先对根应用 transform，再改写其余节点
    # （否则 `metric('x')` 作为整个公式时不会被展开）。
    tree = exp.replace_tree(transform(tree), transform)
    for col in tree.find_all(exp.Column):
        if col.table and col.table in aliases:
            col.args.pop("table", None)
    return tree.sql(dialect="duckdb")


def touches(formula: str, table: str, table_aliases: Mapping[str, str]) -> bool:
    """公式是否引用了来源表 `table`（物理表名或它的查询别名）。

    只认真实的列引用；注释/字符串里提到表名不算。
    """
    if not formula or not formula.strip():
        return False
    alias = table_aliases.get(table)
    for col in _parse(formula).find_all(exp.Column):
        if col.table == table or (alias is not None and col.table == alias):
            return True
    return False


def uses_window(formula: str) -> bool:
    """公式是否含窗口函数（`... OVER (...)`）。"""
    if not formula or not formula.strip():
        return False
    return any(True for _ in _parse(formula).find_all(exp.Window))


def strip_metric_calls(formula: str) -> str:
    """把 `metric('x')` 替换成占位列 `__metric_ref__`，返回重写后的文本。"""
    if not formula or not formula.strip():
        return formula
    tree = _parse(formula)

    def transformer(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Anonymous) and node.this == _METRIC_FN and _metric_call_arg(node) is not None:
            return exp.column("__metric_ref__")
        return node

    tree = exp.replace_tree(transformer(tree), transformer)
    return tree.sql(dialect="duckdb")


def strip_string_literals(formula: str) -> str:
    """把字符串字面量替换成 `''`，返回重写后的文本。"""
    if not formula or not formula.strip():
        return formula
    tree = _parse(formula)

    def transformer(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Literal) and node.is_string:
            return exp.Literal.string("")
        return node

    tree = exp.replace_tree(transformer(tree), transformer)
    return tree.sql(dialect="duckdb")

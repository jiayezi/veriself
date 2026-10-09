"""血缘引用解析、上下游拓扑与"已声明 JOIN 路径"注册表。

本模块是 `semantic` 层的**基础设施**，只处理契约里的血缘字段（纯数据），
不依赖 `contract.py`，以免形成循环 import。

职责：
1. `formula_sql` 引用抽取：`metric('x')` 调用与 `表.列` 引用，
   解析统一委托 `veriself.sqlrefs`（sqlglot AST）——本模块不再持有 SQL 正则。
2. `upstream_metrics` 拓扑排序 + 环检测（契约 §2 规则 5）。
3. JOIN 路径注册表：契约 §1 DDL 里可声明的等值连接路径，供 AST 校验使用（契约 §5 第 4 条）。

`fact_subject_day` 与 `fact_metric_value` 的连接键是复合键
`(subject_id, date_key)`。`declared_join_keys` 返回的是**必须同时出现**的列，
不是可任选其一的列。只声明 `date_key` 会在多主体时把同一天的情境乘到每个主体上。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from veriself import sqlrefs

__all__ = [
    "DIMENSION_TABLES",
    "JOIN_EDGES",
    "READ_PATH_TABLES",
    "LineageError",
    "allowed_tables_for",
    "column_refs",
    "declared_join_keys",
    "is_attribute_name",
    "metric_calls",
    "source_covers",
    "source_tables",
    "strip_metric_calls",
    "strip_string_literals",
    "topological_layers",
]


class LineageError(ValueError):
    """血缘引用非法（由 `contract.py` 转成 `config.ContractError`）。"""


# ---------------------------------------------------------------- 引用抽取
#: 维度/过滤器白名单项的形状（`命名空间.属性`）——这是名字形状检查，不是 SQL 解析。
_ATTRIBUTE_RE = re.compile(r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$")


def metric_calls(formula_sql: str) -> list[str]:
    """抽取 `formula_sql` 里所有 `metric('x')` 引用的 metric_id（去重保序）。"""
    return sqlrefs.metric_calls(formula_sql or "")


def strip_metric_calls(formula_sql: str) -> str:
    """把 `metric('x')` 替换成占位列，避免其中的 metric_id 被误判成 `表.列`。"""
    return sqlrefs.strip_metric_calls(formula_sql or "")


def strip_string_literals(sql: str) -> str:
    """把字符串字面量替换成 `''`，避免把字面量里的点号当成列引用。"""
    return sqlrefs.strip_string_literals(sql or "")


def column_refs(formula_sql: str) -> list[tuple[str, str]]:
    """抽取 `表.列`（或 FROM 别名.列）引用，返回去重且保序的 `(限定名, 列名)`。

    解析走 sqlglot AST：`metric('x')` 的参数是字符串字面量、纯数字是 Literal，
    都不会被误判成 `表.列`；注释与字符串里的 `表.列` 文本也不会命中。
    """
    return sqlrefs.column_refs(formula_sql or "")


def is_attribute_name(name: str) -> bool:
    """判断是否是 `命名空间.属性` 形式（维度/过滤器/order_by 字段的形状检查）。"""
    return bool(_ATTRIBUTE_RE.match(name or ""))


def source_covers(sources: Sequence[str], qualifier: str, column: str) -> bool:
    """判断 `限定名.列` 是否被 `lineage.sources` 覆盖（契约 §2 规则 2）。

    契约把两种写法都视为合法，故本函数同时接受：
    - 精确形式：`fact_observation.sleep_hours` ∈ sources（warehouse 的 18 个 YAML 用这种）。
    - FROM 别名形式：`o.sleep_hours`，只要某条 source 的列名等于 `sleep_hours`
      （Lead 的 materializer 测试与契约 §2 示例用这种：`s.sleep_need_h` / `o.sleep_hours`）。
    若限定名本身就是一个**已声明的表名**，则要求精确匹配，别名形式不适用（更严格）。
    """
    entries = [str(s) for s in sources]
    if f"{qualifier}.{column}" in entries:
        return True
    declared_tables = {e.split(".", 1)[0] for e in entries if "." in e}
    if qualifier in declared_tables:
        return False
    return any(e.split(".", 1)[1] == column for e in entries if "." in e)


# ---------------------------------------------------------------- 拓扑
def topological_layers(upstream: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """按 `upstream_metrics` 分层（Kahn）。

    `upstream` 的键集合即"全部已注册指标"；引用了不存在的上游或存在环 → `LineageError`。
    同层内按 metric_id 排序，保证确定性。
    """
    nodes = list(upstream)
    known = set(nodes)
    indegree: dict[str, int] = {node: 0 for node in nodes}
    dependents: dict[str, list[str]] = {node: [] for node in nodes}
    for node in nodes:
        for dep in upstream[node]:
            if dep not in known:
                raise LineageError(f"metric '{node}' 的 upstream_metrics 引用了不存在的指标 '{dep}'")
            if dep == node:
                raise LineageError(f"metric '{node}' 的 upstream_metrics 自引用，构成环")
            indegree[node] += 1
            dependents[dep].append(node)

    layers: list[list[str]] = []
    ready = sorted(node for node in nodes if indegree[node] == 0)
    placed = 0
    while ready:
        layers.append(list(ready))
        placed += len(ready)
        nxt: list[str] = []
        for node in ready:
            for dependent in dependents[node]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    nxt.append(dependent)
        ready = sorted(nxt)
    if placed != len(nodes):
        stuck = sorted(node for node in nodes if indegree[node] > 0)
        raise LineageError(f"upstream_metrics 存在环，涉及指标：{stuck}")
    return layers


# ---------------------------------------------------------------- JOIN 路径注册表
#: `semantic` 编译产物允许出现的表（读路径白名单）
READ_PATH_TABLES: frozenset[str] = frozenset(
    {"fact_metric_value", "dim_date", "fact_subject_day", "dim_subject"}
)
#: 查询期可 JOIN 的表。`fact_subject_day` 是日事实，放在这里是因为它提供 `context.*` 列。
DIMENSION_TABLES: frozenset[str] = frozenset({"dim_date", "fact_subject_day", "dim_subject"})

#: 已声明等值连接路径 `(左表, 右表, 等值列)`（无向）。
#: 同一对表出现多次时，这些列必须**同时**出现在 ON 里（复合键），不是任选其一。
JOIN_EDGES: tuple[tuple[str, str, str], ...] = (
    ("fact_metric_value", "dim_date", "date_key"),
    ("fact_metric_value", "dim_subject", "subject_id"),
    ("fact_metric_value", "dim_metric", "metric_id"),
    ("fact_metric_value", "fact_subject_day", "subject_id"),
    ("fact_metric_value", "fact_subject_day", "date_key"),
    ("fact_observation", "dim_date", "date_key"),
    ("fact_observation", "dim_subject", "subject_id"),
    ("fact_observation", "dim_source", "source_id"),
    ("fact_event", "dim_date", "date_key"),
    ("fact_event", "dim_subject", "subject_id"),
    ("fact_event", "dim_source", "source_id"),
)

_EDGE_INDEX: dict[tuple[str, str], tuple[str, ...]] = {}
for _left, _right, _key in JOIN_EDGES:
    _EDGE_INDEX.setdefault((_left, _right), ())
    _EDGE_INDEX.setdefault((_right, _left), ())
    _EDGE_INDEX[(_left, _right)] += (_key,)
    _EDGE_INDEX[(_right, _left)] += (_key,)


def declared_join_keys(left: str, right: str) -> tuple[str, ...]:
    """返回两张表之间必须同时等值连接的列；无路径时返回空元组。

    返回多列时是复合键（例如 `fact_subject_day` 的 `subject_id` + `date_key`），
    调用方要检查 ON 子句把它们都写上，不能只命中其中一列。
    """
    return _EDGE_INDEX.get((left, right), ())


def source_tables(contracts: Mapping[str, object], metric_ids: Iterable[str] | None = None) -> frozenset[str]:
    """汇总指标契约 `lineage.sources` 里出现的表名。"""
    ids = list(metric_ids) if metric_ids is not None else list(contracts)
    tables: set[str] = set()
    for metric_id in ids:
        contract = contracts.get(metric_id)
        if contract is None:
            continue
        lineage = getattr(contract, "lineage", None)
        sources = getattr(lineage, "sources", None) or []
        for entry in sources:
            text = str(entry)
            if "." in text:
                tables.add(text.split(".", 1)[0])
    return frozenset(tables)


def allowed_tables_for(
    contracts: Mapping[str, object] | None = None,
    metric_ids: Iterable[str] | None = None,
    *,
    include_lineage: bool = True,
) -> frozenset[str]:
    """AST 校验用的表白名单。

    - 不给契约时：只有编译产物真正会读的表（`READ_PATH_TABLES`）+ `dim_metric`。
    - 给契约时：再加上这些指标 `lineage.sources` 里声明的表（"未声明表"即不在其中）。
    """
    allowed = set(READ_PATH_TABLES) | {"dim_metric"}
    if contracts and include_lineage:
        allowed |= set(source_tables(contracts, metric_ids))
    return frozenset(allowed)

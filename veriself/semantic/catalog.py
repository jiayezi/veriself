"""指标目录、维度/过滤器注册表（IFACE-v1 第 7 节 `list_metrics` / `describe_metric`）。

本模块只做"名字 → 物理列"的**只读映射**：白名单仍然来自每个指标的
`allowed_dimensions` / `allowed_filters`（契约 §2），注册表只负责把通过白名单的名字
解析成可安全构造的 `(表, 列)`，绝不接受客户端给的原始 SQL 片段。

命名空间、列别名和表列白名单从 `veriself/domains/person.yml` 派生，这里不再手写第二份。
`date.between` / `date.last_n_days` 仍是引擎的过滤器形态，名字由时间维命名空间拼出来。

解析不到的名字一律由调用方（`enforcement`）转成契约 §5 规定的拒绝前缀
（`dimension_not_allowed:` / `filter_not_allowed:`），即 fail-closed。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from veriself import config
from veriself.domain import default_domain
from veriself.semantic.contract import AS_OF_DEFINITION, MetricContract

__all__ = [
    "COLUMN_ALIASES",
    "NAMESPACE_TABLES",
    "ColumnRef",
    "UnresolvedName",
    "describe_metric",
    "list_metrics",
    "resolve_column",
    "resolve_filter",
]

_DOMAIN = default_domain()

# 事实表本身暴露 `metric` 与物理表名两个命名空间。列是引擎指标库的列，不进领域文件。
_TABLE_FOR_NAMESPACE: dict[str, str] = _DOMAIN.namespace_tables()
_TABLE_FOR_NAMESPACE["metric"] = _DOMAIN.metric_table
_TABLE_FOR_NAMESPACE[_DOMAIN.metric_table] = _DOMAIN.metric_table

#: 语义名 → 物理列（由领域文件派生，不带别名时直接同名）
COLUMN_ALIASES: dict[str, str] = _DOMAIN.column_aliases()

#: 兼容别名：接受的命名空间（对外文档与 `describe_metric` 输出用）
NAMESPACE_TABLES: Mapping[str, str] = _TABLE_FOR_NAMESPACE

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

#: 各表允许被引用的列。严格白名单 = fail-closed：写错列名会在编译期被拒。
TABLE_COLUMNS: dict[str, frozenset[str]] = dict(_DOMAIN.table_columns())
TABLE_COLUMNS[_DOMAIN.metric_table] = frozenset(
    {"metric_id", "value", _DOMAIN.entity_key, _DOMAIN.time_key}
)

#: 过滤器的特殊形态（其余 `命名空间.属性` 一律按等值过滤处理）。
#: 名字挂在时间维命名空间上，person 领域因此仍是 `date.between` / `date.last_n_days`。
FILTER_KINDS: Mapping[str, str] = {
    f"{_DOMAIN.time_dimension.namespace}.between": "between",
    f"{_DOMAIN.time_dimension.namespace}.last_n_days": "last_n_days",
}


class UnresolvedName(ValueError):
    """维度/过滤器名字无法解析成物理列（fail-closed，由调用方转成拒绝原因）。"""


@dataclass(frozen=True)
class ColumnRef:
    """一个可安全构造的列引用。"""

    name: str
    table: str
    column: str

    @property
    def is_pii(self) -> bool:
        """该列是否属于 `config.PII_COLUMNS`。"""
        return self.column in config.PII_COLUMNS

    @property
    def requires_join(self) -> bool:
        """是否需要额外 JOIN（时间维由编译器无条件内连接）。"""
        return self.table != _DOMAIN.time_dimension.table


def resolve_column(name: str) -> ColumnRef:
    """把 `date.weekday` 这类名字解析成 `ColumnRef`；无法解析抛 `UnresolvedName`。"""
    if not isinstance(name, str) or "." not in name:
        raise UnresolvedName(f"'{name}' 不是 '命名空间.属性' 形式")
    namespace, _, attribute = name.partition(".")
    table = _TABLE_FOR_NAMESPACE.get(namespace)
    if table is None:
        raise UnresolvedName(f"未知命名空间 '{namespace}'（可用：{sorted(set(_TABLE_FOR_NAMESPACE))}）")
    column = COLUMN_ALIASES.get(name, attribute)
    if not _IDENT_RE.match(column):
        raise UnresolvedName(f"属性名 '{attribute}' 不是合法标识符")
    known = TABLE_COLUMNS.get(table, frozenset())
    if known and column not in known:
        raise UnresolvedName(
            f"表 {table} 没有列 '{column}'（可用：{sorted(known)}）"
        )
    return ColumnRef(name=name, table=table, column=column)


def resolve_filter(name: str) -> tuple[str, ColumnRef]:
    """解析过滤器：返回 `(kind, ColumnRef)`，`kind ∈ {between, last_n_days, equals}`。"""
    kind = FILTER_KINDS.get(name)
    if kind is not None:
        relation = _DOMAIN.time_dimension
        calendar = relation.calendar_column or relation.keys[0]
        return kind, ColumnRef(
            name=f"{relation.namespace}.{calendar}",
            table=relation.table,
            column=calendar,
        )
    return "equals", resolve_column(name)


# ---------------------------------------------------------------- 目录
def list_metrics(contracts: Mapping[str, MetricContract]) -> list[dict]:
    """返回目录摘要（契约 §7）：按 metric_id 升序。"""
    return [
        {
            "metric_id": contract.metric_id,
            "display_name": contract.display_name,
            "unit": contract.unit,
            "direction": contract.direction,
            "grain": contract.grain,
            "status": contract.status,
            "version": contract.version,
        }
        for contract in sorted(contracts.values(), key=lambda item: item.metric_id)
    ]


def describe_metric(contracts: Mapping[str, MetricContract], metric_id: str) -> dict:
    """返回单个指标的完整契约 + `contract_hash` + 血缘 + 可用维度/过滤器。

    未知 metric_id 抛 `config.EnforcementError("registered", "unknown_metric: ...")`。
    """
    contract = contracts.get(metric_id)
    if contract is None:
        raise config.EnforcementError(
            "registered",
            f"{config.REASON_PREFIXES['registered']} 指标 '{metric_id}' 未注册"
            f"（已注册 {len(contracts)} 个）",
        )
    payload: dict[str, Any] = contract.model_dump()
    payload["contract_hash"] = contract.contract_hash
    payload["lineage"] = {
        "sources": list(contract.lineage.sources),
        "upstream_metrics": list(contract.lineage.upstream_metrics),
    }
    payload["as_of_definition"] = AS_OF_DEFINITION
    payload["available_dimensions"] = _resolvable(contract.allowed_dimensions)
    payload["available_filters"] = _resolvable(contract.allowed_filters)
    return payload


def _resolvable(names: list[str]) -> list[str]:
    """标出白名单里哪些名字编译器真的能物化（其余查询时会 fail-closed）。"""
    result = []
    for name in names:
        try:
            resolve_column(name)
        except UnresolvedName:
            continue
        result.append(name)
    return result

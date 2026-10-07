"""指标目录、维度/过滤器注册表（IFACE-v1 第 7 节 `list_metrics` / `describe_metric`）。

本模块只做"名字 → 物理列"的**只读映射**：白名单仍然来自每个指标的
`allowed_dimensions` / `allowed_filters`（契约 §2），注册表只负责把通过白名单的名字
解析成可安全构造的 `(表, 列)`，绝不接受客户端给的原始 SQL 片段。

解析不到的名字一律由调用方（`enforcement`）转成契约 §5 规定的拒绝前缀
（`dimension_not_allowed:` / `filter_not_allowed:`），即 fail-closed。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from veriself import config
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

_TABLE_FOR_NAMESPACE: dict[str, str] = {
    "date": "dim_date",
    "dim_date": "dim_date",
    "context": "dim_context",
    "dim_context": "dim_context",
    "subject": "dim_subject",
    "dim_subject": "dim_subject",
    #: 事实表本身只暴露 metric/date 两个语义命名空间给过滤器
    "metric": "fact_metric_value",
    "fact_metric_value": "fact_metric_value",
}

#: 语义名 → 物理列（不带别名时直接同名）
COLUMN_ALIASES: dict[str, str] = {
    "date.day": "date",
    "date.date": "date",
    "date.weekday": "weekday_name",
    # `date.weekday` 是**星期名**（"Monday"…），按它排序得到的是字母序而不是星期序。
    # 要做"周内趋势"必须用数字型 `date.day_of_week`（1=周一 … 7=周日）。
    "date.day_of_week": "day_of_week",
    "date.week": "week",
    "date.month": "month",
    "date.quarter": "quarter",
    "date.year": "year",
    "date.is_weekend": "is_weekend",
    "date.is_holiday": "is_holiday",
    "context.is_travel": "is_travel",
    "dim_context.is_travel": "is_travel",
    "context.is_illness": "is_illness",
    "dim_context.is_illness": "is_illness",
    "context.location_type": "location_type",
    "dim_context.location_type": "location_type",
    "subject.sleep_need_h": "sleep_need_h",
    "dim_subject.sleep_need_h": "sleep_need_h",
    "subject.base_weight_kg": "base_weight_kg",
    "dim_subject.base_weight_kg": "base_weight_kg",
}

#: 兼容别名：接受的命名空间（对外文档与 `describe_metric` 输出用）
NAMESPACE_TABLES: Mapping[str, str] = _TABLE_FOR_NAMESPACE

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

#: 各表允许被引用的列（契约 §1 冻结 DDL）。
#: 严格白名单 = fail-closed：YAML 里的维度/过滤器写错列名会在编译期被拒，
#: 而不是等到执行时抛 DuckDB binder 错误。
TABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "dim_date": frozenset(
        {"date_key", "date", "year", "quarter", "month", "week", "day_of_week",
         "weekday_name", "is_weekend", "is_holiday"}
    ),
    # date_key 是语义层为该表声明的连接路径列（冻结 DDL 未定义，见 lineage 模块说明）
    "dim_context": frozenset({"context_sk", "context_id", "date_key", "is_travel", "is_illness", "location_type"}),
    "dim_subject": frozenset(
        {"subject_sk", "subject_id", "name", "birth_date", "sleep_need_h", "base_weight_kg",
         "timezone", "valid_from", "valid_to", "is_current", "version", "recorded_at"}
    ),
    "fact_metric_value": frozenset({"metric_id", "subject_id", "date_key", "value"}),
}

#: 过滤器的特殊形态（其余 `命名空间.属性` 一律按等值过滤处理）
FILTER_KINDS: Mapping[str, str] = {
    "date.between": "between",
    "date.last_n_days": "last_n_days",
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
        """是否需要额外 JOIN（`dim_date` 由编译器无条件内连接）。"""
        return self.table != "dim_date"


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
        return kind, resolve_column("date.date")
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

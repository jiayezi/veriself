"""指标 SQL 的加载期检查。

只解析、不改写。失败一律 `ContractError`。指标文件里的文本就是执行文本，
这里不补过滤条件、不替换表名。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from veriself import config

__all__ = ["check_metric_sql"]

#: 指标 SQL 允许读取的关系。窄表 `fact_observation` / `fact_event` 不在其中，
#: 通道聚合留在语义模型生成的视图里。
ALLOWED_TABLES = frozenset({"obs_daily", "evt_daily", "subject_asof", "fact_metric_value"})

_FACT = "fact_metric_value"
_OUTPUT_COLUMNS = frozenset({"subject_id", "date_key", "value"})

_FACT_COLUMNS = frozenset({
    "metric_id",
    "subject_id",
    "date_key",
    "value",
    "metric_version",
    "contract_hash",
    "computed_at",
    "valid_from",
    "valid_to",
})


def check_metric_sql(
    sql: str,
    *,
    metric_id: str,
    depends_on: list[str],
    models: Mapping[str, Any] | None,
) -> tuple[tuple[str, int], ...]:
    """检查一条指标 SELECT。

    返回每个 `fact_metric_value` 读取的 `(上游 metric_id, metric_version)`。
    版本是否等于该上游的当前契约版本，由调用方在全部契约加载后核对。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise config.ContractError(f"契约 {metric_id} 的 SQL 为空")

    try:
        statements = [statement for statement in sqlglot.parse(sql, read="duckdb") if statement is not None]
    except sqlglot.ParseError as exc:
        raise config.ContractError(f"契约 {metric_id} 的 SQL 无法解析：{exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise config.ContractError(f"契约 {metric_id} 的 SQL 必须恰好是一条 SELECT")
    root = statements[0]

    names = _output_names(root, metric_id)
    missing = sorted(_OUTPUT_COLUMNS - names)
    if missing:
        raise config.ContractError(
            f"契约 {metric_id} 的 SQL 必须输出列 {sorted(_OUTPUT_COLUMNS)}，缺少 {missing}"
        )

    columns = _view_columns(models) if models else None
    reads: list[tuple[str, int]] = []
    for scope in traverse_scope(root):
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name not in ALLOWED_TABLES:
                raise config.ContractError(
                    f"契约 {metric_id} 的 SQL 不允许读取 '{source.name}'，"
                    f"只能使用 {sorted(ALLOWED_TABLES)}"
                )
        if columns is not None:
            _check_scope_columns(scope, columns, metric_id)
        select = scope.expression
        if isinstance(select, exp.Select) and _scope_reads_fact(scope):
            reads.append(_fact_read(select, metric_id))

    found = [item[0] for item in reads]
    if len(found) != len(set(found)):
        raise config.ContractError(f"契约 {metric_id} 的 SQL 重复读取了上游指标 {found}")
    if set(found) != set(depends_on):
        raise config.ContractError(
            f"契约 {metric_id} 的 depends_on {sorted(depends_on)} 与 SQL 读到的指标 {sorted(found)} 不一致"
        )
    return tuple(reads)


def _output_names(select: exp.Select, metric_id: str) -> set[str]:
    names: set[str] = set()
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            raise config.ContractError(
                f"契约 {metric_id} 的 SQL 必须显式输出 subject_id、date_key、value，不能使用 SELECT *"
            )
        alias = projection.alias_or_name
        if alias:
            names.add(str(alias))
    return names


def _view_columns(models: Mapping[str, Any]) -> dict[str, set[str]]:
    required = ("fact_observation", "fact_event", "dim_subject")
    missing = [table for table in required if table not in models]
    if missing:
        raise config.ContractError(f"语义模型缺少必需来源表: {missing}")
    return {
        "obs_daily": set(models["fact_observation"].column_names) | {"date_key", "subject_id"},
        "evt_daily": set(models["fact_event"].column_names) | {"date_key", "subject_id"},
        "subject_asof": set(models["dim_subject"].column_names) | {"subject_id", "valid_from", "valid_to"},
        _FACT: set(_FACT_COLUMNS),
    }


def _scope_reads_fact(scope: Any) -> bool:
    return any(isinstance(source, exp.Table) and source.name == _FACT for source in scope.sources.values())


def _check_scope_columns(scope: Any, columns: Mapping[str, set[str]], metric_id: str) -> None:
    alias_to_table: dict[str, str] = {}
    for alias, source in scope.sources.items():
        if isinstance(source, exp.Table):
            alias_to_table[str(alias)] = source.name
            alias_to_table[source.name] = source.name
    physical = set(alias_to_table.values())
    for column in scope.columns:
        name = column.name
        if column.table:
            table = alias_to_table.get(column.table)
            if table in columns and name not in columns[table]:
                raise config.ContractError(
                    f"契约 {metric_id} 的 SQL 引用了 '{table}.{name}'，该关系没有列 '{name}'"
                )
            continue
        if not physical:
            continue
        if not any(name in columns[table] for table in physical if table in columns):
            raise config.ContractError(
                f"契约 {metric_id} 的 SQL 引用了不存在的列 '{name}'"
            )


def _fact_read(select: exp.Select, metric_id: str) -> tuple[str, int]:
    where = select.args.get("where")
    conjuncts = _conjuncts(where.this if where is not None else None)
    metric_ids = [value for node in conjuncts if (value := _eq_string(node, "metric_id")) is not None]
    versions = [value for node in conjuncts if (value := _eq_int(node, "metric_version")) is not None]
    has_current = any(_is_null(node, "valid_to") for node in conjuncts)
    if len(metric_ids) != 1 or len(versions) != 1 or not has_current:
        raise config.ContractError(
            f"契约 {metric_id} 读取 fact_metric_value 时，WHERE 必须同时包含 "
            "metric_id = '...'、metric_version = <版本> 和 valid_to IS NULL"
        )
    return metric_ids[0], versions[0]


def _conjuncts(expression: exp.Expression | None) -> list[exp.Expression]:
    if expression is None:
        return []
    if isinstance(expression, exp.And):
        return _conjuncts(expression.left) + _conjuncts(expression.right)
    if isinstance(expression, exp.Paren):
        return _conjuncts(expression.this)
    return [expression]


def _column_name(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Column):
        return expression.name
    return None


def _eq_string(node: exp.Expression, column: str) -> str | None:
    if not isinstance(node, exp.EQ):
        return None
    left, right = node.left, node.right
    if _column_name(left) == column and isinstance(right, exp.Literal) and right.is_string:
        return str(right.this)
    if _column_name(right) == column and isinstance(left, exp.Literal) and left.is_string:
        return str(left.this)
    return None


def _eq_int(node: exp.Expression, column: str) -> int | None:
    if not isinstance(node, exp.EQ):
        return None
    left, right = node.left, node.right
    if _column_name(left) == column and isinstance(right, exp.Literal) and right.is_int:
        return int(right.this)
    if _column_name(right) == column and isinstance(left, exp.Literal) and left.is_int:
        return int(left.this)
    return None


def _is_null(node: exp.Expression, column: str) -> bool:
    if not isinstance(node, exp.Is):
        return False
    if _column_name(node.this) != column:
        return False
    return isinstance(node.expression, exp.Null)

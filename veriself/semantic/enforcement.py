"""五条强制校验（IFACE-v1 第 5 节）——本项目的差异化价值所在。

顺序**固定**且拒绝原因前缀**固定**：

===== ================ ==========================================================
顺序   校验名            拒绝前缀（`config.REASON_PREFIXES`）
===== ================ ==========================================================
1     `registered`     `unknown_metric:` / `deprecated_metric:`
2     `dimensions`     `dimension_not_allowed:` / `filter_not_allowed:`
3     `grain`          `grain_not_compatible:`
4     `ast_join_path`  `ast_violation:`（**真 sqlglot AST 解析**，不是字符串匹配）
5     `rls`            `rls_denied:`（角色无权时拒绝；有权时**改写** SQL 并记录 rls_applied）
===== ================ ==========================================================

第 4 条是本模块的硬核：即使有人绕过 `QueryRequest` 手工塞进一条 SQL，
AST 校验也会识破未声明表、子查询 / CTE / UNION 逃逸、`SELECT *`、表值函数、
未声明的 JOIN 路径与未声明函数。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from veriself import config
from veriself.semantic import lineage as lineage_mod
from veriself.semantic.catalog import (
    ColumnRef,
    UnresolvedName,
    resolve_column,
    resolve_filter,
)
from veriself.semantic.contract import MetricContract
from veriself.semantic.query import QueryRequest

__all__ = [
    "ALLOWED_FUNCTIONS",
    "BUCKET_FIELDS",
    "DimensionsPlan",
    "ParsedFilter",
    "ResolvedDimension",
    "RlsPlan",
    "bucket_field",
    "check_ast_join_path",
    "check_dimensions",
    "check_grain",
    "check_registered",
    "check_rls",
    "normalize_role",
    "validate_ast",
]

#: AST 校验允许出现的函数（其余一律 `ast_violation: 未声明的函数`）。
#: 这份白名单同时挡住了 DuckDB 的 `read_csv` / `read_parquet` / `glob` / `system` 等文件与命令面。
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_IF", "ARG_MAX", "ARG_MIN", "ANY_VALUE",
        "CASE", "IF", "IN",
        "TIMESTAMP_TRUNC", "DATE_TRUNC", "EXTRACT", "DATE_PART", "STRFTIME", "DATE",
        "CAST", "TRY_CAST", "COALESCE", "NULLIF", "IFNULL", "GREATEST", "LEAST",
        "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "SIGN", "POWER", "SQRT", "LN", "LOG", "EXP",
        "LOWER", "UPPER", "LENGTH", "TRIM", "LTRIM", "RTRIM", "SUBSTRING", "CONCAT", "REPLACE",
        "YEAR", "MONTH", "QUARTER", "WEEK", "DAYOFWEEK", "DAYOFYEAR", "ISODOW", "LAST_DAY",
        "STDDEV_POP", "STDDEV_SAMP", "VAR_POP", "VAR_SAMP", "MEDIAN", "MODE",
        "PERCENTILE_CONT", "PERCENTILE_DISC", "LIST", "ARRAY_AGG", "STRING_AGG",
    }
)

#: 第 5 条（rls）的拒绝前缀。`config.REASON_PREFIXES` 里没有 rls 键（契约 §5 该条"不拒绝"），
#: 但角色无权访问必须拒绝，这里用固定前缀 `rls_denied:`，`rule` 仍严格等于 `rls`。
RLS_DENIED_PREFIX = "rls_denied:"

#: sqlglot 里属于"运算符"而非"函数调用"的 Func 子类（不参与函数白名单检查）
_OPERATOR_TYPES: tuple[type, ...] = (exp.Connector, exp.Predicate, exp.Binary, exp.Unary)

#: 时间桶字段（`date.day` 等）：编译器统一解析成"生效粒度"的桶
BUCKET_FIELDS: frozenset[str] = frozenset(
    {"date.day", "date.date", "date.week", "date.month", "date.quarter"}
)

_POLICY_ORDER: tuple[str, ...] = ("owner_only", "aggregate_min5", "no_pii")


# ---------------------------------------------------------------- 公共小工具
def normalize_role(role: Any) -> config.Role:
    """把 `Role` 或字符串统一成 `config.Role`。"""
    if isinstance(role, config.Role):
        return role
    try:
        return config.Role(str(role))
    except ValueError as exc:
        raise config.QueryError(f"未知角色：{role!r}（合法值：{[r.value for r in config.Role]}）") from exc


def bucket_field(grain: str) -> str:
    """粒度 → 输出列名（`day` → `date.day`）。"""
    return f"date.{grain}"


def _reject(rule: str, prefix_key: str, message: str) -> config.EnforcementError:
    """构造带契约规定前缀的拒绝异常。"""
    return config.EnforcementError(rule, f"{config.REASON_PREFIXES[prefix_key]} {message}")


def _reject_rls(message: str) -> config.EnforcementError:
    """构造 rls 拒绝（`rule="rls"`，`detail` 以 `rls_denied:` 开头）。"""
    return config.EnforcementError("rls", f"{RLS_DENIED_PREFIX} {message}")


# ---------------------------------------------------------------- 1. registered
def check_registered(
    req: QueryRequest, contracts: Mapping[str, MetricContract]
) -> list[MetricContract]:
    """校验 1：指标存在且非 deprecated。"""
    metrics: list[MetricContract] = []
    for metric_id in req.metrics:
        contract = contracts.get(metric_id)
        if contract is None:
            raise _reject(
                "registered", "registered",
                f"指标 '{metric_id}' 未注册（已注册 {len(contracts)} 个）",
            )
        if contract.status == "deprecated":
            replaced_by = contract.deprecation.replaced_by if contract.deprecation else None
            raise _reject(
                "registered", "deprecated",
                f"指标 '{metric_id}' 已废弃，请改用 '{replaced_by}'",
            )
        metrics.append(contract)
    return metrics


# ---------------------------------------------------------------- 2. dimensions
@dataclass(frozen=True)
class ResolvedDimension:
    """已通过白名单并解析成物理列的维度。"""

    name: str
    table: str
    column: str


@dataclass(frozen=True)
class ParsedFilter:
    """已通过白名单并解析、校验完值的过滤器。"""

    name: str
    kind: str
    table: str
    column: str
    values: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DimensionsPlan:
    """第 2 条校验的产物。"""

    dimensions: tuple[ResolvedDimension, ...]
    filters: tuple[ParsedFilter, ...]
    order_by: tuple[tuple[str, bool], ...]

    @property
    def touched_columns(self) -> tuple[ColumnRef, ...]:
        """请求触及的物理列（用于 RLS 的 PII 检查）。"""
        refs = [ColumnRef(name=d.name, table=d.table, column=d.column) for d in self.dimensions]
        refs += [ColumnRef(name=f.name, table=f.table, column=f.column) for f in self.filters]
        return tuple(refs)


def check_dimensions(
    req: QueryRequest, contracts: Mapping[str, MetricContract], metrics: Sequence[MetricContract]
) -> DimensionsPlan:
    """校验 2：请求的 dimensions / filters / order_by 必须在各指标白名单内。"""
    dimensions: list[ResolvedDimension] = []
    for name in req.dimensions:
        for contract in metrics:
            if not contract.allow_dimension(name):
                raise _reject(
                    "dimensions", "dimension_not_allowed",
                    f"维度 '{name}' 不在指标 '{contract.metric_id}' 的 allowed_dimensions "
                    f"{list(contract.allowed_dimensions)} 中",
                )
        try:
            ref = resolve_column(name)
        except UnresolvedName as exc:
            raise _reject("dimensions", "dimension_not_allowed", f"维度 '{name}' 无法物化：{exc}") from exc
        if ref.table not in lineage_mod.DIMENSION_TABLES:
            raise _reject(
                "dimensions", "dimension_not_allowed",
                f"维度 '{name}' 指向的不是可连接的维度表（{ref.table}）",
            )
        dimensions.append(ResolvedDimension(name=name, table=ref.table, column=ref.column))

    filters: list[ParsedFilter] = []
    for name, raw in req.filters.items():
        for contract in metrics:
            if not contract.allow_filter(name):
                raise _reject(
                    "dimensions", "filter_not_allowed",
                    f"过滤器 '{name}' 不在指标 '{contract.metric_id}' 的 allowed_filters "
                    f"{list(contract.allowed_filters)} 中",
                )
        try:
            kind, ref = resolve_filter(name)
        except UnresolvedName as exc:
            raise _reject("dimensions", "filter_not_allowed", f"过滤器 '{name}' 无法物化：{exc}") from exc
        if ref.table not in lineage_mod.DIMENSION_TABLES:
            raise _reject(
                "dimensions", "filter_not_allowed",
                f"过滤器 '{name}' 指向的不是可连接的维度表（{ref.table}）",
            )
        values = _parse_filter_values(name, kind, ref, raw)
        filters.append(ParsedFilter(name=name, kind=kind, table=ref.table, column=ref.column, values=values))

    order_by: list[tuple[str, bool]] = []
    known_fields = {d.name for d in dimensions} | {m.metric_id for m in metrics} | set(BUCKET_FIELDS)
    for item in req.order_by:
        name = item["field"]
        if name not in known_fields:
            raise _reject(
                "dimensions", "dimension_not_allowed",
                f"order_by 字段 '{name}' 不在可用输出列 "
                f"{sorted({d.name for d in dimensions} | {m.metric_id for m in metrics} | set(BUCKET_FIELDS))} 中",
            )
        order_by.append((name, item.get("dir", "asc").lower() == "desc"))

    return DimensionsPlan(
        dimensions=tuple(dimensions), filters=tuple(filters), order_by=tuple(order_by)
    )


_WEEKDAYS = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday", "thursday": "Thursday",
    "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday",
}


def _filter_bad(name: str, message: str) -> config.EnforcementError:
    return _reject("dimensions", "filter_not_allowed", f"过滤器 '{name}' {message}")


def _parse_filter_values(name: str, kind: str, ref: ColumnRef, raw: Any) -> tuple[Any, ...]:
    """按过滤器形态解析并校验取值（非法值走 `filter_not_allowed:` 前缀）。"""
    if kind == "between":
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise _filter_bad(name, "的 date.between 取值必须是 [起始日, 结束日] 两个 ISO 日期")
        start, end = (_iso_date(name, item) for item in raw)
        if start > end:
            raise _filter_bad(name, f"的 date.between 区间颠倒：{start} > {end}")
        return (start.isoformat(), end.isoformat())
    if kind == "last_n_days":
        days = _positive_int(name, raw, maximum=3650)
        return (days,)

    items: list[Any] = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    if not items:
        raise _filter_bad(name, "的取值不能是空列表")
    return tuple(_coerce_equals(name, ref, item) for item in items)


def _iso_date(name: str, value: Any) -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    try:
        return _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise _filter_bad(name, f"的日期 '{text}' 不是 ISO 格式（YYYY-MM-DD）") from exc


def _positive_int(name: str, value: Any, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise _filter_bad(name, "的取值不能是布尔值")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise _filter_bad(name, f"的取值 '{value}' 不是整数") from exc
    if number < 1 or number > maximum:
        raise _filter_bad(name, f"的取值 {number} 越界（1..{maximum}）")
    return number


def _coerce_equals(name: str, ref: ColumnRef, value: Any) -> Any:
    """把等值过滤器的取值强制成目标列的类型，避免拿字符串去比整数。"""
    if ref.column in ("is_travel", "is_illness", "is_weekend", "is_holiday"):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "y", "t"):
            return True
        if text in ("false", "0", "no", "n", "f"):
            return False
        raise _filter_bad(name, f"的取值 '{value}' 不是布尔值")
    if ref.column in ("weekday_name",):
        text = str(value).strip()
        lowered = text.lower()
        if lowered in _WEEKDAYS:
            return _WEEKDAYS[lowered]
        raise _filter_bad(name, f"的取值 '{value}' 不是星期名（Monday..Sunday）")
    if ref.column in ("location_type", "timezone", "name"):
        if not isinstance(value, str):
            raise _filter_bad(name, f"的取值 '{value}' 必须是字符串")
        return value
    if ref.column == "month":
        return _month_of(name, value)
    if ref.column in ("year", "quarter", "week", "day_of_week", "date_key"):
        return _positive_int(name, value, maximum=10_000_000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError as exc:
            raise _filter_bad(name, f"的取值 '{value}' 必须是数值") from exc


def _month_of(name: str, value: Any) -> int:
    text = str(value).strip()
    if len(text) >= 7 and text[4] == "-":  # 'YYYY-MM'
        text = text[5:7]
    return _positive_int(name, text, maximum=12)


# ---------------------------------------------------------------- 3. grain
def check_grain(req: QueryRequest, metrics: Sequence[MetricContract]) -> str:
    """校验 3：请求粒度不得细于指标声明粒度；未指定时取最粗的声明粒度。"""
    if req.grain is None:
        return max((m.grain for m in metrics), key=lambda g: config.GRAIN_ORDER.get(g, -1))
    if req.grain not in config.GRAIN_ORDER:
        raise _reject(
            "grain", "grain",
            f"未知粒度 '{req.grain}'（合法值：{list(config.GRAINS)}；day 已是最细粒度，"
            f"分钟/小时级请求一律拒绝）",
        )
    requested = config.GRAIN_ORDER[req.grain]
    for contract in metrics:
        declared = config.GRAIN_ORDER[contract.grain]
        if requested < declared:
            raise _reject(
                "grain", "grain",
                f"请求粒度 '{req.grain}' 细于指标 '{contract.metric_id}' 的声明粒度 '{contract.grain}'"
                f"（{contract.grain} 是物化粒度，无法下钻）",
            )
    return req.grain


# ---------------------------------------------------------------- 4. ast_join_path
def _sqlglot_func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    try:
        return str(node.sql_name()).upper()
    except Exception:  # noqa: BLE001 - 未知节点类型时退化为类名
        return type(node).__name__.upper()


def _table_of(node: Any) -> str | None:
    """取 `FROM`/`JOIN` 目标的物理表名；非物理表（表值函数、字面量表）返回 None。"""
    if not isinstance(node, exp.Table) or not isinstance(node.this, exp.Identifier):
        return None
    return node.name


def _table_alias(node: exp.Table) -> str:
    """取 `FROM`/`JOIN` 目标在 SQL 里使用的限定名（有别名用别名，否则用表名）。"""
    return str(node.alias_or_name)


def _from_clause(tree: exp.Select) -> exp.From | None:
    return tree.args.get("from") or tree.args.get("from_")


def check_ast_join_path(
    sql: str | exp.Expression,
    *,
    allowed_tables: Iterable[str] | None = None,
    contracts: Mapping[str, MetricContract] | None = None,
    metric_ids: Iterable[str] | None = None,
    dialect: str = "duckdb",
) -> exp.Expression:
    """校验 4：用 sqlglot 真解析 SQL，断言结构与表/连接路径合法。

    拒绝一律抛 `EnforcementError("ast_join_path", "ast_violation: ...")`。
    返回解析后的 AST（调用方可复用，避免二次解析）。
    """
    def bad(message: str) -> config.EnforcementError:
        return _reject("ast_join_path", "ast", message)

    allowed = (
        frozenset(str(t) for t in allowed_tables)
        if allowed_tables is not None
        else lineage_mod.allowed_tables_for(contracts, metric_ids)
    )

    if isinstance(sql, exp.Expression):
        statements = [sql]
    else:
        try:
            statements = [s for s in sqlglot.parse(str(sql), dialect=dialect) if s is not None]
        except sqlglot.errors.ParseError as exc:
            raise bad(f"SQL 无法被 sqlglot 解析：{exc}") from exc
    if len(statements) != 1:
        raise bad(f"只允许单条 SELECT 语句，实际解析出 {len(statements)} 条（禁止分号拼接多语句）")
    tree = statements[0]
    if isinstance(tree, exp.SetOperation):
        raise bad("禁止 UNION / INTERSECT / EXCEPT 逃逸")
    if not isinstance(tree, exp.Select):
        raise bad(f"根语句必须是 SELECT，实际是 {type(tree).__name__}")

    if tree.args.get("with_") is not None or tree.find(exp.With) or tree.find(exp.CTE):
        raise bad("禁止 CTE（WITH ...）逃逸")
    if tree.find(exp.SetOperation):
        raise bad("禁止 UNION / INTERSECT / EXCEPT 逃逸")
    if tree.find(exp.Subquery):
        raise bad("禁止子查询逃逸")
    if len(list(tree.find_all(exp.Select))) > 1:
        raise bad("禁止子查询逃逸（检测到嵌套 SELECT）")

    for item in tree.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            raise bad("禁止 SELECT *（必须显式列出列）")
    for star in tree.find_all(exp.Star):
        ancestor = star.parent
        inside_count = False
        while ancestor is not None and not isinstance(ancestor, (exp.Select, exp.Column)):
            if isinstance(ancestor, exp.Count):
                inside_count = True
                break
            ancestor = ancestor.parent
        if not inside_count:
            raise bad("禁止 SELECT *（仅允许 COUNT(*)）")

    for table in tree.find_all(exp.Table):
        name = _table_of(table)
        if name is None:
            raise bad(f"禁止表值函数 / 字面量表：{table.sql(dialect=dialect)}")
        if name not in allowed:
            raise bad(f"未声明表 '{name}'（白名单：{sorted(allowed)}）")

    for func in tree.find_all(exp.Func):
        if isinstance(func, _OPERATOR_TYPES):
            # 比较/布尔/一元运算符在 sqlglot 里也是 Func 子类，但它们不是"函数调用"面
            continue
        name = _sqlglot_func_name(func)
        if name not in ALLOWED_FUNCTIONS:
            raise bad(f"未声明的函数 '{name}'（白名单外函数一律拒绝）")

    from_clause = _from_clause(tree)
    if from_clause is None:
        raise bad("缺少 FROM 子句")
    first = _table_of(from_clause.this)
    if first is None:
        raise bad(f"FROM 必须是物理表：{from_clause.this.sql(dialect=dialect)}")
    if first not in allowed:
        raise bad(f"未声明表 '{first}'（白名单：{sorted(allowed)}）")
    # ON 条件里用的是**别名**（如 f/d/s），声明路径注册表用的是物理表名，这里做映射
    first_alias = _table_alias(from_clause.this)
    alias_to_table: dict[str, str] = {first_alias: first}
    visible: list[str] = [first_alias]

    for join in tree.args.get("joins") or []:
        kind = str(join.args.get("kind") or "").upper()
        if kind == "CROSS":
            raise bad("禁止 CROSS JOIN")
        on_clause = join.args.get("on")
        if on_clause is None:
            raise bad("JOIN 必须带 ON 等值条件（禁止 USING / 自然连接）")
        target = _table_of(join.this)
        if target is None:
            raise bad(f"JOIN 目标必须是物理表：{join.this.sql(dialect=dialect)}")
        if target not in allowed:
            raise bad(f"未声明表 '{target}'（白名单：{sorted(allowed)}）")
        target_alias = _table_alias(join.this)
        alias_to_table[target_alias] = target
        matched = False
        for eq in on_clause.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            left_alias, right_alias = left.table, right.table
            if not left_alias or not right_alias or left_alias == right_alias:
                continue
            if target_alias not in (left_alias, right_alias):
                continue
            other = right_alias if left_alias == target_alias else left_alias
            if other not in visible:
                raise bad(f"JOIN '{target}' 与本轮不可见的表 '{other}' 连接")
            left_physical = alias_to_table.get(left_alias)
            right_physical = alias_to_table.get(right_alias)
            if left_physical is None or right_physical is None:
                raise bad(f"JOIN '{target}' 的 ON 条件引用了未声明的表限定名 '{other}'")
            keys = lineage_mod.declared_join_keys(left_physical, right_physical)
            if not keys:
                raise bad(f"JOIN 未走已声明路径：{left_physical} ↔ {right_physical}")
            if not ({left.name, right.name} & set(keys)):
                raise bad(
                    f"JOIN 键列未声明：{left_physical}.{left.name} ↔ {right_physical}.{right.name}"
                    f"（已声明键 {list(keys)}）"
                )
            matched = True
        if not matched:
            raise bad(f"JOIN '{target}' 未与已声明表建立等值连接（禁止隐式/笛卡尔连接）")
        visible.append(target_alias)

    return tree


#: 别名，便于外部按"校验名"调用
validate_ast = check_ast_join_path


# ---------------------------------------------------------------- 5. rls
@dataclass(frozen=True)
class RlsPlan:
    """第 5 条校验的产物：RLS 是**改写**而非拒绝。"""

    policies: tuple[str, ...]
    subject_scope: bool
    cross_subject: bool
    min_group_size: int | None
    pii_blocked: tuple[str, ...]

    @property
    def rls_applied(self) -> list[str]:
        """审计头 `rls_applied`。"""
        return list(self.policies)


def check_rls(
    metrics: Sequence[MetricContract],
    role: Any,
    plan: DimensionsPlan | None = None,
) -> RlsPlan:
    """校验 5：角色无权 → 拒绝；有权 → 返回改写计划（行级过滤/HAVING/PII 列禁止）。"""
    resolved_role = normalize_role(role)
    policies: list[str] = []
    for contract in metrics:
        policy = contract.rls_policy
        visible = config.rls_visible_roles(policy)
        if resolved_role not in visible:
            raise _reject_rls(
                f"角色 '{resolved_role.value}' 无权访问指标 '{contract.metric_id}'"
                f"（rls_policy='{policy}'，可见角色 {sorted(r.value for r in visible)}）"
            )
        if policy not in policies:
            policies.append(policy)
    policies = [p for p in _POLICY_ORDER if p in policies]

    pii_blocked: set[str] = set()
    if any(policy == "no_pii" for policy in policies) or resolved_role in (
        config.Role.PARTNER,
        config.Role.RESEARCHER,
    ):
        pii_blocked |= set(config.PII_COLUMNS)
    if plan is not None and pii_blocked:
        for ref in plan.touched_columns:
            if ref.column in pii_blocked:
                raise _reject_rls(
                    f"角色 '{resolved_role.value}' / 策略 {policies} 禁止选中 PII 列 "
                    f"'{ref.column}'（config.PII_COLUMNS）"
                )

    owner = resolved_role is config.Role.OWNER
    cross_subject = not owner
    min_group_size = (
        config.MIN_GROUP_SIZE
        if (cross_subject and any(policy == "aggregate_min5" for policy in policies))
        else None
    )
    return RlsPlan(
        policies=tuple(policies),
        subject_scope=owner,
        cross_subject=cross_subject,
        min_group_size=min_group_size,
        pii_blocked=tuple(sorted(pii_blocked)),
    )

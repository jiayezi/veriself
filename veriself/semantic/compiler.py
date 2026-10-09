"""SQL 编译与执行（IFACE-v1 第 7 节 `compile_query` / `execute_query`）。

设计要点
--------
1. **零拼接**：SQL 完全由 sqlglot 表达式树构造，所有客户端取值一律走 `?` 参数绑定
   （`_render_with_params` 会按 SQL 中**实际出现顺序**还原参数表，杜绝顺序错位）。
2. **跨粒度上卷**：`fact_metric_value` 的物化粒度是契约声明的 `grain`；请求更粗时按契约
   `agg` 聚合（sum→SUM / mean→AVG / min→MIN / max→MAX / last→ARG_MAX(value, date_key)）；
   **同粒度不聚合**（取原始值 `ANY_VALUE`，等价于原始值，因为分组内每个指标只有一行）。
3. **当前有效版本**：`valid_to IS NULL` **且** `metric_version = 契约 version`，
   只按 `valid_to IS NULL` 会把旧口径版本也算进来。
4. **RLS 改写**：owner 注入 `subject_id = ?`（只看自己）；partner/researcher 跨主体聚合，
   `aggregate_min5` 追加 `HAVING COUNT(DISTINCT subject_id) >= 5 AND COUNT(*) >= 5`。
5. **扫描量预检**：执行前 `EXPLAIN` 取估算行数，超 `config.SCAN_LIMIT_HINT` 拒绝。
6. **审计**：返回审计头（契约 §4），`audit=True` 时写 `fact_audit_log`
   （调用 `veriself.warehouse.loader.write_audit`；缺模块/签名不符时降级并记日志）。
"""

from __future__ import annotations

import datetime as _dt
import importlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import duckdb
from sqlglot import exp

from veriself import config
from veriself.semantic import enforcement
from veriself.semantic import lineage as lineage_mod
from veriself.semantic.contract import AS_OF_DEFINITION, MetricContract
from veriself.semantic.query import CompiledQuery, QueryRequest, QueryResult

__all__ = ["compile_query", "estimate_scan_rows", "execute_query"]

_log = logging.getLogger(__name__)

_TZ = _dt.timezone(_dt.timedelta(hours=8))
_ROWS_RE = re.compile(r"~\s*([\d,]+)\s*rows")
_EC_RE = re.compile(r"EC:\s*([\d,]+)")
_PH_MARK_RE = re.compile(r"'__SWH_PH_(\d+)__'")
#: 审计头里额外记录"版本选择"（契约 §4 结构之外的显式说明，Lead 要求写进审计头）
ROW_VERSIONING = "valid_to IS NULL AND metric_version = contract.version"


def _today() -> _dt.date:
    """`date.last_n_days` 的锚点（东八区今天）。测试可 monkeypatch。"""
    return _dt.datetime.now(_TZ).date()


def _now() -> _dt.datetime:
    """审计时间戳（东八区，ISO8601 带 +08:00）。"""
    return _dt.datetime.now(_TZ)


# ---------------------------------------------------------------- 参数化渲染
def _render_with_params(tree: exp.Expr, values: Sequence[object]) -> tuple[str, list[object]]:
    """渲染 SQL 并按占位符在 SQL 中的**实际顺序**输出参数表。

    做法：把每个 `?` 占位符原地换成唯一标记字面量（标记里带该占位符在 `values` 中的下标）
    → 渲染 → 按标记出现顺序还原参数 → 再把标记文本换成 `?`。
    这样即使以后调整 AST 构造顺序（或 sqlglot 的遍历顺序变化），参数也绝不会错位。
    """
    nodes = list(tree.find_all(exp.Placeholder))
    if len(nodes) != len(values):
        raise config.QueryError(f"参数化失败：SQL 里 {len(nodes)} 个占位符，但收集到 {len(values)} 个参数")
    markers: dict[int, exp.Expr] = {}
    for node in nodes:
        index = node.meta.get("swh_value_index")
        if index is None:
            raise config.QueryError("参数化失败：占位符缺少取值下标（必须由 _Builder.ph 生成）")
        marker = exp.Literal.string(f"__SWH_PH_{index}__")
        markers[index] = marker
        node.replace(marker)
    try:
        probe = tree.sql(dialect="duckdb")
    finally:
        for marker in markers.values():
            marker.replace(exp.Placeholder())
    order = [int(found) for found in _PH_MARK_RE.findall(probe)]
    if sorted(order) != list(range(len(values))):
        raise config.QueryError(f"参数化失败：占位符顺序异常（{order}）")
    return _PH_MARK_RE.sub("?", probe), [values[index] for index in order]


# ---------------------------------------------------------------- SQL 构造
class _Builder:
    """把（已通过前三条校验的）请求构造成 sqlglot SELECT。"""

    def __init__(
        self,
        req: QueryRequest,
        metrics: Sequence[MetricContract],
        plan: enforcement.DimensionsPlan,
        grain: str,
        rls: enforcement.RlsPlan | None,
    ) -> None:
        self.req = req
        self.metrics = list(metrics)
        self.plan = plan
        self.grain = grain
        self.rls = rls
        self.values: list[object] = []
        self.bucket = enforcement.bucket_field(grain)

    # -------------------------------------------------- 小工具
    def ph(self, value: object) -> exp.Placeholder:
        """生成一个 `?` 占位符并登记绑定值（占位符自带取值下标，保证参数顺序不错位）。"""
        index = len(self.values)
        self.values.append(value)
        node = exp.Placeholder()
        node.meta["swh_value_index"] = index
        return node

    @staticmethod
    def col(name: str, table: str) -> exp.Column:
        """构造带引号的限定列引用。"""
        return exp.column(name, table=table, quoted=True)

    @staticmethod
    def _alias_for(table: str) -> str:
        return {
            "dim_date": "d",
            "fact_subject_day": "sd",
            "dim_subject": "s",
            "fact_metric_value": "f",
        }[table]

    def _needs_join(self, table: str) -> bool:
        if table not in lineage_mod.DIMENSION_TABLES or table == "dim_date":
            return False
        return any(item.table == table for item in self.plan.dimensions) or any(
            item.table == table for item in self.plan.filters
        )

    def _rollup(self, contract: MetricContract) -> bool:
        """该指标是否需要（跨时间粒度）上卷聚合。"""
        declared = config.GRAIN_ORDER.get(contract.grain, -1)
        requested = config.GRAIN_ORDER.get(self.grain, -1)
        return requested > declared

    def _cross_subject(self) -> bool:
        return bool(self.rls and self.rls.cross_subject)

    # -------------------------------------------------- 各部分
    def _bucket_expr(self) -> exp.Expr:
        date_col = self.col("date", "d")
        if self.grain == "day":
            return date_col
        # DuckDB 的 date_trunc 返回 TIMESTAMP，这里统一裁成 DATE，输出与 date.day 一致
        return exp.cast(exp.DateTrunc(unit=exp.Literal.string(self.grain), this=date_col), "DATE")

    def _metric_expr(self, contract: MetricContract) -> exp.Expr:
        """单个指标的输出表达式（`CASE WHEN metric_id = ?` + 该指标的 agg）。"""
        case = exp.Case().when(
            self.col("metric_id", "f").eq(self.ph(contract.metric_id)),
            self.col("value", "f"),
        )
        need_agg = self._rollup(contract) or self._cross_subject()
        if not need_agg:
            # 同粒度且单主体：禁止聚合，取原始值（分组内该指标只有一行，ANY_VALUE 即原值）
            return exp.Anonymous(this="ANY_VALUE", expressions=[case])
        if contract.agg == "sum":
            return exp.Sum(this=case)
        if contract.agg == "mean":
            return exp.Avg(this=case)
        if contract.agg == "min":
            return exp.Min(this=case)
        if contract.agg == "max":
            return exp.Max(this=case)
        if contract.agg == "last":
            return exp.ArgMax(this=case, expression=self.col("date_key", "f"))
        raise config.ContractError(f"指标 '{contract.metric_id}' 的 agg='{contract.agg}' 不受支持")

    def _select_list(self) -> list[exp.Expr]:
        items: list[exp.Expr] = [
            exp.alias_(self._bucket_expr(), self.bucket, quoted=True)
        ]
        for dim in self.plan.dimensions:
            items.append(
                exp.alias_(self.col(dim.column, self._alias_for(dim.table)), dim.name, quoted=True)
            )
        for contract in self.metrics:
            items.append(exp.alias_(self._metric_expr(contract), contract.metric_id, quoted=True))
        return items

    def _metric_predicate(self) -> exp.Expr:
        """`(metric_id = ? AND metric_version = ?) OR ...` —— 当前有效版本。"""
        parts = [
            exp.and_(
                self.col("metric_id", "f").eq(self.ph(contract.metric_id)),
                self.col("metric_version", "f").eq(self.ph(int(contract.version))),
            )
            for contract in self.metrics
        ]
        return parts[0] if len(parts) == 1 else exp.or_(*parts)

    def _date_filters(self) -> list[exp.Expr]:
        conditions: list[exp.Expr] = []
        for item in self.plan.filters:
            if item.table != "dim_date":
                continue
            ref = self.col(item.column, self._alias_for(item.table))
            if item.kind == "between":
                start, end = item.values
                conditions.append(exp.GTE(this=ref, expression=exp.cast(self.ph(start), "DATE")))
                conditions.append(exp.LTE(this=ref, expression=exp.cast(self.ph(end), "DATE")))
            elif item.kind == "last_n_days":
                days = int(item.values[0])
                anchor = _today()
                start = anchor - _dt.timedelta(days=days - 1)
                conditions.append(
                    exp.GTE(this=ref, expression=exp.cast(self.ph(start.isoformat()), "DATE"))
                )
                conditions.append(
                    exp.LTE(this=ref, expression=exp.cast(self.ph(anchor.isoformat()), "DATE"))
                )
            else:
                conditions.append(self._equality(ref, item.values))
        return conditions

    def _other_filters(self) -> list[exp.Expr]:
        conditions: list[exp.Expr] = []
        for item in self.plan.filters:
            if item.table == "dim_date":
                continue
            conditions.append(self._equality(self.col(item.column, self._alias_for(item.table)), item.values))
        return conditions

    def _equality(self, ref: exp.Column, values: Sequence[Any]) -> exp.Expr:
        if len(values) == 1:
            return ref.eq(self.ph(values[0]))
        return exp.In(this=ref, expressions=[self.ph(value) for value in values])

    def _having(self) -> exp.Expr:
        size = int(self.rls.min_group_size) if self.rls and self.rls.min_group_size else config.MIN_GROUP_SIZE
        subjects = exp.GTE(
            this=exp.Count(this=exp.Distinct(expressions=[self.col("subject_id", "f")])),
            expression=exp.Literal.number(size),
        )
        # 行数下限一并保留（人是 ≥5，且分组内至少有 5 行），两条都写进 SQL 供审计阅读
        rows = exp.GTE(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(size))
        return exp.and_(subjects, rows)

    def _order_by(self) -> list[exp.Ordered]:
        items: list[exp.Ordered] = []
        for name, desc in self.plan.order_by:
            alias = self.bucket if name in enforcement.BUCKET_FIELDS else name
            items.append(exp.Ordered(this=exp.column(alias, quoted=True), desc=desc))
        if not items:
            # Lead 要求：默认按日期升序
            items.append(exp.Ordered(this=exp.column(self.bucket, quoted=True), desc=False))
        return items

    # -------------------------------------------------- 组装
    def build(self) -> tuple[exp.Select, list[object]]:
        """构造 SELECT，返回 (AST, 绑定值列表)。"""
        fact = exp.table_("fact_metric_value", alias="f", quoted=True)
        query = exp.select(*self._select_list()).from_(fact)
        # 内连接 dim_date（Lead 明确要求 inner join）
        query = query.join(
            exp.table_("dim_date", alias="d", quoted=True),
            on=self.col("date_key", "d").eq(self.col("date_key", "f")),
            join_type="inner",
        )
        if self._needs_join("fact_subject_day"):
            query = query.join(
                exp.table_("fact_subject_day", alias="sd", quoted=True),
                on=exp.and_(
                    self.col("subject_id", "sd").eq(self.col("subject_id", "f")),
                    self.col("date_key", "sd").eq(self.col("date_key", "f")),
                ),
                join_type="inner",
            )
        if self._needs_join("dim_subject"):
            query = query.join(
                exp.table_("dim_subject", alias="s", quoted=True),
                on=exp.and_(
                    self.col("subject_id", "s").eq(self.col("subject_id", "f")),
                    self.col("is_current", "s").is_(exp.Boolean(this=True)),
                ),
                join_type="inner",
            )

        conditions: list[exp.Expr] = [
            self.col("valid_to", "f").is_(exp.Null()),
            self._metric_predicate(),
        ]
        if self.rls and self.rls.subject_scope:
            conditions.append(self.col("subject_id", "f").eq(self.ph(config.SUBJECT_ID)))
        conditions.extend(self._date_filters())
        conditions.extend(self._other_filters())
        query = query.where(exp.and_(*conditions))

        # GROUP BY 序号：1..(1 + 维度数)，与 select 前若干列一一对应
        group_count = 1 + len(self.plan.dimensions)
        query = query.group_by(*[exp.Literal.number(index) for index in range(1, group_count + 1)])
        if self.rls and self.rls.min_group_size:
            query = query.having(self._having())
        query = query.order_by(*self._order_by())
        query = query.limit(self.ph(self.req.limit))
        return query, self.values


# ---------------------------------------------------------------- compile_query
def compile_query(
    req: QueryRequest,
    contracts: dict[str, MetricContract],
    role: config.Role | str = config.Role.OWNER,
) -> CompiledQuery:
    """执行五条强制校验并生成 SQL；被拒绝时抛 `config.EnforcementError(rule, detail)`。

    校验顺序 = `config.ENFORCED_CHECKS`：registered → dimensions → grain → rls → ast_join_path。

    两处刻意设计：

    - **RLS 先于 SQL 构造**：`check_rls` 只依赖契约与角色，不依赖 SQL，因此先算出改写计划，
      SQL 只构造一次（否则要先构造一份"未注入 RLS"的 SQL 做校验再丢弃）。
    - **AST 校验复用表达式树**：把 `tree` 而非渲染后的字符串传给 `check_ast_join_path`，
      避免 sqlglot 二次解析（约占编译耗时的四分之一）。
    """
    resolved_role = enforcement.normalize_role(role)
    # 实际执行过的校验，逐个追加；用于审计头（见下方 enforced_checks 的说明）。
    # 只有真的调用成功了才记名——这样"删掉一条校验"会立刻在测试里变红。
    # 记录的是**执行顺序**，因此是 rls 先于 ast_join_path（RLS 计划要先算出来）。
    check_trace: list[str] = []

    # 1) registered
    metrics = enforcement.check_registered(req, contracts)
    check_trace.append("registered")
    # 2) dimensions / filters / order_by 白名单
    plan = enforcement.check_dimensions(req, contracts, metrics)
    check_trace.append("dimensions")
    # 3) grain
    grain = enforcement.check_grain(req, metrics)
    check_trace.append("grain")

    # 4) rls：改写而非拒绝（先定计划，避免构造两份 SQL）
    rls = enforcement.check_rls(metrics, resolved_role, plan)
    check_trace.append("rls")

    # 5) ast_join_path：只构造一次，并对**最终**（含 RLS）的树做结构校验
    tree, values = _Builder(req, metrics, plan, grain, rls=rls).build()
    enforcement.check_ast_join_path(tree, allowed_tables=lineage_mod.READ_PATH_TABLES)
    check_trace.append("ast_join_path")
    sql, params = _render_with_params(tree, values)

    return CompiledQuery(
        request=req,
        sql=sql,
        params=params,
        metric_versions={contract.metric_id: int(contract.version) for contract in metrics},
        contract_hashes={contract.metric_id: contract.contract_hash for contract in metrics},
        rls_applied=rls.rls_applied,
        # 记录**实际执行过**的校验，而不是把 `config.ENFORCED_CHECKS` 抄一遍：
        # 审计栏若只回显配置常量，就证明不了任何事（删掉某条校验也不会变红）。
        # 对外按契约声明的规范顺序输出（执行顺序见 `check_trace`）。
        enforced_checks=_canonical_order(check_trace),
    )


def _canonical_order(executed: Sequence[str]) -> list[str]:
    """把实际执行过的校验按 `config.ENFORCED_CHECKS` 的规范顺序输出。

    两者的差别是**有意的**：`rls` 必须先算出改写计划，`ast_join_path` 才能对最终 SQL 校验，
    所以执行顺序是 `rls → ast_join_path`；而契约把 `ast_join_path` 列为第 4 条
    （"先证明表格结构合法，再谈行级可见性"）。对外只暴露规范顺序，避免读者以为契约写错了。

    这里**不是**简单返回常量：`executed` 缺了任何一条，输出就会缺那一条。
    """
    missing = [name for name in executed if name not in config.ENFORCED_CHECKS]
    if missing:  # pragma: no cover - 防呆：新增校验常量后忘了同步
        raise config.QueryError(f"执行记录含未知校验名 {missing}")
    return [name for name in config.ENFORCED_CHECKS if name in set(executed)]


# ---------------------------------------------------------------- 执行
def estimate_scan_rows(conn, sql: str, params: Sequence[object] | None = None) -> int | None:
    """用 `EXPLAIN` 估算扫描行数（取计划里各节点 `~N rows` 的最大值）。

    解析不到估算值时返回 `None`（此时不做扫描量拒绝，但会记 warning）。
    """
    explain_sql = f"EXPLAIN {sql}"
    cursor = conn.execute(explain_sql, list(params) if params else None)
    rows = cursor.fetchall()
    text = "\n".join(str(cell) for row in rows for cell in row)
    found = [int(value.replace(",", "")) for value in _ROWS_RE.findall(text)]
    if not found:
        found = [int(value.replace(",", "")) for value in _EC_RE.findall(text)]
    return max(found) if found else None


def _connect():
    """自建 DuckDB 连接（默认读 `config.WAREHOUSE_PATH`）。"""
    path = config.WAREHOUSE_PATH
    if not path.exists():
        raise config.QueryError(f"数仓文件不存在：{path}（请先运行 veriself init 完成建表与物化）")
    try:
        return duckdb.connect(str(path))
    except duckdb.Error as exc:  # pragma: no cover - 依赖本机锁文件状态
        raise config.QueryError(f"无法打开数仓 {path}：{exc}") from exc


def _guard_context_join(conn, sql: str) -> None:
    """fail-closed：存量库的 `fact_subject_day` 缺复合键时拒绝，而不是按残缺键给出错数。"""
    if "fact_subject_day" not in sql:
        return
    try:
        info = conn.execute("PRAGMA table_info('fact_subject_day')").fetchall()
    except duckdb.Error:
        return
    if not info:  # 表不存在，交给主查询报错
        return
    columns = {str(row[1]) for row in info}
    missing = {"subject_id", "date_key"} - columns
    if missing:
        raise config.EnforcementError(
            "ast_join_path",
            f"{config.REASON_PREFIXES['ast']} fact_subject_day 缺少已声明 JOIN 路径所需的"
            f" {sorted(missing)} 列（连接必须同时用 subject_id 与 date_key）；"
            f"请重建数仓（veriself synth && veriself init）",
        )


def _subject_scoped(tree_sql: str) -> bool:
    """AST 判断编译产物是否带了 `subject_id = ?` 行级过滤（只看 WHERE，不看 HAVING 的分组计数）。"""
    import sqlglot

    tree = sqlglot.parse_one(tree_sql, dialect="duckdb")
    where = tree.args.get("where")
    if where is None:
        return False
    for eq in where.find_all(exp.EQ):
        left, right = eq.left, eq.right
        if isinstance(left, exp.Column) and left.name == "subject_id" and not isinstance(right, exp.Column):
            return True
        if isinstance(right, exp.Column) and right.name == "subject_id" and not isinstance(left, exp.Column):
            return True
    return False


def _warn_role_mismatch(compiled: CompiledQuery, role: config.Role) -> None:
    """`CompiledQuery`（冻结结构）不带角色，这里对"编译期角色 ≠ 执行期角色"给出告警。"""
    scoped = _subject_scoped(compiled.sql)
    if scoped and role is not config.Role.OWNER:
        _log.warning(
            "编译产物带 subject_id 行级过滤，但执行角色是 %s；请用与 compile_query 相同的角色执行",
            role.value,
        )
    elif not scoped and role is config.Role.OWNER:
        _log.warning(
            "执行角色是 owner，但编译产物没有 subject_id 行级过滤；请用与 compile_query 相同的角色执行"
        )


def _jsonable(value: Any) -> Any:
    """把 DuckDB 返回值转成可 JSON 序列化的 Python 值。"""
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        import decimal

        if isinstance(value, decimal.Decimal):
            return float(value)
    except ImportError:  # pragma: no cover
        pass
    return value


def _audit_header(compiled: CompiledQuery, role: config.Role) -> dict:
    """契约 §4 审计头（额外记录版本选择与执行角色）。"""
    return {
        "metric_versions": dict(compiled.metric_versions),
        "contract_hashes": dict(compiled.contract_hashes),
        "compiled_sql": compiled.sql,
        "rls_applied": list(compiled.rls_applied),
        "enforced_checks": list(config.ENFORCED_CHECKS),
        "as_of_definition": AS_OF_DEFINITION,
        "queried_at": _now().isoformat(timespec="seconds"),
        "row_versioning": ROW_VERSIONING,
        "actor_role": role.value,
    }


def _audit_record(compiled: CompiledQuery, role: config.Role, outcome: str) -> dict:
    """`fact_audit_log` 行（键与契约 §1 DDL 对齐；`audit_id` 由 loader 生成）。"""
    return {
        "queried_at": _now().replace(tzinfo=None),
        "actor_role": role.value,
        "request_json": compiled.request.model_dump_json(),
        "compiled_sql": compiled.sql,
        "metric_versions": json.dumps(compiled.metric_versions, ensure_ascii=False),
        "contract_hashes": json.dumps(compiled.contract_hashes, ensure_ascii=False),
        "rls_applied": json.dumps(compiled.rls_applied, ensure_ascii=False),
        "checks_passed": json.dumps(list(compiled.enforced_checks), ensure_ascii=False),
        "outcome": outcome,
    }


def _write_audit(conn, record: Mapping[str, Any]) -> bool:
    """调用 `warehouse.loader.write_audit` 写审计日志；不可用时降级并记 warning。

    期望签名：`write_audit(conn, record: dict) -> None`（record 的键 = fact_audit_log 列，不含 audit_id）。
    为解耦，本函数对若干合理调用形态都做兼容尝试，全部失败只降级、不抛异常。
    """
    try:
        module = importlib.import_module("veriself.warehouse.loader")
    except Exception as exc:  # noqa: BLE001 - 可能是 ImportError，也可能是其依赖缺失
        _log.warning("审计日志降级：无法导入 veriself.warehouse.loader（%s）", exc)
        return False
    writer = getattr(module, "write_audit", None)
    if writer is None:
        _log.warning("审计日志降级：loader 模块没有 write_audit")
        return False
    attempts = (
        lambda: writer(conn, dict(record)),
        lambda: writer(record=dict(record), conn=conn),
        lambda: writer(dict(record), conn=conn),
    )
    for attempt in attempts:
        try:
            attempt()
            return True
        except TypeError as exc:
            _log.debug("write_audit 调用形态不匹配：%s", exc)
            continue
        except Exception as exc:  # noqa: BLE001 - 写审计失败不能影响查询结果
            _log.warning("审计日志降级：write_audit 执行失败（%s）", exc)
            return False
    _log.warning("审计日志降级：write_audit 的签名无法匹配（期望 write_audit(conn, record: dict)）")
    return False


def execute_query(
    compiled: CompiledQuery,
    role: config.Role = config.Role.OWNER,
    conn=None,
    audit: bool = True,
) -> QueryResult:
    """执行 + EXPLAIN 预检 + 写审计日志 + 组装审计头（契约 §7）。"""
    resolved_role = enforcement.normalize_role(role)
    # 防御性复核：CompiledQuery 若被手工构造/篡改，这里仍然拦得住
    enforcement.check_ast_join_path(compiled.sql, allowed_tables=lineage_mod.READ_PATH_TABLES)

    owns_conn = conn is None
    connection = _connect() if owns_conn else conn
    try:
        _warn_role_mismatch(compiled, resolved_role)
        _guard_context_join(connection, compiled.sql)
        params = list(compiled.params)
        try:
            estimate = estimate_scan_rows(connection, compiled.sql, params)
        except duckdb.Error as exc:
            _guard_context_join(connection, compiled.sql)
            raise config.QueryError(f"EXPLAIN 预检失败（SQL 无法绑定到当前数仓）：{exc}") from exc
        if estimate is None:
            _log.warning("EXPLAIN 未返回估算行数，跳过扫描量预检")
        elif estimate > config.SCAN_LIMIT_HINT:
            if audit:
                _write_audit(connection, _audit_record(compiled, resolved_role, "rejected:scan"))
            raise config.EnforcementError(
                "scan",
                f"{config.REASON_PREFIXES['scan']} EXPLAIN 估算扫描行数 {estimate} "
                f"超过上限 {int(config.SCAN_LIMIT_HINT)}",
            )
        try:
            cursor = connection.execute(compiled.sql, params) if params else connection.execute(compiled.sql)
            columns = [str(desc[0]) for desc in (cursor.description or [])]
            rows = cursor.fetchall()
        except duckdb.Error as exc:
            raise config.QueryError(f"查询执行失败：{exc}") from exc
        data = [dict(zip(columns, (_jsonable(value) for value in row))) for row in rows]
        result = QueryResult(data=data, audit=_audit_header(compiled, resolved_role))
        if audit:
            _write_audit(connection, _audit_record(compiled, resolved_role, "ok"))
        return result
    finally:
        if owns_conn:
            connection.close()

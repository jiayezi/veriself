"""求值 SQL 的构造（materializer 内部）。

把 SQL 骨架与"填空逻辑"分开：模板放本模块顶部，`_compute_ctes` 只负责选哪个 + 填什么。
这样读函数体时能一眼看完求值的**结构与决策**，不必在 100+ 行里区分 SQL 文本与 Python。

模板**左对齐书写**，需要用缩进时交给 `_indent_block()`——在模板里数空格极易出错，
且缩进量会随外层结构变化而失效。

公式解析/重写统一委托 `veriself.sqlrefs`（sqlglot AST）；
列清单/别名来自语义模型（`views._models` / `views.source_aliases`）。
"""

from __future__ import annotations

import logging
import re
import textwrap as _textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from veriself import config, sqlrefs
from veriself.materializer import contracts as _contracts
from veriself.materializer import views as _views

logger = logging.getLogger(__name__)

__all__ = [
    "_compute_ctes",
    "_row_expression",
    "_touches",
]

# ------------------------------------------------------------------ 求值骨架模板
# 三个骨架片段都要求 `(date_key, subject_id)` 唯一——否则后续 JOIN 会产生笛卡尔积。
# `obs_daily` / `evt_daily` 已按该键聚合，`fact_metric_value` 用 DISTINCT 投影，均可安全作骨架。
_SPINE_OBS = "SELECT date_key, subject_id FROM obs_daily"
_SPINE_EVT = "SELECT date_key, subject_id FROM evt_daily"
_SPINE_UPSTREAM = (
    "SELECT DISTINCT date_key, subject_id FROM fact_metric_value"
    " WHERE valid_to IS NULL AND metric_id IN ({upstream_ids})"
)

_SOURCE_CTES = """obs_src AS (
    SELECT o.date_key AS obs_date_key, o.subject_id AS obs_subject_id,
           o.* EXCLUDE (date_key, subject_id)
    FROM obs_daily o
),
evt_src AS (
    SELECT e.date_key AS evt_date_key, e.subject_id AS evt_subject_id,
           e.* EXCLUDE (date_key, subject_id)
    FROM evt_daily e
),
dim_src AS (
    SELECT s.subject_id AS dim_subject_id, {dim_cols}
    FROM subject_current s
)"""

# 通道/事件列清单由语义模型生成（见 `views.ensure_views` / `_compute_ctes`）。
_DAILY_SRC = """daily_src AS (
    SELECT
        d.date_key AS date_key,
        d.subject_id AS subject_id,
        {obs_cols},
        {evt_cols},
        {dim_cols_plain}{upstream_cols}
    FROM spine d
    LEFT JOIN obs_src o
        ON o.obs_date_key = d.date_key AND o.obs_subject_id = d.subject_id
    LEFT JOIN evt_src e
        ON e.evt_date_key = d.date_key AND e.evt_subject_id = d.subject_id
    LEFT JOIN dim_src s
        ON s.dim_subject_id = d.subject_id
    {lateral}
),
daily AS (
    SELECT
        date_key,
        subject_id,
        ({expr}) AS daily_value
    FROM daily_src
),{rollup}"""

# 求值骨架的头部：与 `_SOURCE_CTES` / `_DAILY_SRC` 一样**左对齐**书写，
# 缩进由 `_compute_ctes` 整体施加（在模板里数空格极易出错）。
_EVAL_PREFIX = """WITH spine AS (
    {spine}
),
{source_ctes},
"""

# 上卷方式之一：日粒度、或公式本身已用窗口表达跨时间逻辑 → 原样落库
_ROLLUP_DAILY = """
rolled AS (
    SELECT date_key, subject_id, daily_value
    FROM daily
    WHERE daily_value IS NOT NULL AND isfinite(daily_value)
)"""

# 上卷方式之二：非日粒度 → 每桶保留一行，`date_key` 取**桶内最后一个有数据的真实日期**。
# 无 `bucket` 声明的旧形态：公式手写窗口 + CASE 标记代表行，这里用 arg_max 取代表行的值
# （契约 §8：`date_key` 永远是真实存在的日期，便于与 `dim_date` 内连接）。
_ROLLUP_BUCKETED = """
rolled AS (
    SELECT
        {bucket} AS date_key,
        subject_id,
        arg_max(daily_value, date_key) AS daily_value
    FROM daily
    WHERE daily_value IS NOT NULL AND isfinite(daily_value)
    GROUP BY 1, 2
)"""

# 上卷方式之三：契约声明了 `bucket.agg` → 物化器自动做桶内聚合。
# formula_sql 是日粒度标量表达式；`daily_value` 按桶聚合，`date_key` = max(date_key)
# 仍满足"桶内最后一个有数据的真实日期"。
# `bucket.agg=last` 时聚合表达式为 `arg_max(daily_value, date_key)`（取最后一天的值）。
_ROLLUP_BUCKET_DECLARATIVE = """
rolled AS (
    SELECT
        max(date_key) AS date_key,
        subject_id,
        {agg_expr} AS daily_value
    FROM daily
    WHERE daily_value IS NOT NULL AND isfinite(daily_value)
    GROUP BY {bucket}, subject_id
)"""


#: UNION 后的骨架续行缩进（与 `{spine}` 在模板里的位置对齐 + 4）
_SPINE_CONT = " " * 12

#: `source_ctes` 块的整体缩进
_SOURCE_INDENT = 8


def _spine_ctes(spines: Sequence[str]) -> str:
    """把多个骨架 UNION 起来（各自已保证 `(date_key, subject_id)` 唯一）。"""
    return f"\n{_SPINE_CONT}UNION\n{_SPINE_CONT}".join(spines)


def _indent_block(text: str, spaces: int) -> str:
    """给多行文本的**每一行**加缩进（空行保持为空）。

    缩进只在这一处施加：模板与各片段都左对齐书写，避免 f-string 内联时
    "首行已被外层缩进、又被整体缩进一次"的重复。
    """
    return _textwrap.indent(text, " " * spaces, lambda line: bool(line.strip()))


def _eval_prefix(spines: Sequence[str], source_ctes: str) -> str:
    """`WITH spine AS (...), <source_ctes>,` —— 缩进只在这里施加一次。"""
    return _EVAL_PREFIX.format(
        spine=_spine_ctes(spines),
        source_ctes=_indent_block(source_ctes, _SOURCE_INDENT),
    )


# ------------------------------------------------------------------ 公式改写
def _alias(metric_id: str) -> str:
    """把 metric_id 转成合法且唯一的 SQL 别名。"""
    return re.sub(r"[^0-9a-zA-Z]+", "_", metric_id).strip("_")


def _row_expression(
    formula_sql: str,
    table_aliases: Mapping[str, str] | None = None,
) -> str:
    """契约公式 → 可在 `daily_src` 之上求值的裸列表达式。

    契约允许两种写法（契约 §2），两种都要认，所以先归一到别名形式再去前缀：
      1. 物理表名 → 查询别名：`fact_observation.x` → `o.x`（本来就写 `o.x` 的原样保留）；
      2. `metric('X')` → `up_<alias>`：上游值以裸列形式由 `daily_src` 的
         `up_<alias>.value AS up_<alias>` 透出（见 merge.build_insert_sql 的 upstream_cols）；
      3. 去掉别名前缀：`o.x` → `x`——外层作用域里没有表别名，留着会 Binder Error。

    解析与改写由 `veriself.sqlrefs`（sqlglot AST）统一实现，本模块不再持有
    正则：注释/字符串字面量不会被误判成引用。
    别名映射来自语义模型（`views.source_aliases`），`table_aliases=None` 时自动取默认集。

    `obs_daily` 之类的物化视图名**不是**契约词汇：写它不会被识别（见 `_touches`），
    以免出现"骨架按它建、重写不认它"的半支持状态。
    """
    return sqlrefs.rewrite(
        formula_sql,
        table_aliases=table_aliases if table_aliases is not None else _views.source_aliases(),
        metric_alias=lambda metric_id: f"up_{_alias(metric_id)}",
    )


def _touches(
    formula_sql: str,
    table: str,
    table_aliases: Mapping[str, str] | None = None,
) -> bool:
    """判断公式是否引用了某张来源表。

    只认契约允许的两种写法（物理表名 / `o.` / `e.` / `s.` 别名，见语义模型的 alias）——
    与 `_row_expression` 共用 `sqlrefs` 的同一棵 AST，两者对"合法写法"不可能有分歧。
    只认**真实列引用**：注释/字符串字面量里提到表名不算。
    参数应为**重写之前**的原始契约公式。
    """
    return sqlrefs.touches(
        formula_sql,
        table,
        table_aliases if table_aliases is not None else _views.source_aliases(),
    )


def _lateral_joins(contracts_in_formula: Sequence[str], contracts: Mapping[str, Any]) -> str:
    """为公式中每个 metric('X') 生成一个 LATERAL 关联子查询。

    用显式 LATERAL 而不是隐式相关标量子查询：关联条件写在 JOIN 的 ON 子句里，
    不依赖数据库对相关子查询作用域的推断，跨引擎/升级时更不容易静默失效。
    """
    joins: list[str] = []
    for upstream_id in dict.fromkeys(contracts_in_formula):  # 去重且保序
        version = int(_contracts._field(contracts[upstream_id], "version", 1) or 1)
        alias = _alias(upstream_id)
        joins.append(
            "LEFT JOIN LATERAL (\n"
            "            SELECT mv.value AS value FROM fact_metric_value mv\n"
            f"            WHERE mv.metric_id = '{upstream_id}' AND mv.metric_version = {version}\n"
            "              AND mv.valid_to IS NULL AND mv.date_key = d.date_key\n"
            "            LIMIT 1\n"
            f"        ) up_{alias} ON TRUE"
        )
    return "\n        ".join(joins)


def _bucket_date_expr(grain: str) -> str:
    """把日粒度 date_key（yyyymmdd 整数）压到周/月桶，取该桶最小日期作为代表。"""
    as_date = "strptime(CAST(date_key AS VARCHAR), '%Y%m%d')"
    if grain == "week":
        return f"CAST(strftime(date_trunc('week', {as_date}), '%Y%m%d') AS INTEGER)"
    if grain == "month":
        return f"CAST(strftime(date_trunc('month', {as_date}), '%Y%m%d') AS INTEGER)"
    if grain == "quarter":
        return f"CAST(strftime(date_trunc('quarter', {as_date}), '%Y%m%d') AS INTEGER)"
    raise config.ContractError(f"非法 grain: {grain}")


# ------------------------------------------------------------------ 求值 CTE 组装
def _compute_ctes(
    contract: Any,
    contracts: Mapping[str, Any],
    models: Mapping[str, Any] | None = None,
) -> str:
    """生成求值用的完整 `WITH ... SELECT` 片段（供 `merge.build_insert_sql` /
    `merge.build_merge_sql` 共用）。

    ⚠️ **返回值以具名 CTE `rolled` 结尾**（形如 `..., rolled AS (...)`，**不带**结尾 SELECT），
    调用方自己接 `SELECT ... FROM rolled`。这是本模块内部的隐式契约，改签名要同时改
    `merge` 的两个调用方。

    列清单（obs/evt/dim）由语义模型生成；`models=None` 时取 `semantic_models/` 默认集。

    `date_key` 的取值口径：
      - grain=day：直接用日粒度主干日期；
      - grain=week/month：先按周/月桶取代表行，`date_key` 保持为"真实存在的日期"，
        便于与 `dim_date` 内连接。
    """
    metric_id = _contracts._metric_id(contract)
    formula = str(_contracts._field(contract, "formula_sql", "") or "").strip()
    if not formula:
        raise config.ContractError(f"{metric_id} 缺少 formula_sql")

    resolved = _views._models(models)
    obs = resolved.get("fact_observation")
    evt = resolved.get("fact_event")
    sub = resolved.get("dim_subject")
    missing = [
        table for table in ("fact_observation", "fact_event", "dim_subject") if table not in resolved
    ]
    if missing:
        raise config.ContractError(f"语义模型缺少必需来源表: {missing}")
    aliases = _views.source_aliases(resolved)

    grain = _contracts._grain(contract)
    upstream_ids = _contracts._upstreams(contract)
    refs = tuple(dict.fromkeys(sqlrefs.metric_calls(formula)))
    for upstream_id in refs:
        if upstream_id not in contracts:
            raise config.ContractError(f"formula_sql 引用了未注册的上游指标: {upstream_id}")
        upstream_grain = _contracts._grain(contracts[upstream_id])
        # 上游比当前更粗才是真问题（例如日指标依赖周指标 → 日粒度值会被周值填充）。
        # 上游更细是正常且常见的设计（周指标按周桶聚合日指标），不告警。
        if config.GRAIN_ORDER[upstream_grain] > config.GRAIN_ORDER[grain]:
            logger.warning(
                "上游指标 %s(grain=%s) 比当前指标 %s(grain=%s) 更粗，日粒度值可能被粗粒度值填充",
                upstream_id,
                upstream_grain,
                metric_id,
                grain,
            )
    expr = _row_expression(formula, aliases)
    lateral = _lateral_joins(refs, contracts)
    # 上游指标值以裸列 `up_<metric_id>` 形式透出到公式作用域
    upstream_cols = "".join(f",\n        up_{_alias(u)}.value AS up_{_alias(u)}" for u in refs)

    # 骨架：公式引用了哪些来源，就 UNION 哪些来源的 (date_key, subject_id)
    spines: list[str] = []
    if _touches(formula, "fact_observation", aliases):
        spines.append(_SPINE_OBS)
    if _touches(formula, "fact_event", aliases):
        spines.append(_SPINE_EVT)
    if upstream_ids:
        quoted = ", ".join(f"'{u}'" for u in upstream_ids) or "''"
        spines.append(_SPINE_UPSTREAM.format(upstream_ids=quoted))
    if not spines:
        raise config.ContractError(
            f"{metric_id} 的 formula_sql 未引用任何已知来源，无法确定日期骨架"
        )

    # 上卷分支（优先级从高到低）：
    #   1. `bucket.agg` 声明 → 物化器自动桶内聚合（公式是日粒度标量表达式）；
    #   2. 公式含 OVER → 原样落库（滚动窗口等跨时间逻辑已在公式内表达，不得二次聚合）；
    #   3. 其余非日粒度 → 按桶 arg_max 取代表行（旧形态）。
    bucket_agg = _contracts._bucket_agg(contract)
    uses_window = sqlrefs.uses_window(formula)
    if bucket_agg:
        agg_expr = (
            "arg_max(daily_value, date_key)"
            if bucket_agg == "last"
            else f"{bucket_agg}(daily_value)"
        )
        rollup = _ROLLUP_BUCKET_DECLARATIVE.format(
            bucket=_bucket_date_expr(grain),
            agg_expr=agg_expr,
        )
    else:
        rollup = (
            _ROLLUP_DAILY
            if grain == "day" or uses_window
            else _ROLLUP_BUCKETED.format(bucket=_bucket_date_expr(grain))
        )

    obs_cols = ",\n        ".join(f"{obs.alias}.{c.name}" for c in obs.columns)
    evt_cols = ",\n        ".join(f"{evt.alias}.{c.name}" for c in evt.columns)
    dim_cols = _indent_block(",\n".join(f"{sub.alias}.{c.name}" for c in sub.columns), 8)
    dim_cols_plain = _indent_block(",\n".join(f"{sub.alias}.{c.name}" for c in sub.columns), 8)

    return _eval_prefix(spines, _SOURCE_CTES.format(dim_cols=dim_cols)) + _indent_block(
        _DAILY_SRC.format(
            obs_cols=obs_cols,
            evt_cols=evt_cols,
            dim_cols_plain=dim_cols_plain,
            upstream_cols=upstream_cols,
            lateral=lateral,
            expr=expr,
            rollup=rollup,
        ),
        8,
    )

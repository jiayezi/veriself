"""指标物化器：把契约里的 `formula_sql` 算成 `fact_metric_value` 的行。

设计要点（对应 `docs/00-接口契约.md` 第 8 节）：

1. **分层求值**：先算只依赖原始观测/事件的指标（第 0 层），再算依赖上游指标的派生指标。
   同层按 metric_id 排序，保证结果确定。
2. **统一日粒度视图**：`fact_observation` 是"通道-值"窄表（EAV），而契约的 `formula_sql`
   是按列写的表达式。逻辑列定义在 `semantic_models/*.yml`（`veriself.semantic_model`），
   本模块据此建立 `obs_daily` / `evt_daily` / `subject_asof` 三个视图把通道转成列，
   使契约表达式可直接求值——契约因此不必感知物理存储形态。
3. **grain 语义**：
   - metric 的 `grain` 即 `formula_sql` 求值所在的行粒度（day 或 week/month）。
   - `formula_sql` 用窗口函数表达跨时间逻辑（例如 7 日滚动和用
     `... OVER (ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)`），
     **物化时按行原样落库，不做额外聚合**——这是最容易搞错的地方：
     若在窗口结果上再套一层 `mean`，7 日滚动和会被再平均一次，语义就错了。
   - 因此物化阶段只做"求值 + 落库"，**跨粒度上卷由 semantic 的编译器按 `agg` 负责**。
4. **禁用 NaN/Inf**：无数据的日子不写行（而不是写 0），避免污染后续统计。
5. **不自己实现哈希**：哈希来自 `veriself.contract_hash`。

内部结构：`contracts`（契约字段访问原语）→ `views`（语义模型 → 视图）→
`topo`（拓扑分层）/ `sqlbuild`（求值 SQL 构造）→ `merge`（双时间轴合并）。
本文件只保留对外入口 `materialize_all` 与 re-export。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from veriself import config
from veriself.materializer import contracts as _contracts
from veriself.materializer import merge as _merge
from veriself.materializer import sqlbuild as _sqlbuild
from veriself.materializer import topo as _topo
from veriself.materializer import views as _views

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ re-export
# 冻结 API（docs/00 §8）：全部符号经 `materializer.<name>` 可达。
VALUE_REL_TOL = _merge.VALUE_REL_TOL
VALUE_ABS_FLOOR = _merge.VALUE_ABS_FLOOR
_values_equal = _merge._values_equal
build_merge_sql = _merge.build_merge_sql
build_insert_sql = _merge.build_insert_sql
_hash_for = _merge._hash_for
_model_to_mapping = _merge._model_to_mapping
_batch_timestamp = _merge._batch_timestamp

_row_expression = _sqlbuild._row_expression
_touches = _sqlbuild._touches
_compute_ctes = _sqlbuild._compute_ctes
_alias = _sqlbuild._alias
_lateral_joins = _sqlbuild._lateral_joins
_bucket_date_expr = _sqlbuild._bucket_date_expr

_topo_layers = _topo._topo_layers

ensure_views = _views.ensure_views
source_aliases = _views.source_aliases
_channel_agg_sql = _views._channel_agg_sql
_models = _views._models

_field = _contracts._field
_metric_id = _contracts._metric_id
_upstreams = _contracts._upstreams
_grain = _contracts._grain

__all__ = [
    "VALUE_ABS_FLOOR",
    "VALUE_REL_TOL",
    "build_insert_sql",
    "build_merge_sql",
    "ensure_views",
    "materialize_all",
    "source_aliases",
]


def _affected_count(rows: Any) -> int:
    """读取 DuckDB DDL/DML 结果里的单行 Count。空结果按 0。"""
    if not rows:
        return 0
    return int(rows[0][0])


# ------------------------------------------------------------------ 对外入口
def materialize_all(
    conn: Any,
    contracts: Mapping[str, Any],
    *,
    batch_ts: Any = None,
    models: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, int]]:
    """按拓扑层计算全部指标，并以**合并**方式写入 `fact_metric_value`。

    **幂等**：值与口径都没变时不写任何行（`computed_at` 保持首次写入时间）；
    只有值/口径真正变化、或某些键不再产出时，历史行才会增加。

    返回 `{metric_id: {"live": 当前有效行数, "inserted": 新增, "closed_changed": 值变化关闭,
    "closed_vanished": 键消失关闭, "unchanged": 跳过}}`。

    `batch_ts` 可显式传入批次时间戳（测试用）；默认取当前 UTC 时间。
    `models` 为语义模型集（`semantic_models/*.yml`）；None 时自动加载默认集。
    """
    if not contracts:
        raise config.ContractError("契约集合为空，无法物化")

    resolved = _views._models(models)
    stamp = _merge._batch_timestamp(batch_ts)
    _views.ensure_views(conn, resolved)
    layers = _topo._topo_layers(contracts)
    stats: dict[str, dict[str, int]] = {}

    for depth, layer in enumerate(layers):
        for metric_id in layer:
            contract = contracts[metric_id]

            statements, temp = _merge.build_merge_sql(
                contract, contracts, batch_ts=stamp, models=resolved
            )
            # 四条语句的 DuckDB Count 与 _MERGE_COUNT_KEYS 对齐。
            # 不在合并前后把有效键 / 历史行拉回 Python：那些数只为填统计，
            # 而本批次产出行数和三步 DML 的影响行数引擎已经返回。
            counts: list[int] = []
            try:
                for sql_stmt, sql_params in statements:
                    counts.append(_affected_count(_views._exec(conn, sql_stmt, sql_params)))
            finally:
                _views._exec(conn, f"DROP TABLE IF EXISTS {temp}")

            keys = _merge._MERGE_COUNT_KEYS
            if len(counts) != len(keys):
                raise RuntimeError(
                    f"合并语句应返回 {len(keys)} 个 Count（{', '.join(keys)}），实际 {len(counts)}"
                )
            live, closed_changed, inserted, closed_vanished = counts
            # unchanged = live - inserted：本批次每个键最终都有一行有效，
            # 插入只覆盖「变化后重插」和「全新键」。前提是同一
            # (metric, version, subject, date_key) 最多一行有效。
            # 对账断言：tests/test_materializer.py::test_value_change_creates_history
            unchanged = live - inserted

            stats[metric_id] = {
                "live": live,
                "inserted": inserted,
                "closed_changed": closed_changed,
                "closed_vanished": closed_vanished,
                "unchanged": unchanged,
            }
            logger.info(
                "物化完成 layer=%d metric=%s live=%d inserted=%d changed=%d vanished=%d unchanged=%d",
                depth,
                metric_id,
                live,
                inserted,
                closed_changed,
                closed_vanished,
                unchanged,
            )

    return stats

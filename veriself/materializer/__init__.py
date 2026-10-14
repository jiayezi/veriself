"""指标物化器：把每个指标的 SELECT 算成 `fact_metric_value` 的行。

设计要点（对应 `docs/00-接口契约.md` 第 8 节）：

1. **分层求值**：按 `depends_on` 先算上游，再算派生指标。同层按 metric_id 排序。
2. **来源视图**：`fact_observation` / `fact_event` 是窄表。逻辑列在 `semantic_models/*.yml`，
   本模块据此建立 `obs_daily` / `evt_daily` / `subject_asof`。指标 SQL 只读这些视图
   或 `fact_metric_value`，引擎不改写 SQL。
3. **落库**：指标 SQL 输出 `(subject_id, date_key, value)`。引擎丢掉非有限值，
   拒绝重复粒度键，再按双时间轴合并。查询期上卷仍由 semantic 按 `agg` 负责。
4. **不自己实现哈希**：哈希来自 `veriself.contract_hash`。

内部结构：`contracts`（契约字段）→ `views`（语义模型 → 视图）→
`topo`（拓扑分层）→ `merge`（执行指标 SQL + 双时间轴合并）。
本文件只保留对外入口 `materialize_all` 与 re-export。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from veriself import config
from veriself.materializer import contracts as _contracts
from veriself.materializer import merge as _merge
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
            # 重复粒度键在关闭/插入之前拒绝，避免把笛卡尔积写进事实表。
            counts: list[int] = []
            try:
                create_sql, create_params = statements[0]
                counts.append(_affected_count(_views._exec(conn, create_sql, create_params)))
                _merge.assert_unique_grain(conn, temp, metric_id)
                for sql_stmt, sql_params in statements[1:]:
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

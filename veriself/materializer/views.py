"""语义模型 → 物化视图（`obs_daily` / `evt_daily` / `subject_current`）。

逻辑列（通道 → 日粒度列、事件逻辑列、维度列）与"表名 → 查询别名"的映射全部来自
`semantic_models/*.yml`（`veriself.semantic_model`）——加一张来源表或一个
逻辑列只需改 YAML，本模块与 `sqlbuild` 都不再持有列清单。

注意：必须映射到**别名**而不是视图名。DuckDB 中 `FROM obs_daily o` 之后原表名 `obs_daily`
会被别名遮蔽，公式里再写 `obs_daily.xxx` 会直接 Binder Error。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from veriself import config, semantic_model

__all__ = ["ensure_views", "source_aliases"]


def _exec(conn: Any, sql: str, params: Sequence[Any] | None = None) -> Any:
    """执行并返回 fetchall 结果；DuckDB 连接与 cursor 都可直接 execute。"""
    return conn.execute(sql, list(params or [])).fetchall()


def _models(models: Mapping[str, Any] | None) -> dict[str, Any]:
    """解析 `models` 参数：None → 默认加载 `semantic_models/`（进程内缓存）。"""
    return dict(models) if models is not None else semantic_model.default_models()


def source_aliases(models: Mapping[str, Any] | None = None) -> dict[str, str]:
    """表名 → 查询别名（由语义模型派生，`sqlrefs.rewrite` / `_touches` 的唯一来源）。"""
    return {model.table: model.alias for model in _models(models).values() if model.alias}


def _channel_agg_sql(column: Any) -> str:
    """通道列 → `obs_daily` 的聚合表达式。

    ⚠️ 必须带 `FILTER (WHERE channel = ...)`：`fact_observation` 是"通道-值"窄表，
    漏掉过滤会让每个通道列都变成"当天全部通道的聚合"——行数正常但数值全错，极难发现。
    `channel` / `agg` 已在语义模型加载期校验（合法标识符 + 枚举），可安全拼 SQL。
    """
    return f"{column.agg}(value) FILTER (WHERE channel = '{column.channel}') AS {column.name}"


def ensure_views(conn: Any, models: Mapping[str, Any] | None = None) -> None:
    """幂等建立物化所需的三个视图（`obs_daily` / `evt_daily` / `subject_current`）。

    视图列清单由语义模型驱动；缺少任一必需来源表 → `ContractError`（fail-closed）。
    """
    resolved = _models(models)
    obs = resolved.get("fact_observation")
    evt = resolved.get("fact_event")
    sub = resolved.get("dim_subject")
    missing = [
        table for table in ("fact_observation", "fact_event", "dim_subject") if table not in resolved
    ]
    if missing:
        raise config.ContractError(f"语义模型缺少必需来源表: {missing}")

    obs_cols = ",\n        ".join(_channel_agg_sql(c) for c in obs.columns)
    evt_cols = ",\n        ".join(f"{c.expr} AS {c.name}" for c in evt.columns)
    sub_cols = ", ".join(c.name for c in sub.columns)
    _exec(
        conn,
        f"""
    CREATE OR REPLACE VIEW obs_daily AS
    SELECT
        date_key,
        subject_id,
        {obs_cols}
    FROM fact_observation
    GROUP BY date_key, subject_id
    """,
    )
    _exec(
        conn,
        f"""
    CREATE OR REPLACE VIEW evt_daily AS
    SELECT
        date_key,
        subject_id,
        {evt_cols}
    FROM fact_event
    GROUP BY date_key, subject_id
    """,
    )
    _exec(
        conn,
        f"""
    CREATE OR REPLACE VIEW subject_current AS
    SELECT subject_id, {sub_cols}
    FROM dim_subject
    WHERE is_current
    """,
    )

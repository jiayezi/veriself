"""契约字段访问原语（materializer 包内部共享）。

`_field` 兼容 Pydantic 模型与普通 dict 两种契约表示；`_metric_id` / `_upstreams` /
`_grain` / `_sql` 是物化器读契约的入口。`topo` / `merge` 都从这里 import，
禁止各自实现。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from veriself import config

__all__ = ["_alias", "_field", "_grain", "_metric_id", "_sql", "_upstreams"]


def _field(contract: Any, name: str, default: Any = None) -> Any:
    """兼容 Pydantic 模型与普通 dict 两种契约表示。
    产品路径只走 Pydantic，测试路径只走普通 dict。
    """
    if isinstance(contract, Mapping):
        return contract.get(name, default)
    return getattr(contract, name, default)


def _metric_id(contract: Any) -> str:
    return str(_field(contract, "metric_id", ""))


def _upstreams(contract: Any) -> tuple[str, ...]:
    ups = _field(contract, "depends_on", ()) or ()
    return tuple(str(item) for item in ups)


def _grain(contract: Any) -> str:
    grain = str(_field(contract, "grain", "day") or "day").lower()
    if grain not in config.GRAINS:
        raise config.ContractError(f"非法 grain: {grain}")
    return grain


def _sql(contract: Any) -> str:
    """指标 SELECT 原文。执行前去掉首尾空白和末尾分号，不改写语句本身。"""
    text = _field(contract, "sql", None)
    if not isinstance(text, str) or not text.strip():
        raise config.ContractError(f"{_metric_id(contract)} 缺少 sql")
    return text.strip().rstrip(";").strip()


def _alias(metric_id: str) -> str:
    """把 metric_id 转成合法且唯一的 SQL 标识符。"""
    return re.sub(r"[^0-9a-zA-Z]+", "_", metric_id).strip("_")

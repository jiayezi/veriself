"""契约字段访问原语（materializer 包内部共享）。

`_field` 兼容 Pydantic 模型与普通 dict 两种契约表示；`_metric_id` / `_upstreams` /
`_grain` 是物化器读契约的三个入口。`topo` / `sqlbuild` / `merge` 都从这里 import，
禁止各自实现（防止"某处读 `contract["lineage"]`、另一处读 `contract.lineage`"的漂移）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from veriself import config

__all__ = ["_bucket_agg", "_field", "_grain", "_metric_id", "_upstreams"]


def _field(contract: Any, name: str, default: Any = None) -> Any:
    """兼容 Pydantic 模型与普通 dict 两种契约表示。
    产品路径只走 Pydantic，测试路径只走普通 dict。
    """
    if isinstance(contract, Mapping):
        return contract.get(name, default)
    return getattr(contract, name, default)


def _bucket_agg(contract: Any) -> str | None:
    """契约 `bucket.agg`；无 `bucket` 块时返回 None。

    兼容 Pydantic 模型（`BucketSpec`）与普通 dict（`{"agg": "sum"}`）两种表示。
    """
    bucket = _field(contract, "bucket", None)
    if bucket is None:
        return None
    if isinstance(bucket, Mapping):
        return str(bucket.get("agg") or "")
    return str(getattr(bucket, "agg", "") or "")


def _metric_id(contract: Any) -> str:
    return str(_field(contract, "metric_id", ""))


def _upstreams(contract: Any) -> tuple[str, ...]:
    lineage = _field(contract, "lineage", {}) or {}
    if isinstance(lineage, Mapping):
        ups = lineage.get("upstream_metrics") or ()
    else:  # pydantic 子模型
        ups = getattr(lineage, "upstream_metrics", ()) or ()
    return tuple(str(u) for u in ups)


def _grain(contract: Any) -> str:
    grain = str(_field(contract, "grain", "day") or "day").lower()
    if grain not in config.GRAINS:
        raise config.ContractError(f"非法 grain: {grain}")
    return grain

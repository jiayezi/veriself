"""按 `lineage.upstream_metrics` 做拓扑分层（materializer 内部）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from veriself import config
from veriself.materializer import contracts as _contracts

__all__ = ["_topo_layers"]


def _topo_layers(contracts: Mapping[str, Any]) -> list[list[str]]:
    """按 lineage.upstream_metrics 分拓扑层，返回 [[layer0...], [layer1...], ...]。"""
    remaining = {mid: set(_contracts._upstreams(c)) for mid, c in contracts.items()}
    layers: list[list[str]] = []
    resolved: set[str] = set()

    while remaining:
        # 等价 ups.issubset(resolved)：该指标的全部上游是否都已算完
        ready = sorted(mid for mid, ups in remaining.items() if ups <= resolved)
        if not ready:
            cyclic = ", ".join(sorted(remaining))
            raise config.ContractError(f"契约存在环，无法拓扑分层: {cyclic}")
        layers.append(ready)
        resolved.update(ready)
        for mid in ready:
            remaining.pop(mid)
    return layers

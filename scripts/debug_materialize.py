"""从最外层 `materialize_all` 调试一个指标。

用法（在仓库根目录）：

    uv run python scripts/debug_materialize.py
    uv run python scripts/debug_materialize.py subject.sleep_need_deviation_daily

在 IDE 里对本文件启动调试，断点打在下面 `materialize_all(...)` 那一行，然后单步**进入**。
调用栈是：

    materialize_all          __init__.py   建视图，按层循环
      ensure_views           views.py      obs_daily / evt_daily / subject_asof，只建一次
      _topo_layers           topo.py       上游在前，同层按 metric_id 排序
      对每个 metric_id：
        build_merge_sql      merge.py      四条语句：临时表（指标 SQL 原文）、关变化、插入、关消失
        执行这四条语句；重复 (subject_id, date_key) 在插入前拒绝
        DROP 临时表

指标怎么算，看 `veriself/metrics/<metric_id>.sql`。引擎不改写它。

派生指标会把上游一并放进同一次 `materialize_all`。只传目标指标时，拓扑找不到上游，
求值 SQL 也读不到上游的 `fact_metric_value`。循环里先跑到的是上游，目标指标在最后一层。

数仓用 `config.WAREHOUSE_PATH`（源码布局下是仓库根的 `data/warehouse.duckdb`）。
文件不存在时会先合成数据。合并会写入这一个库；值没变时零写入。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

import duckdb

from veriself import config
from veriself.materializer import materialize_all
from veriself.materializer.contracts import _upstreams
from veriself.materializer.topo import _topo_layers
from veriself.semantic.contract import load_contracts
from veriself.warehouse.loader import ensure_schema

# 不传命令行参数时调试这个指标。第 0 层，公式只有一列。
DEFAULT_METRIC_ID = "subject.sleep_duration_daily"


def _with_upstreams(contracts: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    """目标指标加上它直接、间接依赖的上游。"""
    if metric_id not in contracts:
        known = "\n".join(f"  {mid}" for mid in sorted(contracts))
        raise SystemExit(f"未知指标 {metric_id}\n已注册：\n{known}")

    selected: dict[str, Any] = {}
    pending = [metric_id]
    while pending:
        current = pending.pop()
        if current in selected:
            continue
        if current not in contracts:
            raise SystemExit(f"{metric_id} 的上游 {current} 不在已加载契约里")
        selected[current] = contracts[current]
        pending.extend(_upstreams(contracts[current]))
    return selected


def _open_warehouse() -> duckdb.DuckDBPyConnection:
    """打开数仓。没有文件时先合成来源表并建 schema。"""
    path = config.WAREHOUSE_PATH
    if not path.exists():
        from veriself.synth import generate_all

        print(f"数仓不存在，先合成数据：{path}")
        generate_all(db_path=path)
    connection = duckdb.connect(str(path))
    ensure_schema(connection)
    return connection


def main(argv: list[str]) -> None:
    metric_id = argv[1] if len(argv) > 1 else DEFAULT_METRIC_ID
    selected = _with_upstreams(load_contracts(), metric_id)
    layers = _topo_layers(selected)
    print(f"目标指标  {metric_id}")
    print(f"数仓      {config.WAREHOUSE_PATH}")
    print("本次 materialize_all 的分层（上游在前）：")
    for depth, layer in enumerate(layers):
        mark = "  ← 目标" if metric_id in layer else ""
        print(f"  第 {depth} 层  {', '.join(layer)}{mark}")

    connection = _open_warehouse()
    try:
        # 断点打在下一行，单步进入 materialize_all。
        stats = materialize_all(connection, selected)
    finally:
        connection.close()

    print("合并统计：")
    for mid, row in stats.items():
        print(
            f"  {mid}  live={row['live']} inserted={row['inserted']}"
            f" changed={row['closed_changed']} vanished={row['closed_vanished']}"
            f" unchanged={row['unchanged']}"
        )


if __name__ == "__main__":
    main(sys.argv)

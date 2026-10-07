"""`warehouse` 包：DuckDB 星型模型的 DDL 与装载（IFACE-v1 契约第 1 节）。

对外只用这四个入口，DDL 一律来自 `warehouse/schema.sql`：

    from veriself.warehouse import (
        ensure_schema, upsert_dim_metric, materialize_metric, write_audit,
    )
"""

from __future__ import annotations

from veriself.warehouse.loader import (
    ensure_schema,
    materialize_metric,
    read_schema_sql,
    upsert_dim_metric,
    write_audit,
)

__all__ = [
    "ensure_schema",
    "materialize_metric",
    "read_schema_sql",
    "upsert_dim_metric",
    "write_audit",
]

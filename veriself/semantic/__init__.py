"""`semantic` 层公开 API（IFACE-v1 第 7 节，**冻结**；`interfaces` 按此调用）。

```python
from veriself.semantic import (
    MetricContract, load_contracts,          # 契约加载
    QueryRequest, CompiledQuery, QueryResult, # 查询对象
    compile_query, execute_query,             # 编译与执行
    list_metrics, describe_metric,            # 目录
)
```

铁律（契约 §7）：`interfaces` 层不得自行拼 SQL、不得直接 import duckdb 执行业务查询，
一律走 `compile_query` + `execute_query`。
"""

from __future__ import annotations

from veriself.semantic.catalog import describe_metric, list_metrics
from veriself.semantic.compiler import compile_query, estimate_scan_rows, execute_query
from veriself.semantic.contract import (
    AS_OF_DEFINITION,
    MetricContract,
    load_contracts,
    metric_contract_from_mapping,
)
from veriself.semantic.enforcement import (
    ALLOWED_FUNCTIONS,
    RLS_DENIED_PREFIX,
    check_ast_join_path,
    validate_ast,
)
from veriself.semantic.query import CompiledQuery, QueryRequest, QueryResult

__all__ = [  # noqa: RUF022 — 按契约分组（冻结 API 在前），刻意不按字母序
    # 契约 §7 冻结 API
    "MetricContract",
    "load_contracts",
    "QueryRequest",
    "CompiledQuery",
    "QueryResult",
    "compile_query",
    "execute_query",
    "list_metrics",
    "describe_metric",
    # 附带导出（便于测试与审计，不属于冻结 API）
    "AS_OF_DEFINITION",
    "RLS_DENIED_PREFIX",
    "ALLOWED_FUNCTIONS",
    "validate_ast",
    "check_ast_join_path",
    "estimate_scan_rows",
    "metric_contract_from_mapping",
]

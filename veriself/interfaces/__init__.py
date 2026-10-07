"""`interfaces` 层：命令行（`veriself`）与 MCP server。

本层是「AI 不能写 SQL」这一卖点的门面：

* 对外（CLI 参数、MCP 工具入参）只暴露**结构化查询对象**
  （`metrics` / `dimensions` / `filters` / `grain` / `order_by` / `limit`），
  不存在任何可以传原生语句的字段；
* 对内一律调用 `veriself.semantic` 的冻结 API
  （见 `docs/00-接口契约.md` 第 7 节），本层从不拼装业务 SQL。

模块划分：

===============  ====================================================
`gateway`        与下游模块（semantic/warehouse/synth/materializer）的
                 唯一适配层：函数内延迟导入 + 签名容错 + 失败降级
`auditlog`       只读审计表 `fact_audit_log`（契约允许的唯一例外）
`render`         rich 渲染（表格 / 面板 / 拒绝对比）
`cli`            Typer 入口 `veriself.interfaces.cli:app`
`mcp_server`     stdio MCP server，4 个工具
===============  ====================================================
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

"""veriself —— 面向 AI agent 的类型化指标执行层。

LLM 不能写 SQL，只能组合已注册的指标；每个数字都带口径版本、粒度、血缘与审计头。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "config"]

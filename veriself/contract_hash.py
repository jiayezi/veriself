# -*- coding: utf-8 -*-
"""`contract_hash` 的唯一权威实现（Lead 所有）。

契约见 `docs/00-接口契约.md` 第 4 节：对规范化后的契约文本取 sha256，前缀 `sha256:`，取前 16 位 hex。

**为什么单独成模块**：`warehouse`（写 dim_metric）与 `semantic`（出审计头）都要用这个哈希，
如果各自实现一份必然漂移，导致"同一个契约两个哈希"，审计头就失去意义。
因此这里提供纯函数，两边都必须 import 本模块，不得自行实现。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

HASH_PREFIX = "sha256:"
HASH_HEX_LEN = 16


def canonicalize(contract: Mapping[str, Any]) -> str:
    """把契约规范化成稳定文本：键排序、UTF-8、不转义非 ASCII、紧凑分隔符。"""
    return json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def contract_hash(contract: Mapping[str, Any]) -> str:
    """计算契约哈希。`contract` 可以是原始 YAML 解析出的 dict。"""
    digest = hashlib.sha256(canonicalize(contract).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest[:HASH_HEX_LEN]}"


# 固定样例：两个模块的实现都必须复现这个值，用于一致性核对。
GOLDEN_CONTRACT: dict[str, Any] = {
    "metric_id": "subject.demo",
    "version": 1,
    "unit": "hour",
    "agg": "sum",
    "grain": "day",
}
GOLDEN_CONTRACT_HASH = "sha256:015c2525e5106928"

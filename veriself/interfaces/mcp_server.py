"""`veriself` 的 MCP server（stdio）：把指标执行层暴露给 LLM 客户端。

4 个工具
========

=================  =========================================================
`list_metrics`     列出全部指标契约摘要（metric_id / 单位 / 方向 / 粒度 / 版本）
`describe_metric`  单个指标的完整契约：口径、公式、血缘、白名单维度、契约哈希
`query_metric`     执行结构化查询对象，返回契约第 4 节结构 `data` + `audit`
`explain_result`   解释结果口径：上卷语义、RLS 可见角色、as-of、契约哈希（可挂某次审计）
=================  =========================================================

设计要点（`docs/00-接口契约.md` 第 3、4、5 节）：

* `query_metric` 的入参**只有** `metrics` / `dimensions` / `filters` / `grain` /
  `order_by` / `limit` 六个字段，且入参 schema 被收紧成
  `additionalProperties = false`：协议层就没有任何可以夹带手写查询语句的开放字段。
  多传的字段会被 MCP 的入参校验直接挡掉。
* 所有查询都经 `veriself.semantic` 的冻结 API 执行（`interfaces/gateway.py`），
  本模块不拼装任何业务查询语句。
* "预期失败"（未注册指标、维度越界、粒度不兼容、查询对象非法、下游未就绪）统一返回
  `{"ok": false, "reason": ...}`，让 LLM 客户端能读到契约规定的 `reason` 并自行修正；
  只有真正的崩溃才作为异常抛给 SDK。
* 角色固定为 `owner`（`query_metric` 不开放角色字段 —— 契约第 3 节只允许那 6 个键）；
  需要看 RLS 口径时用 `explain_result(role=...)`。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

try:  # mcp 2.x：FastMCP 已更名为 MCPServer
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.tools import Tool
except ModuleNotFoundError as _exc:  # pragma: no cover - 依赖已在 pyproject 声明
    raise ModuleNotFoundError(
        "veriself.interfaces.mcp_server 需要 mcp>=2（FastMCP 在 mcp 2 里更名为 "
        f"MCPServer）；当前环境不满足：{_exc}"
    ) from _exc

from veriself import config
from veriself.interfaces import __version__, auditlog, gateway

__all__ = [
    "VeriselfServer",
    "build_server",
    "main",
    "server",
]

#: `query_metric` 允许的入参字段（契约第 3 节的查询对象，一个不多一个不少）。
QUERY_FIELDS: tuple[str, ...] = ("metrics", "dimensions", "filters", "grain", "order_by", "limit")

#: `explain_result` 允许的入参字段。
EXPLAIN_FIELDS: tuple[str, ...] = ("metrics", "role", "as_of", "audit_id")

#: 过滤器取值：只允许标量与标量列表（键由契约白名单校验，值由 semantic 做注入面检查）。
FilterValue = str | int | float | list[str | int | float]

#: 契约第 2 节冻结的跨粒度上卷语义（用于向 LLM 解释"这个数是怎么合出来的"）。
ROLLUP_SEMANTICS: dict[str, str] = {
    "sum": "请求粒度更粗时按 SUM(value) 上卷（睡眠债、支出、次数）",
    "mean": "请求粒度更粗时按 AVG(value) 上卷（时长、得分）",
    "min": "请求粒度更粗时按 MIN(value) 上卷",
    "max": "请求粒度更粗时按 MAX(value) 上卷",
    "last": "请求粒度更粗时取时间上最后一个值",
    "same_grain": "请求粒度等于指标声明粒度时**不做聚合**，直接返回原始值（契约第 2 节）",
}

_SERVER_NAME = "veriself"

_INSTRUCTIONS = (
    "这是一个「AI 只能提交结构化查询对象」的指标执行层，不是通用数据库客户端。\n"
    "1) 先 list_metrics 看有哪些指标，再 describe_metric 看口径/血缘/可用维度与过滤器；\n"
    "2) 查询一律用 query_metric，参数只有 metrics/dimensions/filters/grain/order_by/limit；"
    "不存在任何可以传入手写查询语句的字段；\n"
    "3) 越界或未注册的请求会被拒绝，返回 {ok:false, reason:...}；reason 前缀是稳定的"
    "（unknown_metric / deprecated_metric / dimension_not_allowed / filter_not_allowed / "
    "grain_not_compatible / ast_violation / scan_too_large / invalid_query_object），"
    "请按 reason 修正后重试，不要试图绕过；\n"
    "4) query_metric 固定以 owner 角色执行；想看 RLS 口径用 explain_result。"
)


# --------------------------------------------------------------------------- 输出封装


def _dump(payload: Any) -> str:
    """序列化成 JSON 文本（LLM 客户端直接可读；datetime 等退化为字符串）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _ok(payload: Mapping[str, Any]) -> str:
    """成功信封：`{"ok": true, ...}`。"""
    return _dump({"ok": True, **dict(payload)})


def _error(reason: str) -> str:
    """失败信封：`{"ok": false, "reason": ..., "rule": ...}`（reason 前缀即 rule）。"""
    text = str(reason)
    return _dump({"ok": False, "reason": text, "rule": text.split(":", 1)[0]})


def _failure(exc: BaseException) -> str:
    """把**预期失败**翻译成 `ok=false`；意外错误继续上抛（由 SDK 记为 is_error）。"""
    if isinstance(exc, (gateway.GatewayUnavailable, auditlog.AuditLogUnavailable)):
        return _error(str(exc))
    if gateway.is_project_error(exc):
        return _error(gateway.rejection_reason(exc))
    raise exc


def _find_field(detail: Mapping[str, Any], *tokens: str) -> Any:
    """按字段名子串取契约字段（容忍 semantic 的字段改名，例如公式字段）。"""
    for key, value in detail.items():
        lowered = str(key).lower()
        if all(token in lowered for token in tokens):
            return value
    return None


def _visible_roles(policy: Any) -> list[str]:
    """`rls_policy` → 可访问角色列表（未知策略给空列表，不抛异常）。"""
    if not isinstance(policy, str) or not policy:
        return []
    try:
        return sorted(role.value for role in config.rls_visible_roles(policy))
    except ValueError:
        return []


# --------------------------------------------------------------------------- 工具实现


def _list_metrics() -> str:
    """列出全部指标契约摘要（metric_id / display_name / unit / direction / grain / status / version）。"""
    try:
        contracts = gateway.load_contracts()
        rows = gateway.list_metric_summaries(contracts)
        return _ok({"metrics": rows, "count": len(rows)})
    except Exception as exc:  # noqa: BLE001 — 工具边界漏斗，见 `_failure`
        return _failure(exc)


def _describe_metric(metric_id: str) -> str:
    """查一个指标的完整契约：口径、公式、血缘（数据源/上游指标）、可用维度与过滤器白名单、契约哈希、版本。

    metric_id 未注册时返回 `{"ok": false, "reason": "unknown_metric: ..."}`。
    """
    try:
        contracts = gateway.load_contracts()
        detail = gateway.describe(contracts, metric_id)
        policy = detail.get("rls_policy")
        return _ok({
            "metric": detail,
            "visible_roles": _visible_roles(policy),
            "rls_policy": policy,
        })
    except Exception as exc:  # noqa: BLE001 — 工具边界漏斗，见 `_failure`
        return _failure(exc)


def _query_metric(
    metrics: list[str],
    dimensions: list[str] | None = None,
    filters: dict[str, FilterValue] | None = None,
    grain: str | None = None,
    order_by: list[dict[str, str]] | None = None,
    limit: int = config.DEFAULT_LIMIT,
) -> str:
    """按指标契约查询数据，返回契约第 4 节的 `data` + `audit`（含版本、契约哈希、RLS 改写与强制校验链）。

    入参就是契约第 3 节的查询对象本身：metrics 必填；dimensions / filters / grain / order_by /
    limit 可空。维度与过滤器必须在该指标的白名单内，否则以非 ok 结果返回具体 reason。
    本工具没有、也不会有任何可以传入原生查询语句的参数。
    """
    try:
        payload = gateway.normalize_payload(
            metrics=metrics,
            dimensions=dimensions,
            filters=filters,
            grain=grain,
            order_by=order_by,
            limit=limit,
        )
        contracts = gateway.load_contracts()
        _compiled, result = gateway.query(payload, contracts, role=config.Role.OWNER)
        return _ok(gateway.result_payload(result))
    except Exception as exc:  # noqa: BLE001 — 工具边界漏斗，见 `_failure`
        return _failure(exc)


def _explain_result(
    metrics: list[str],
    role: str = config.Role.OWNER.value,
    as_of: str | None = None,
    audit_id: int | None = None,
) -> str:
    """解释结果口径：每个指标的公式/血缘/单位/方向/上卷语义，以及 RLS 可见角色、as-of 口径、契约哈希与版本。

    可选 `audit_id`：把某次查询的审计记录（请求、编译结果、RLS、校验链、结果状态）一起返回，
    用于回答"这个数当时是怎么被允许查出来的"。
    """
    try:
        resolved_role = config.Role(role)
    except ValueError:
        return _error(f"invalid_query_object: role 必须是 owner|partner|researcher（收到 {role!r}）")
    try:
        contracts = gateway.load_contracts()
        entries: list[dict[str, Any]] = []
        versions: dict[str, Any] = {}
        hashes: dict[str, Any] = {}
        policies: dict[str, Any] = {}
        for metric_id in metrics:
            detail = gateway.describe(contracts, metric_id)
            policy = detail.get("rls_policy")
            aggregate = str(detail.get("agg") or "")
            entries.append({
                "metric_id": detail.get("metric_id", metric_id),
                "display_name": detail.get("display_name"),
                "definition": detail.get("definition"),
                "formula": _find_field(detail, "formula"),
                "unit": detail.get("unit"),
                "direction": detail.get("direction"),
                "grain": detail.get("grain"),
                "agg": detail.get("agg"),
                "rollup": ROLLUP_SEMANTICS.get(aggregate, "见契约第 2 节的跨粒度上卷语义"),
                "lineage": detail.get("lineage"),
                "allowed_dimensions": list(detail.get("allowed_dimensions") or []),
                "allowed_filters": list(detail.get("allowed_filters") or []),
                "status": detail.get("status"),
                "version": detail.get("version"),
                "contract_hash": detail.get("contract_hash"),
                "rls_policy": policy,
                "visible_roles": _visible_roles(policy),
            })
            versions[str(metric_id)] = detail.get("version")
            hashes[str(metric_id)] = detail.get("contract_hash")
            policies[str(metric_id)] = policy

        explanation: dict[str, Any] = {
            "role": resolved_role.value,
            "as_of_definition": as_of or datetime.now(UTC).date().isoformat(),
            "metric_versions": versions,
            "contract_hashes": hashes,
            "rls_applied": policies,
            "enforced_checks": list(config.ENFORCED_CHECKS),
            "rollup_semantics": ROLLUP_SEMANTICS,
            "metrics": entries,
        }
        if audit_id is not None:
            explanation["audit"] = auditlog.fetch_one(audit_id)
        return _ok(explanation)
    except Exception as exc:  # noqa: BLE001 — 工具边界漏斗，见 `_failure`
        return _failure(exc)


# --------------------------------------------------------------------------- server 组装


class VeriselfServer(MCPServer):
    """MCPServer + 一个同步的公开 `tools` 视图。

    SDK 只给了 `async list_tools()` 和私有的 `_tool_manager`；红队自检/演示脚本想同步检查
    "注册了哪些工具、入参 schema 长什么样"时不方便。这里补一个只读属性，
    语义等价于 `await list_tools()`，并把 `_tool_manager` 收敛在这一个地方。
    """

    @property
    def tools(self) -> list[Tool]:
        """已注册工具（`Tool` 对象带 `.name` / `.description` / `.parameters`）。"""
        return list(self._tool_manager.list_tools())


def _build_tool(fn: Any, *, name: str, description: str, allowed: Sequence[str]) -> Tool:
    """注册一个工具，并把入参 schema 收紧成"封闭对象"。

    这是对 LLM 客户端能做出的最强承诺：入参对象只有白名单里的字段，
    多传字段在 MCP 入参校验层就被拒（`additionalProperties = false`）。
    注册时顺带自检：函数签名与白名单不一致就直接失败，避免悄悄放宽。
    """
    tool = Tool.from_function(fn, name=name, description=description)
    schema = tool.parameters
    schema["type"] = "object"
    properties = schema.setdefault("properties", {})
    unexpected = sorted(set(properties) - set(allowed))
    missing = sorted(set(allowed) - set(properties))
    if unexpected or missing:
        raise RuntimeError(f"工具 {name} 的入参 schema 与白名单不一致：多余 {unexpected}，缺失 {missing}")
    schema["additionalProperties"] = False
    return tool


def build_server() -> VeriselfServer:
    """构造 MCP server（4 个工具，stdio 传输）。每次调用都会重新注册工具。"""
    return VeriselfServer(
        name=_SERVER_NAME,
        title="veriself 指标执行层",
        description="AI 只能提交结构化查询对象、无法手写查询语句的指标执行层。",
        instructions=_INSTRUCTIONS,
        version=__version__,
        tools=[
            _build_tool(
                _list_metrics,
                name="list_metrics",
                description="列出全部指标契约摘要（metric_id / 单位 / 方向 / 粒度 / 状态 / 版本）。",
                allowed=(),
            ),
            _build_tool(
                _describe_metric,
                name="describe_metric",
                description=(
                    "查单个指标的完整契约：口径、公式、血缘、可用维度与过滤器白名单、契约哈希、版本。"
                ),
                allowed=("metric_id",),
            ),
            _build_tool(
                _query_metric,
                name="query_metric",
                description=(
                    "执行结构化查询对象并返回 data + audit（版本/契约哈希/RLS 改写/强制校验链）。"
                    "参数只有 metrics/dimensions/filters/grain/order_by/limit，"
                    "没有任何可以传入原生查询语句的字段；越界请求会返回 ok=false 与 reason。"
                ),
                allowed=QUERY_FIELDS,
            ),
            _build_tool(
                _explain_result,
                name="explain_result",
                description=(
                    "解释结果的算法口径：公式、血缘、单位与方向、跨粒度上卷语义、RLS 可见角色、"
                    "as-of 口径、契约哈希与版本；可用 audit_id 挂上某次查询的审计记录。"
                ),
                allowed=EXPLAIN_FIELDS,
            ),
        ],
    )


#: 模块级 server 实例（`veriself mcp` 与 `python -m veriself.interfaces.mcp_server` 共用）。
server = build_server()


def main() -> None:
    """以 stdio 传输启动 MCP server（阻塞直到客户端断开）。"""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

"""`interfaces` 层与下游模块之间**唯一**的适配层。

职责与设计约束（`docs/00-接口契约.md` 第 6/7/8 节）：

1. **不拼业务 SQL**：所有指标查询都调用 `veriself.semantic` 的冻结 API
   （`load_contracts` / `QueryRequest.from_json` / `compile_query` /
   `execute_query` / `list_metrics` / `describe_metric`）。本模块只做参数搬运。
2. **延迟导入**：`semantic` / `warehouse` / `synth` / `materializer` 由队友并行开发，
   可能在 import 期并不存在。所有 import 都发生在函数内，并把异常收敛成
   `GatewayUnavailable`，让 CLI/MCP 打印可读原因而不是抛栈。
3. **测试注入点**：`tests/test_interfaces.py` 通过 monkeypatch `semantic()` /
   `warehouse()` / `synth()` / `materializer()` 注入假实现，**不复制**一份语义层实现。
4. **签名容错**：第 7/8 节只冻结了 `semantic` 与 `materializer`；`warehouse` 与
   `synth` 的签名未冻结（例如 `ensure_schema(conn)` 与 `ensure_schema(db_path)` 都合理），
   因此用 `invoke_unfrozen()` 按形参名适配，并在无法适配时给出明确原因。
"""

from __future__ import annotations

import difflib
import importlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from veriself import config

__all__ = [
    "GatewayUnavailable",
    "as_dict",
    "build_request",
    "closest_names",
    "compile_query",
    "describe",
    "ensure_schema",
    "execute_query",
    "generate_all",
    "invoke_unfrozen",
    "is_project_error",
    "list_metric_summaries",
    "load_contracts",
    "materialize_all",
    "materializer",
    "metric_ids",
    "normalize_order_by",
    "normalize_payload",
    "open_warehouse",
    "query",
    "rejection_reason",
    "result_payload",
    "semantic",
    "synth",
    "upsert_dim_metrics",
    "warehouse",
]

# 下游函数可能用同名但不同源的异常类（例如 semantic 自己定义一个 ContractError），
# 因此除了 isinstance 之外再按类名兜底识别。
_PROJECT_ERROR_NAMES: frozenset[str] = frozenset(
    {"VeriselfError", "ContractError", "EnforcementError", "QueryError"}
)

_SEMANTIC_MODULE = "veriself.semantic"
_WAREHOUSE_MODULES = ("veriself.warehouse.loader", "veriself.warehouse")


class GatewayUnavailable(RuntimeError):
    """下游模块未就绪：缺失、import 失败、或公开 API 与契约不符。"""

    def __init__(self, module: str, detail: str) -> None:
        self.module = module
        self.detail = detail
        super().__init__(f"downstream_unavailable: {module} ({detail})")


# --------------------------------------------------------------------------- 延迟导入


def _import(module_name: str) -> Any:
    """import 一个下游模块；任何失败都转成 `GatewayUnavailable`。"""
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # ImportError 及其连带错误（下游自身依赖缺失时也会走到这里）
        raise GatewayUnavailable(module_name, f"{type(exc).__name__}: {exc}") from exc


def semantic() -> Any:
    """返回 `veriself.semantic` 模块（契约第 7 节）。"""
    return _import(_SEMANTIC_MODULE)


def warehouse() -> Any:
    """返回 `veriself.warehouse` 模块（契约第 1 节：DDL 与装载）。"""
    return _import("veriself.warehouse")


def synth() -> Any:
    """返回 `veriself.synth` 模块（合成数据生成器）。"""
    return _import("veriself.synth")


def materializer() -> Any:
    """返回 `veriself.materializer` 模块（契约第 8 节）。"""
    return _import("veriself.materializer")


def _member(module_names: Sequence[str], attr: str, *, get_module: Any = None) -> tuple[Any, str]:
    """在候选模块（含其 `loader` 子模块）里查找可调用成员。

    `get_module` 保留可打桩的间接层：测试 monkeypatch `gateway.warehouse` 之后，
    `ensure_schema` / `upsert_dim_metric` 必须走打桩版本，不能偷偷 import 真实模块。

    返回 `(可调用对象, 人读标签)`；找不到时抛 `GatewayUnavailable` 并列出查找过的位置。
    """
    resolve = get_module or _import
    tried: list[str] = []
    for module_name in module_names:
        try:
            module = resolve(module_name)
        except GatewayUnavailable as exc:
            tried.append(f"{module_name}（{exc.detail}）")
            continue
        holders: list[tuple[str, Any]] = [(module_name, module)]
        loader = getattr(module, "loader", None)
        if loader is not None and loader is not module:
            holders.append((f"{module_name}.loader", loader))
        for holder_name, holder in holders:
            fn = getattr(holder, attr, None)
            if callable(fn):
                return fn, f"{holder_name}.{attr}"
        tried.append(f"{module_name}（无 {attr}）")
    detail = "；".join(tried) if tried else "无候选模块"
    raise GatewayUnavailable(" / ".join(module_names), f"找不到 {attr}()：{detail}")


def _warehouse_module(module_name: str) -> Any:
    """取 `warehouse` 包或其 `loader` 子模块，两者都经 `warehouse()` 间接层。"""
    package = warehouse()
    if module_name.endswith(".loader"):
        loader = getattr(package, "loader", None)
        if loader is not None:
            return loader
        try:  # 真实包里 loader 子模块可能没被 __init__ 导入
            return _import(module_name)
        except GatewayUnavailable:
            return package
    return package


def _param_names(fn: Any) -> list[str]:
    """尽力取出函数形参名；取不到时返回空列表。"""
    try:
        return [
            p.name
            for p in inspect.signature(fn).parameters.values()
            if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
    except (TypeError, ValueError):  # 内建函数 / C 扩展
        return []


def invoke_unfrozen(
    fn: Any,
    *,
    candidates: Mapping[str, Any],
    fallback: Sequence[Any] = (),
    label: str = "",
) -> Any:
    """调用**签名未冻结**的下游函数。

    规则：先按形参名从 `candidates` 取值；命名不匹配且没有默认值的必填形参，
    按 `fallback` 顺序补位。这样 `ensure_schema(conn)` / `ensure_schema(connection)` /
    `ensure_schema(db_path)` / `ensure_schema()` 四种写法都能正确调用。
    """
    name = label or f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(*fallback)

    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    remaining = list(fallback)
    for param in params:
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.name in candidates:
            value = candidates[param.name]
        elif param.default is not inspect.Parameter.empty:
            value = param.default
        elif remaining:
            value = remaining.pop(0)
        else:
            raise GatewayUnavailable(name, f"签名未冻结且无法适配必填参数 {param.name!r}")
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            keywords[param.name] = value
        else:
            positional.append(value)
    return fn(*positional, **keywords)


# --------------------------------------------------------------------------- 结果归一化


def as_dict(obj: Any) -> dict[str, Any]:
    """把 pydantic 模型 / 映射 / dataclass / 普通对象统一成 dict（只用于展示与序列化）。"""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return {str(key): value for key, value in obj.items()}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return {str(key): value for key, value in dumped.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {str(key): value for key, value in asdict(obj).items()}
    raw = getattr(obj, "__dict__", None)
    if isinstance(raw, Mapping) and raw:
        return {str(key): value for key, value in raw.items() if not str(key).startswith("_")}
    return {"value": obj}


def is_project_error(exc: BaseException) -> bool:
    """判断异常是否属于契约定义的失败（ContractError / EnforcementError / QueryError）。"""
    return (
        isinstance(exc, config.VeriselfError)
        or type(exc).__name__ in _PROJECT_ERROR_NAMES
    )


def rejection_reason(exc: BaseException) -> str:
    """抽出契约第 5 节规定的 `reason` 字符串（必须以固定前缀开头）。

    `EnforcementError(rule, detail)` 的两种实现习惯都能兼容：
    `rule="unknown_metric", detail="subject.x"` 与
    `rule="registered", detail="unknown_metric: subject.x"`。
    """
    rule = getattr(exc, "rule", None)
    detail = getattr(exc, "detail", None)
    prefixes = tuple(config.REASON_PREFIXES.values())
    for text in (detail, str(exc)):
        candidate = "" if text is None else str(text).strip()
        if candidate and candidate.startswith(prefixes):
            return candidate
    if rule is not None and detail is not None:
        return f"{rule}: {detail}".strip()
    return str(exc).strip() or type(exc).__name__


# --------------------------------------------------------------------------- 数仓连接


def open_warehouse(db_path: Path | str, *, read_only: bool = False) -> Any:
    """打开 DuckDB 连接。

    这里 import duckdb 只是为了**开连接**（交给 semantic/warehouse 使用），
    本层不执行任何业务查询；唯一的直读例外是 `interfaces/auditlog.py` 的审计表。
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - 依赖已 vendored
        raise GatewayUnavailable("duckdb", str(exc)) from exc
    path = Path(db_path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


# --------------------------------------------------------------------------- 契约目录


def load_contracts(metrics_dir: Path | str | None = None) -> dict[str, Any]:
    """加载指标契约（契约第 7 节 `load_contracts`）。"""
    module = semantic()
    fn = getattr(module, "load_contracts", None)
    if not callable(fn):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 load_contracts()")
    contracts = fn(Path(metrics_dir)) if metrics_dir is not None else fn()
    if not isinstance(contracts, Mapping):
        raise GatewayUnavailable(
            _SEMANTIC_MODULE, f"load_contracts() 返回 {type(contracts).__name__}，期望映射"
        )
    return {str(key): value for key, value in contracts.items()}


def metric_ids(contracts: Mapping[str, Any]) -> list[str]:
    """契约里的全部 metric_id（排序后，供建议与展示使用）。"""
    ids: set[str] = set()
    for key, contract in contracts.items():
        value = getattr(contract, "metric_id", None)
        if value is None and isinstance(contract, Mapping):
            value = contract.get("metric_id")
        ids.add(str(value or key))
    return sorted(ids)


def list_metric_summaries(contracts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """指标目录摘要（契约第 7 节 `list_metrics`）。"""
    if not contracts:
        return []
    module = semantic()
    fn = getattr(module, "list_metrics", None)
    if not callable(fn):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 list_metrics()")
    produced = fn(contracts)
    if not produced:
        return []
    if isinstance(produced, (str, bytes)) or not isinstance(produced, Iterable):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "list_metrics() 必须返回行的可迭代对象")
    return [as_dict(row) for row in produced]


def describe(contracts: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    """单个指标的完整契约 + contract_hash + 血缘（契约第 7 节 `describe_metric`）。"""
    module = semantic()
    fn = getattr(module, "describe_metric", None)
    if not callable(fn):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 describe_metric()")
    return as_dict(fn(contracts, metric_id))


def closest_names(name: str, candidates: Sequence[str], *, limit: int = 3) -> list[str]:
    """`difflib` 近似匹配（`veriself reject` / `metrics show` 的建议来源）。"""
    pool = [str(item) for item in candidates]
    if not name or not pool:
        return []
    matches = difflib.get_close_matches(str(name), pool, n=limit, cutoff=0.45)
    if not matches:  # 阈值太严时退回"最接近的几个"，保证演示里总有可比照的候选
        matches = difflib.get_close_matches(str(name), pool, n=limit, cutoff=0.0)
    return matches


# --------------------------------------------------------------------------- 查询链路


def _as_str_list(value: Any) -> list[str]:
    """把 `"a,b"` / `["a", "b"]` 统一成 `["a", "b"]`（去空白、丢空项）。"""
    if value is None:
        return []
    raw = [value] if isinstance(value, (str, bytes)) else list(value)
    items: list[str] = []
    for entry in raw:
        items.extend(part.strip() for part in str(entry).split(","))
    return [item for item in items if item]


def normalize_order_by(value: Any) -> list[dict[str, str]]:
    """规整排序项：接受 `None` / 映射 / 序列（元素可为映射或 `"date.day:asc"` 简写）。

    Raises:
        config.QueryError: 结构非法（缺 field、dir 不是 asc/desc）。
    """
    if value in (None, "", [], {}):
        return []
    if isinstance(value, Mapping):
        value = [value]
    if isinstance(value, (str, bytes)):
        value = [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]
    if not isinstance(value, Sequence):
        raise config.QueryError("invalid_query_object: order_by 必须是数组或 'date.day:asc' 简写")
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            field = str(item.get("field") or item.get("name") or "").strip()
            direction = str(item.get("dir") or item.get("direction") or "asc").strip().lower()
        else:
            field, _, direction = str(item).partition(":")
            field, direction = field.strip(), (direction.strip() or "asc").lower()
        if not field:
            raise config.QueryError("invalid_query_object: order_by 的 field 不能为空")
        if direction not in {"asc", "desc"}:
            raise config.QueryError(
                f"invalid_query_object: order_by 的 dir 只能是 asc/desc（收到 {direction!r}）"
            )
        result.append({"field": field, "dir": direction})
    return result


def normalize_payload(
    *,
    metrics: Any,
    dimensions: Any = None,
    filters: Any = None,
    grain: Any = None,
    order_by: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """组装契约第 3 节的查询对象，**只产出这 6 个键**（CLI 与 MCP 共用同一套规则）。

    Raises:
        config.QueryError: 查询对象结构非法（空 metrics、limit < 1、filters 不是对象等）。
    """
    metric_list = _as_str_list(metrics)
    if not metric_list:
        raise config.QueryError("invalid_query_object: metrics 不能为空（至少一个 metric_id）")
    resolved_limit = config.DEFAULT_LIMIT if limit is None else int(limit)
    if resolved_limit < 1:
        raise config.QueryError(f"invalid_query_object: limit 必须 >= 1（收到 {limit}）")
    resolved_limit = min(resolved_limit, config.DEFAULT_LIMIT)  # 超过上限一律截断，避免全表扫描

    payload: dict[str, Any] = {"metrics": metric_list, "limit": resolved_limit}
    dims = _as_str_list(dimensions)
    if dims:
        payload["dimensions"] = dims
    if filters is not None:
        if not isinstance(filters, Mapping):
            raise config.QueryError("invalid_query_object: filters 必须是对象（键=过滤器名，值=参数）")
        payload["filters"] = {str(key): value for key, value in filters.items()}
    if isinstance(grain, str) and grain.strip():
        payload["grain"] = grain.strip()
    order = normalize_order_by(order_by)
    if order:
        payload["order_by"] = order
    return payload


def build_request(payload: Mapping[str, Any]) -> Any:
    """把结构化查询对象（契约第 3 节）交给 semantic 解析。

    刻意调用冻结的 `QueryRequest.from_json()`：注入面检查（原生查询关键字 / 注释符 /
    分号等）是 semantic 的职责，本层只负责把 JSON 文本递过去，不另立一套规则。
    """
    module = semantic()
    request_cls = getattr(module, "QueryRequest", None)
    if request_cls is None:
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 QueryRequest")
    from_json = getattr(request_cls, "from_json", None)
    if not callable(from_json):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "QueryRequest 缺少 from_json()")
    raw = json.dumps(dict(payload), ensure_ascii=False)
    return from_json(raw)


def compile_query(request: Any, contracts: Mapping[str, Any], role: config.Role) -> Any:
    """执行五条强制校验并生成 SQL（契约第 7 节 `compile_query`）。"""
    module = semantic()
    fn = getattr(module, "compile_query", None)
    if not callable(fn):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 compile_query()")
    return fn(request, contracts, role)


def execute_query(
    compiled: Any,
    *,
    role: config.Role,
    conn: Any = None,
    audit: bool = True,
) -> Any:
    """执行 + EXPLAIN 预检 + 写审计日志（契约第 7 节 `execute_query`）。"""
    module = semantic()
    fn = getattr(module, "execute_query", None)
    if not callable(fn):
        raise GatewayUnavailable(_SEMANTIC_MODULE, "缺少 execute_query()")
    return invoke_unfrozen(
        fn,
        candidates={
            "compiled": compiled,
            "query": compiled,
            "role": role,
            "conn": conn,
            "connection": conn,
            "audit": audit,
        },
        fallback=(compiled, role, conn, audit),
        label=f"{_SEMANTIC_MODULE}.execute_query",
    )


def query(
    payload: Mapping[str, Any],
    contracts: Mapping[str, Any],
    *,
    role: config.Role,
    conn: Any = None,
) -> tuple[Any, Any]:
    """完整查询链路：`build_request` → `compile_query` → `execute_query`。"""
    request = build_request(payload)
    compiled = compile_query(request, contracts, role)
    result = execute_query(compiled, role=role, conn=conn)
    return compiled, result


def result_payload(result: Any) -> dict[str, Any]:
    """把 `QueryResult` 归一化成契约第 4 节的结构：`{"data": [...], "audit": {...}}`。"""
    payload = as_dict(result)
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        for row in data:
            rows.append(as_dict(row) if not isinstance(row, Mapping) else dict(row))
    return {"data": rows, "audit": as_dict(payload.get("audit"))}


# ------------------------------------------------------- warehouse / synth / materializer


def ensure_schema(conn: Any, *, db_path: Any = None) -> str:
    """建表（契约第 1 节 DDL，唯一来源 `warehouse/schema.sql`）。返回被调用的函数标签。"""
    fn, label = _member(_WAREHOUSE_MODULES, "ensure_schema", get_module=_warehouse_module)
    invoke_unfrozen(
        fn,
        candidates={"conn": conn, "connection": conn, "db_path": db_path, "path": db_path,
                    "warehouse_path": db_path},
        fallback=(conn, db_path),
        label=label,
    )
    return label


def upsert_dim_metrics(conn: Any, contracts: Mapping[str, Any]) -> int:
    """把契约写入维度表（`warehouse` 层的 `upsert_dim_metric`），返回处理的契约数。

    下游签名未冻结，兼容两种写法：`upsert_dim_metric(conn, contract)`（逐个）
    与 `upsert_dim_metric(conn, contracts)`（一次性）。
    """
    if not contracts:
        return 0
    fn, label = _member(_WAREHOUSE_MODULES, "upsert_dim_metric", get_module=_warehouse_module)
    params = _param_names(fn)
    batch_names = {"contracts", "all_contracts", "metric_contracts", "catalog"}
    if any(name in batch_names for name in params):
        invoke_unfrozen(
            fn,
            candidates={
                "conn": conn,
                "connection": conn,
                "contracts": dict(contracts),
                "metric_contracts": dict(contracts),
                "catalog": dict(contracts),
            },
            fallback=(conn, dict(contracts)),
            label=label,
        )
        return len(contracts)
    for contract in contracts.values():
        invoke_unfrozen(
            fn,
            candidates={"conn": conn, "connection": conn, "contract": contract, "c": contract},
            fallback=(conn, contract),
            label=label,
        )
    return len(contracts)


def materialize_all(conn: Any, contracts: Mapping[str, Any]) -> dict[str, int] | None:
    """指标物化（契约第 8 节）。

    物化器返回逐指标的统计字典
    （`{"live", "inserted", "closed_changed", "closed_vanished", "unchanged"}`），
    本层把它归一化成 `{metric_id: 当前有效行数}` 供 CLI 展示——
    语义仍是"该指标现在有多少行有效数据"。

    模块未就绪时返回 `None`（调用方打印提示但**不**改变退出码，见 `veriself init`）；
    物化本身失败（如契约环）则原样抛出 `ContractError`。
    """
    if not contracts:
        return {}
    try:
        module = materializer()
    except GatewayUnavailable:
        return None
    fn = getattr(module, "materialize_all", None)
    if not callable(fn):
        return None

    # 合并（merge）是唯一正确的物化语义，本层不做覆盖写。
    raw = fn(conn, dict(contracts)) or {}
    if not isinstance(raw, Mapping):
        raise GatewayUnavailable("veriself.materializer", "materialize_all() 必须返回映射")

    normalized: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            normalized[str(key)] = int(value.get("live", 0) or 0)
        else:
            normalized[str(key)] = int(value or 0)
    return normalized


def generate_all(*, conn: Any = None, db_path: Any = None, seed: int | None = None) -> Any:
    """生成合成数据（`veriself.synth.generate_all()`）。"""
    module = synth()
    fn = getattr(module, "generate_all", None)
    if not callable(fn):
        raise GatewayUnavailable("veriself.synth", "缺少 generate_all()")
    return invoke_unfrozen(
        fn,
        candidates={
            "conn": conn,
            "connection": conn,
            "db_path": db_path,
            "path": db_path,
            "warehouse_path": db_path,
            "seed": seed,
            "rng_seed": seed,
        },
        fallback=tuple(value for value in (conn, db_path, seed) if value is not None),
        label="veriself.synth.generate_all",
    )

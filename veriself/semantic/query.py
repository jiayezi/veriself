"""查询对象与结果（IFACE-v1 第 3/4/7 节）。

**注入面为零**是本模块的设计目标：
LLM（或任何客户端）唯一能产出的东西是结构化查询对象，接口不接受任何 SQL 字符串；
`QueryRequest.from_json` 会对**所有字符串键与值**做 SQL 关键字/注释/分号扫描，
命中即抛 `config.QueryError`。

`filters` 的值只允许标量与标量列表（不得嵌套对象），避免把结构化数据偷渡进 SQL 生成路径。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from veriself import config

__all__ = [
    "INJECTION_KEYWORDS",
    "INJECTION_MARKERS",
    "CompiledQuery",
    "QueryRequest",
    "QueryResult",
]

#: 契约 §3 明确要求拒绝的关键字，加上加固面（DDL/DCL/语句拼接口）
INJECTION_KEYWORDS: tuple[str, ...] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "COPY",
    "EXPORT",
    "IMPORT",
    "INSTALL",
    "LOAD",
    "CALL",
    "EXECUTE",
    "EXEC",
    "MERGE",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "VACUUM",
    "SYSTEM",
    "SHELL",
)
#: 注释与语句分隔符
INJECTION_MARKERS: tuple[str, ...] = ("--", ";", "/*", "*/")

#: 词边界匹配关键字，避免误伤 `upload`、`delete_rate` 这类标识符
_KEYWORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(INJECTION_KEYWORDS) + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _scan_text(text: str, path: str) -> None:
    """对单个字符串做注入面扫描，命中抛 `QueryError`。"""
    keyword = _KEYWORD_RE.search(text)
    if keyword:
        raise config.QueryError(
            f"字段 {path} 命中 SQL 关键字 {keyword.group(0)!r}，接口不接受任何 SQL 片段：{text[:120]!r}"
        )
    for marker in INJECTION_MARKERS:
        if marker in text:
            raise config.QueryError(
                f"字段 {path} 命中注释/分号 {marker!r}，接口不接受任何 SQL 片段：{text[:120]!r}"
            )


def _scan_payload(value: Any, path: str = "$") -> None:
    """递归扫描任意 JSON 结构里的所有字符串（键与值都扫）。"""
    if isinstance(value, str):
        _scan_text(value, path)
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_payload(str(key), f"{path}.<key>")
            _scan_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_payload(item, f"{path}[{index}]")
    # 其余类型（int/float/bool/None）不含字符串，无需扫描


class QueryRequest(BaseModel):
    """LLM 唯一能产出的查询对象（契约 §3）。"""

    model_config = ConfigDict(extra="forbid")

    metrics: list[str]
    dimensions: list[str] = []
    filters: dict[str, object] = {}
    grain: str | None = None
    order_by: list[dict[str, str]] = []
    limit: int = 1000

    # ------------------------------------------------------------ 校验
    @model_validator(mode="before")
    @classmethod
    def _scan_injection(cls, data: Any) -> Any:
        """注入面扫描：先于字段校验执行，键与值都扫。"""
        if isinstance(data, dict):
            _scan_payload(data)
        return data

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, value: list[str]) -> list[str]:
        if not value:
            raise config.QueryError("metrics 必填且非空（LLM 必须声明要算哪个指标）")
        for metric_id in value:
            if not isinstance(metric_id, str) or not metric_id.strip():
                raise config.QueryError(f"metrics 里有非法项：{metric_id!r}")
        return list(value)

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise config.QueryError(f"limit 必须是整数，实际是 {type(value).__name__}")
        if value < 1:
            raise config.QueryError(f"limit 必须 >= 1，实际是 {value}")
        if value > config.DEFAULT_LIMIT:
            raise config.QueryError(f"limit 超过上限 config.DEFAULT_LIMIT={config.DEFAULT_LIMIT}，实际是 {value}")
        return value

    @field_validator("grain")
    @classmethod
    def _check_grain_type(cls, value: str | None) -> str | None:
        """只做类型检查；枚举检查在 `grain` 强制校验里做（拒绝前缀 grain_not_compatible:）。"""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise config.QueryError(f"grain 必须是非空字符串或 null，实际是 {value!r}")
        return value.strip().lower()

    @field_validator("filters")
    @classmethod
    def _check_filters(cls, value: dict[str, object]) -> dict[str, object]:
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise config.QueryError(f"filters 的键必须是非空字符串，实际是 {key!r}")
            _check_scalar("filters." + key, item)
        return dict(value)

    @field_validator("order_by")
    @classmethod
    def _check_order_by(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise config.QueryError(f"order_by[{index}] 必须是对象，实际是 {type(item).__name__}")
            unknown = set(item) - {"field", "dir"}
            if unknown:
                raise config.QueryError(f"order_by[{index}] 含未知键 {sorted(unknown)}，只允许 field/dir")
            field = item.get("field")
            if not isinstance(field, str) or not field.strip():
                raise config.QueryError(f"order_by[{index}] 缺少 field")
            direction = str(item.get("dir", "asc")).strip().lower()
            if direction not in ("asc", "desc"):
                raise config.QueryError(f"order_by[{index}].dir 必须是 asc/desc，实际是 {item.get('dir')!r}")
            item = {"field": field.strip(), "dir": direction}
            value[index] = item
        return value

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_json(cls, raw: str | dict) -> QueryRequest:
        """解析 JSON/字典并做注入面检查；任何非法输入抛 `QueryError`。"""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise config.QueryError(f"查询对象不是合法 JSON：{exc}") from exc
        elif isinstance(raw, dict):
            payload = raw
        else:
            raise config.QueryError(f"查询对象必须是 JSON 字符串或 dict，实际是 {type(raw).__name__}")
        if not isinstance(payload, dict):
            raise config.QueryError(f"查询对象必须是 JSON 对象，实际是 {type(payload).__name__}")
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            location = ".".join(str(part) for part in first.get("loc", ())) or "$"
            raise config.QueryError(f"查询对象字段非法（{location}）：{first.get('msg', exc)}") from exc


def _check_scalar(path: str, value: Any) -> None:
    """filters 的值只允许标量或标量列表。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (list, tuple, dict)):
                raise config.QueryError(f"{path}[{index}] 不允许嵌套结构（只支持标量列表）")
            _check_scalar(f"{path}[{index}]", item)
        return
    raise config.QueryError(f"{path} 的值类型不支持：{type(value).__name__}（只支持标量/标量列表）")


class CompiledQuery(BaseModel):
    """编译产物（契约 §7）：`sql` 是**已注入 RLS** 的最终 SQL。"""

    model_config = ConfigDict(extra="forbid")

    request: QueryRequest
    sql: str
    params: list[object] = []
    metric_versions: dict[str, int]
    contract_hashes: dict[str, str]
    rls_applied: list[str]
    enforced_checks: list[str]


class QueryResult(BaseModel):
    """查询结果（契约 §4）：`audit` 结构见契约第 4 节。"""

    model_config = ConfigDict(extra="forbid")

    data: list[dict]
    audit: dict

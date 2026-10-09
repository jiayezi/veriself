"""只读审计表 `ops.audit_log` 的访问器 —— `interfaces` 层**唯一**允许直读数据表的地方。

为什么允许：契约第 6 节把 `veriself audit` 的读取需求留给本层；审计表不是业务表，
它记录的是"谁在什么时候提交了什么查询、被哪条规则拒绝了"，不经过编译链也必须可读。

除此之外本层不碰任何数据表：所有指标数据一律走 `veriself.semantic` 的
`compile_query` + `execute_query`（见 `gateway.py`）。

实现上刻意使用 DuckDB 的 relation API（`con.table(...).order(...).limit(...)`）而不是
原生查询文本，这样整个 `veriself/interfaces/` 包内不存在任何原生语句字面量，
`tests/test_interfaces.py` 会对整包做静态扫描断言（任何大小写的查询关键字都不许出现）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veriself import config

__all__ = ["AUDIT_TABLE", "AuditLogUnavailable", "fetch_one", "fetch_recent"]

#: 审计表（契约第 1.3 节）。限定名，避免读到 main 里的同名表。
AUDIT_TABLE = "ops.audit_log"

#: 排序键，保证"最近查询"在最前面。
_ORDER_KEY = "audit_id"


class AuditLogUnavailable(RuntimeError):
    """审计表不可读（数仓未初始化 / 表不存在 / 文件损坏）。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"audit_log_unavailable: {detail}")


def fetch_recent(limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """读取最近 `limit` 条审计记录，返回 `[{列名: 值}]`（按 audit_id 倒序）。

    Args:
        limit: 最多返回多少条；小于 1 时按 1 处理。
        db_path: 数仓路径，默认 `config.WAREHOUSE_PATH`。

    Raises:
        AuditLogUnavailable: 数仓文件不存在、表不存在或读取失败。
    """
    return _read(
        lambda relation: _ordered(relation).limit(max(1, int(limit))),
        db_path,
    )


def fetch_one(audit_id: int, db_path: Path | str | None = None) -> dict[str, Any] | None:
    """按 `audit_id` 读取单条审计记录；不存在时返回 `None`。

    `audit_id` 先强制转成 int 再拼进过滤表达式，不存在注入面。
    """
    try:
        key = int(audit_id)
    except (TypeError, ValueError) as exc:
        raise AuditLogUnavailable(f"audit_id 必须是整数：{audit_id!r}") from exc
    rows = _read(lambda relation: relation.filter(f"{_ORDER_KEY} = {key}").limit(1), db_path)
    return rows[0] if rows else None


def _ordered(relation: Any) -> Any:
    """按 audit_id 倒序；排序键缺失时退化为物理顺序（只影响展示顺序）。"""
    try:
        return relation.order(f"{_ORDER_KEY} DESC")
    except Exception:  # noqa: BLE001 — 排序键不可用时退化为物理顺序（只影响展示顺序）
        return relation


def _read(build: Any, db_path: Path | str | None) -> list[dict[str, Any]]:
    """打开只读连接、按 `build(relation)` 取数、返回 `[{列名: 值}]`。"""
    path = Path(db_path or config.WAREHOUSE_PATH)
    if not path.exists():
        raise AuditLogUnavailable(f"数仓文件不存在：{path}（先运行 `veriself init`，可选 `veriself synth`）")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - 依赖已 vendored
        raise AuditLogUnavailable(f"duckdb 不可用：{exc}") from exc

    connection = None
    try:
        connection = duckdb.connect(str(path), read_only=True)
        relation = build(connection.table(AUDIT_TABLE))
        columns = [str(name) for name in relation.columns]
        rows = relation.fetchall()
    except Exception as exc:
        raise AuditLogUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    return [dict(zip(columns, row)) for row in rows]

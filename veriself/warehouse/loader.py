"""DuckDB 星型模型装载器（IFACE-v1 契约第 1 / 4 节，第 7 节铁律 3）。

职责边界：**只做 DDL 与装载，不实现任何契约校验逻辑**（校验属 `semantic` 模块）。
对外四个入口：

- :func:`ensure_schema`      —— 从 `warehouse/schema.sql`（唯一 DDL 来源）幂等建表。
- :func:`upsert_dim_metric`  —— 契约编译写入 `dim_metric`，哈希来自
  `veriself.contract_hash`（唯一权威实现，禁止本地复刻算法）。
- :func:`materialize_metric` —— 双时间轴写入 `fact_metric_value`：先关闭该
  `(metric_id, subject_id, date_key)` 的旧行 `valid_to`，再插入
  `valid_from=computed_at, valid_to=NULL` 的新行；这是 as-of 溯源的基础。
- :func:`write_audit`        —— 追加写 `fact_audit_log`。

`contract` 参数同时接受「YAML 解析出的 ``Mapping``」与「Pydantic 模型」两种表示。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from veriself import config
from veriself.contract_hash import contract_hash

__all__ = [
    "ensure_schema",
    "materialize_metric",
    "read_schema_sql",
    "upsert_dim_metric",
    "write_audit",
]

# dim_metric 的写入列顺序（契约第 1 节）
_DIM_METRIC_COLUMNS: tuple[str, ...] = (
    "metric_id",
    "display_name",
    "unit",
    "direction",
    "grain",
    "version",
    "contract_hash",
    "status",
)

# fact_metric_value 的写入列顺序（valid_to 由双时间轴逻辑决定，恒为 NULL）
_FACT_METRIC_VALUE_COLUMNS: tuple[str, ...] = (
    "metric_id",
    "subject_id",
    "date_key",
    "value",
    "metric_version",
    "contract_hash",
    "computed_at",
    "valid_from",
    "valid_to",
)

# fact_audit_log 的写入列顺序
_AUDIT_COLUMNS: tuple[str, ...] = (
    "audit_id",
    "queried_at",
    "actor_role",
    "request_json",
    "compiled_sql",
    "metric_versions",
    "contract_hashes",
    "rls_applied",
    "checks_passed",
    "outcome",
)

_ISO_DATE8 = re.compile(r"^\d{8}$")


# ------------------------------------------------------------------ 小工具
def read_schema_sql() -> str:
    """读取唯一 DDL 来源（`config.SCHEMA_SQL_PATH`）。"""
    return config.SCHEMA_SQL_PATH.read_text(encoding="utf-8")


def _iter_statements(sql: str) -> Iterator[str]:
    """按 `;` 切分 DDL，并丢弃整行 `--` 注释产生的空语句。"""
    kept = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    for chunk in "\n".join(kept).split(";"):
        statement = chunk.strip()
        if statement:
            yield statement


def _field(contract: Any, name: str, default: Any = None) -> Any:
    """兼容 Pydantic 模型与普通 dict 两种契约表示。"""
    if isinstance(contract, Mapping):
        return contract.get(name, default)
    return getattr(contract, name, default)


def _contract_mapping(contract: Any) -> dict[str, Any]:
    """把契约归一成 `dict`，用于计算 `contract_hash`（口径：原始 YAML dict）。"""
    if isinstance(contract, Mapping):
        return dict(contract)
    dump = getattr(contract, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return {str(key): value for key, value in dumped.items()}
        return {str(key): value for key, value in vars(dumped).items()}
    return dict(vars(contract))


def _resolve_hash(contract: Any) -> str:
    """契约自带 `contract_hash` 时直接采用（保证与 semantic 审计头一致），否则现算。"""
    existing = _field(contract, "contract_hash", None)
    if existing:
        return str(existing)
    return contract_hash(_contract_mapping(contract))


def _iter_contracts(contracts: Any) -> Iterator[tuple[str | None, Any]]:
    """接受 `{metric_id: contract}` 映射或契约可迭代对象。"""
    if isinstance(contracts, Mapping):
        for key, contract in contracts.items():
            yield (str(key), contract)
    else:
        for contract in contracts:
            yield (None, contract)


def _utcnow() -> _dt.datetime:
    """DuckDB `TIMESTAMP` 是 naïve 类型，统一写 naive-UTC，避免时区偏移歧义。"""
    return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)


def _coerce_ts(value: Any) -> _dt.datetime | None:
    """把 datetime / date / ISO 字符串归一成 naive-UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(_dt.UTC).replace(tzinfo=None)
        return value
    if isinstance(value, _dt.date):
        # 日期没有钟点，按 UTC 午夜理解，再去掉 tzinfo：DuckDB TIMESTAMP 只收 naïve。
        return _dt.datetime(value.year, value.month, value.day, tzinfo=_dt.UTC).replace(tzinfo=None)
    if isinstance(value, str):
        parsed = _dt.datetime.fromisoformat(value.strip())
        return _coerce_ts(parsed)
    raise TypeError(f"无法解析时间戳: {value!r}")


def _coerce_date_key(value: Any) -> int:
    """把 date_key 归一成 yyyymmdd 整数（接受 int / date / 'yyyymmdd' / 'yyyy-mm-dd'）。"""
    if isinstance(value, bool):
        raise TypeError("date_key 不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, _dt.datetime):
        return int(value.strftime("%Y%m%d"))
    if isinstance(value, _dt.date):
        return int(value.strftime("%Y%m%d"))
    if isinstance(value, str):
        text = value.strip()
        if _ISO_DATE8.match(text):
            return int(text)
        return int(_dt.date.fromisoformat(text[:10]).strftime("%Y%m%d"))
    raise TypeError(f"无法解析 date_key: {value!r}")


def _coerce_value(value: Any) -> float | None:
    """数值归一；NULL / NaN / Inf 一律返回 None（契约第 8 节：禁止写入 NaN/Inf）。"""
    if value is None:
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _encode(payload: Any, default: str = "{}") -> str:
    """审计列编码：字符串原样，其余 JSON 序列化（dict/list），None 用默认值。"""
    if payload is None:
        return default
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ------------------------------------------------------------------ DDL
def ensure_schema(conn: Any) -> None:
    """幂等建表：执行 `warehouse/schema.sql`（全部语句均为 `IF NOT EXISTS`）。

    ⚠️ **没有迁移机制**（有意为之）。DuckDB 不支持
    `ALTER TABLE ... DROP CONSTRAINT` / `ADD CONSTRAINT ... CHECK`，约束变更只能重建表；
    而 `CREATE TABLE IF NOT EXISTS` 遇到已存在的表会**静默跳过**，因此
    **早于当前 DDL 建库的存量库不会自动获得新约束**。
    本项目 v0.1 未发布、无历史用户，升级方式是**重建库**：

        veriself synth && veriself init

    若将来需要支持就地升级，再引入 schema 版本号 + 通用重建迁移
    （不要再逐个累积 `_migrate_*` 函数）。
    """
    for statement in _iter_statements(read_schema_sql()):
        conn.execute(statement)


# ------------------------------------------------------------------ 契约 -> dim_metric
def upsert_dim_metric(conn: Any, contracts: Any) -> int:
    """把指标契约编译写入 `dim_metric`，返回写入（含更新）行数。

    - 主键是 `(metric_id, version)`。同一指标的当前版与历史版可以共存；
      冲突时只更新该版本的描述列，不会改掉另一个版本。
    - 不删除调用方这次没带来的版本：历史行留在表里，直到显式重建库。
    - `contract_hash` 走 `veriself.contract_hash`；契约自带该字段时以契约为准，
      保证 `dim_metric` 与审计头的哈希同源。
    - 本函数不读目录。`veriself init` 传入的是当前契约加上 `metrics/history/`
      （见 `semantic.contract.load_definition_versions`）。只传当前版时，
      已有的历史行保持不动。
    """
    rows: list[tuple[Any, ...]] = []
    for key, contract in _iter_contracts(contracts):
        metric_id = _field(contract, "metric_id", key)
        if not metric_id:
            raise ValueError("契约缺少 metric_id，无法写入 dim_metric")
        version = _field(contract, "version", 1)
        if version is None:
            raise ValueError(f"契约 {metric_id} 缺少 version，无法写入 dim_metric")
        rows.append(
            (
                str(metric_id),
                _field(contract, "display_name"),
                _field(contract, "unit"),
                _field(contract, "direction"),
                _field(contract, "grain"),
                int(version),
                _resolve_hash(contract),
                _field(contract, "status"),
            )
        )
    if not rows:
        return 0

    placeholders = ", ".join("?" * len(_DIM_METRIC_COLUMNS))
    update_columns = [
        column for column in _DIM_METRIC_COLUMNS if column not in ("metric_id", "version")
    ]
    sql = (
        f"INSERT INTO dim_metric ({', '.join(_DIM_METRIC_COLUMNS)}) VALUES ({placeholders}) "
        "ON CONFLICT (metric_id, version) DO UPDATE SET "
        + ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    )
    conn.executemany(sql, rows)
    return len(rows)


# ------------------------------------------------------------------ 双时间轴物化
def _closure_groups(
    rows: Sequence[tuple[str, int, float, _dt.datetime]],
) -> dict[tuple[str, _dt.datetime], list[int]]:
    """按 `(subject_id, computed_at)` 分组收集需要关闭旧版本的 date_key。"""
    groups: dict[tuple[str, _dt.datetime], list[int]] = {}
    for subject_id, date_key, _value, computed_at in rows:
        groups.setdefault((subject_id, computed_at), []).append(date_key)
    return groups


def materialize_metric(
    conn: Any,
    metric_id: str,
    values: Sequence[Mapping[str, Any]],
    contract: Any,
) -> int:
    """把某指标的一批日粒度取值双时间轴写入 `fact_metric_value`，返回落库行数。

    `values` 每项必须含 `subject_id` / `date_key` / `value`，可选 `computed_at`：

    1. 先对每个 `(metric_id, subject_id, date_key)` 关闭旧行：`valid_to = computed_at`
       （仅关闭当前仍有效的 `valid_to IS NULL` 行）；
    2. 再插入 `valid_from = computed_at`、`valid_to = NULL` 的新行，
       `metric_version` / `contract_hash` 取自 `contract`。

    同一 `(subject_id, date_key)` 重复出现时后者覆盖前者；`value` 为 NULL/NaN/Inf 的行
    直接跳过（契约第 8 节：无数据不写行，禁止 NaN/Inf）。返回实际落库行数。
    """
    version = int(_field(contract, "version", 1) or 1)
    fingerprint = _resolve_hash(contract)
    default_ts = _utcnow()

    latest: dict[tuple[str, int], tuple[str, int, float, _dt.datetime]] = {}
    for item in values:
        value = _coerce_value(item.get("value"))
        if value is None:
            continue
        subject_id = str(item["subject_id"])
        date_key = _coerce_date_key(item["date_key"])
        computed_at = _coerce_ts(item.get("computed_at")) or default_ts
        latest[(subject_id, date_key)] = (subject_id, date_key, value, computed_at)

    rows = list(latest.values())
    if not rows:
        return 0

    for (subject_id, computed_at), date_keys in _closure_groups(rows).items():
        placeholders = ", ".join("?" * len(set(date_keys)))
        conn.execute(
            "UPDATE fact_metric_value SET valid_to = ? "
            "WHERE metric_id = ? AND subject_id = ? AND valid_to IS NULL "
            f"AND date_key IN ({placeholders})",
            [computed_at, metric_id, subject_id, *sorted(set(date_keys))],
        )

    placeholders = ", ".join("?" * len(_FACT_METRIC_VALUE_COLUMNS))
    sql = (
        f"INSERT INTO fact_metric_value ({', '.join(_FACT_METRIC_VALUE_COLUMNS)}) "
        f"VALUES ({placeholders}) "
        "ON CONFLICT (metric_id, subject_id, date_key, valid_from) DO UPDATE SET "
        "value = excluded.value, metric_version = excluded.metric_version, "
        "contract_hash = excluded.contract_hash, computed_at = excluded.computed_at, "
        "valid_to = excluded.valid_to"
    )
    conn.executemany(
        sql,
        [
            (metric_id, subject_id, date_key, value, version, fingerprint, computed_at,
             computed_at, None)
            for subject_id, date_key, value, computed_at in rows
        ],
    )
    return len(rows)


# ------------------------------------------------------------------ 审计日志
def write_audit(conn: Any, record: Mapping[str, Any]) -> None:
    """向 `fact_audit_log` 追加一条审计记录（`audit_id` 自增，单语句原子分配）。

    `record` 的键对应契约第 1 节的列；`metric_versions` / `contract_hashes` /
    `rls_applied` / `checks_passed` 允许传 dict / list（自动 JSON 编码）。
    `checks_passed` 未给时兼容审计头的 `enforced_checks` 命名。
    `queried_at` 缺省取当前 UTC；`actor_role` 与 `outcome` 为 NOT NULL，缺失即抛
    `ValueError`（审计记录不允许静默降级）。
    """
    payload = dict(record)
    audit_id = payload.pop("audit_id", None)
    actor_role = payload.get("actor_role")
    outcome = payload.get("outcome")
    if actor_role is None:
        raise ValueError("审计记录缺少 actor_role")
    if outcome is None:
        raise ValueError("审计记录缺少 outcome")

    values = (
        _coerce_ts(payload.get("queried_at")) or _utcnow(),
        str(actor_role),
        _encode(payload.get("request_json")),
        payload.get("compiled_sql"),
        _encode(payload.get("metric_versions")),
        _encode(payload.get("contract_hashes")),
        _encode(payload.get("rls_applied"), default="[]"),
        _encode(payload.get("checks_passed", payload.get("enforced_checks")), default="[]"),
        str(outcome),
    )
    placeholders = ", ".join("?" * len(values))
    if audit_id is None:  # 单条 INSERT ... SELECT，避免"先查 max 再插入"的竞态
        conn.execute(
            f"INSERT INTO fact_audit_log ({', '.join(_AUDIT_COLUMNS)}) "
            f"SELECT coalesce(max(audit_id), 0) + 1, {placeholders} FROM fact_audit_log",
            list(values),
        )
    else:
        conn.execute(
            f"INSERT INTO fact_audit_log ({', '.join(_AUDIT_COLUMNS)}) "
            f"VALUES (?, {placeholders})",
            [int(audit_id), *values],
        )

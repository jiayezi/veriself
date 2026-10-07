"""双时间轴合并（materializer 内部）。

`build_merge_sql` 生成三步合并语句；`build_insert_sql` 是
"求值 + 追加落库"的简单形式（测试与一次性装载用）。哈希一律来自
`veriself.contract_hash`（见 `_hash_for`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from veriself.materializer import contracts as _contracts
from veriself.materializer import sqlbuild as _sqlbuild

__all__ = [
    "VALUE_ABS_FLOOR",
    "VALUE_REL_TOL",
    "build_insert_sql",
    "build_merge_sql",
]

# 值等价容差：DOUBLE 运算的求和/平均顺序不稳定，末位可能漂移
# （`avg(...) OVER (PARTITION BY ...)` 会在 `3599.53` 与 `3599.5300000000007` 之间摆动）。
# 这类差异低于任何实际精度需求，但若不忽略，每次重算都会为它留一行"变化"历史——
# 于是"无变化检测"被浮点噪声打穿。判据用相对容差 `rel_tol`，另配极小绝对下限防零值。
VALUE_REL_TOL = 1e-9
VALUE_ABS_FLOOR = 1e-12


def _values_equal(left: str, right: str) -> str:
    """生成"两个 DOUBLE 是否等价"的 SQL 布尔表达式（含容差，且对 NULL 安全）。"""
    return (
        f"({left} IS NULL AND {right} IS NULL)"
        f" OR ({left} IS NOT NULL AND {right} IS NOT NULL AND ("
        f"abs({left} - {right}) <= {VALUE_ABS_FLOOR}"
        f" + {VALUE_REL_TOL} * greatest(abs({left}), abs({right}))))"
    )


def _hash_for(contract: Any) -> str:
    """从契约对象取哈希；取不到时按权威实现现算，保证与 dim_metric 一致。

    兜底的哈希输入必须是**原始契约 dict**（与 `semantic.contract` 的加载路径同源）。
    不能用 `model_dump()`：它会补默认值，并把加载器附加的 `contract_hash` /
    `source_file` 也算进去，现算出的哈希与 `dim_metric` 不一致。

    这里按属性名鸭子类型取 `raw`，不 import `semantic`——依赖方向是
    `interfaces → semantic → warehouse/materializer → config`，materializer 不得反向依赖。
    """
    existing = _contracts._field(contract, "contract_hash", None)
    if existing:
        return str(existing)
    from veriself.contract_hash import contract_hash

    if isinstance(contract, Mapping):
        return contract_hash(contract)
    # pydantic 契约：`raw` 是原始 YAML dict（private `_raw` 的只读副本）
    payload = getattr(contract, "raw", None)
    if not isinstance(payload, Mapping):
        payload = _model_to_mapping(contract)
    return contract_hash(payload)


def _model_to_mapping(model: Any) -> dict[str, Any]:
    """把 pydantic 模型或普通对象转成字段映射（键统一为 str）。

    不能写成 `dict(dump())`：`builtins.callable` 的签名是 `TypeIs[(...) -> object]`，
    正向分支会把 `dump` 收窄成"返回 object 的可调用对象"，`dict(object)` 匹配不上任何
    重载，类型检查器会按 `Iterable[list[bytes]]` 那条兜底重载报错。显式 `isinstance`
    判断既让类型具体化，也把"`model_dump()` 不返回映射"从 TypeError 变成优雅降级。
    """
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return {str(key): value for key, value in dumped.items()}
        return {str(key): value for key, value in vars(dumped).items()}
    return dict(vars(model))


def build_merge_sql(
    contract: Any,
    contracts: Mapping[str, Any],
    *,
    batch_ts: str,
    models: Mapping[str, Any] | None = None,
) -> tuple[list[tuple[str, list[Any]]], str]:
    """生成本批次的**三步合并**语句。

    为什么不用单条 `MERGE`：

    1. `MERGE ... USING (WITH ... SELECT ...)` —— DuckDB 不支持 `WITH` 出现在 `USING` 子查询里
       （`Parser Error`）。所以先把本批次结果落到临时表。
    2. 单条 `MERGE` 带 `WHEN MATCHED AND <值不同> THEN UPDATE` + `WHEN NOT MATCHED THEN INSERT`
       —— DuckDB 在 `WHEN MATCHED AND` 条件不成立时**不会继续尝试后续分支**，
       于是"值变化"的键只被关闭、新值永远插不进去（有效行数反而少了 1）。
       这是与标准 SQL MERGE 语义的差异，不足以承载本需求。

    因此拆成三步，每步语义单一、可单独测试：

    | 步骤 | 作用 |
    | --- | --- |
    | ① 关闭变化键 | `value` 或 `contract_hash` 与临时表**不同**的有效行 → `valid_to = batch_ts` |
    | ② 插入新行 | 临时表里"当前没有有效行"的键全部插入（含①刚关闭的） |
    | ③ 关闭消失键 | 本批次未产出的键 → `valid_to = batch_ts`（防"陈旧但被当作当前事实"的僵尸行） |

    三个步骤合起来恰好覆盖三种情形，且无变化时①③影响 0 行、②插入 0 行 → **纯重算零写放大**。

    返回 `(statements, temp_table)`；`statements` 需按顺序执行。
    `valid_from` 统一用**批次时间戳**，使同批次行可整体识别、as-of 可按批次切分。
    """
    metric_id = _contracts._metric_id(contract)
    version = int(_contracts._field(contract, "version", 1) or 1)
    temp = f"_batch_{_sqlbuild._alias(metric_id)}_{version}"
    ctes = _sqlbuild._compute_ctes(contract, contracts, models=models)

    create = (
        f"CREATE OR REPLACE TEMP TABLE {temp} AS\n"
        f"{ctes}\n"
        "SELECT subject_id, date_key, daily_value AS value\n"
        "FROM rolled\n"
        "WHERE daily_value IS NOT NULL AND isfinite(daily_value)"
    )

    scope = (
        f"tgt.metric_id = '{metric_id}'"
        f" AND tgt.metric_version = {version}"
        " AND tgt.valid_to IS NULL"
    )

    # ① 值或口径变化 → 关闭旧行（值比较带浮点容差，见 `_values_equal`）
    _same_value = _values_equal("src.value", "tgt.value")
    close_changed = (
        f"UPDATE fact_metric_value AS tgt SET valid_to = CAST(? AS TIMESTAMP)\n"
        f"WHERE {scope}\n"
        "  AND EXISTS (\n"
        f"      SELECT 1 FROM {temp} AS src\n"
        "      WHERE src.subject_id = tgt.subject_id AND src.date_key = tgt.date_key\n"
        f"        AND (NOT ({_same_value})\n"
        f"             OR '{_hash_for(contract)}' IS DISTINCT FROM tgt.contract_hash)\n"
        "  )"
    )

    # ② 插入本批次中"当前没有有效行"的键
    insert_new = f"""INSERT INTO fact_metric_value
    (metric_id, subject_id, date_key, value, metric_version, contract_hash,
     computed_at, valid_from, valid_to)
SELECT '{metric_id}', src.subject_id, src.date_key, src.value, {version},
       '{_hash_for(contract)}',
       CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), NULL
FROM {temp} AS src
WHERE NOT EXISTS (
    SELECT 1 FROM fact_metric_value AS tgt
    WHERE tgt.metric_id = '{metric_id}'
      AND tgt.metric_version = {version}
      AND tgt.subject_id = src.subject_id
      AND tgt.date_key = src.date_key
      AND tgt.valid_to IS NULL
)"""

    # ③ 本批次未产出的键 → 关闭（否则成为僵尸有效行）
    close_vanished = (
        f"UPDATE fact_metric_value AS tgt SET valid_to = CAST(? AS TIMESTAMP)\n"
        f"WHERE {scope}\n"
        "  AND NOT EXISTS (\n"
        f"      SELECT 1 FROM {temp} AS src\n"
        "      WHERE src.subject_id = tgt.subject_id AND src.date_key = tgt.date_key\n"
        "  )"
    )

    statements = [
        (create, []),
        (close_changed, [batch_ts]),
        (insert_new, [batch_ts, batch_ts]),
        (close_vanished, [batch_ts]),
    ]
    return statements, temp


def build_insert_sql(
    contract: Any,
    contracts: Mapping[str, Any],
    models: Mapping[str, Any] | None = None,
) -> str:
    """生成"求值 + 落库"的 INSERT ... SELECT（追加语义，供测试与一次性装载使用）。

    日常物化走 `build_merge_sql`：本函数不做无变化检测，重复调用会累积历史行。
    """
    metric_id = _contracts._metric_id(contract)
    version = int(_contracts._field(contract, "version", 1) or 1)
    contract_hash = _hash_for(contract)
    compute = _sqlbuild._compute_ctes(contract, contracts, models=models)
    return f"""INSERT INTO fact_metric_value
    (metric_id, subject_id, date_key, value, metric_version, contract_hash, computed_at, valid_from, valid_to)
{compute}
SELECT
    '{metric_id}',
    subject_id,
    date_key,
    daily_value,
    {version},
    '{contract_hash}',
    now(),
    now(),
    NULL
FROM rolled
WHERE daily_value IS NOT NULL AND isfinite(daily_value)
"""


def _batch_timestamp(explicit: Any = None) -> str:
    """批次时间戳：同一批全部行使用同一个值（便于按批次切分 as-of）。"""
    if explicit is not None:
        return str(explicit)
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).replace(tzinfo=None).isoformat(sep=" ")


def _live_key_set(conn: Any, metric_id: str, version: int) -> set[tuple[str, int]]:
    rows = conn.execute(
        "SELECT subject_id, date_key FROM fact_metric_value"
        " WHERE metric_id = ? AND metric_version = ? AND valid_to IS NULL",
        [metric_id, version],
    ).fetchall()
    return {(str(r[0]), int(r[1])) for r in rows}


def _history_count(conn: Any, metric_id: str, version: int) -> int:
    rows = conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = ? AND metric_version = ? AND valid_to IS NOT NULL",
        [metric_id, version],
    ).fetchall()
    return int(rows[0][0]) if rows else 0

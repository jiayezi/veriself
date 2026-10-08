"""编排：一键生成合成数据（Parquet 中间产物 + ``data/warehouse.duckdb`` 前 6 张契约表）。

流程：``dim_date`` -> 外生窗口 -> 潜在结构 -> 观测通道 -> 维表/事件 -> 写 Parquet -> 装载 DuckDB。

DDL 归属：契约规定 ``veriself/warehouse/schema.sql`` 是唯一 DDL 来源。本模块
**优先执行该文件**；若它不存在（或执行失败），才用 DuckDB 从 DataFrame 推断列类型建表
（``CREATE TABLE t AS SELECT * FROM df LIMIT 0``），并给出警告。装载只影响契约前 6 张表：
先 ``DELETE`` 再 ``INSERT ... BY NAME``，不 DROP、不动其他表（如 ``dim_metric``）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

from veriself import config
from veriself.synth import dimensions, events, latent, observations

__all__ = [
    "CONTRACT_TABLES",
    "INTERMEDIATE_TABLES",
    "generate_all",
]

#: 本模块负责写入的 6 张契约表（顺序即装载顺序）
CONTRACT_TABLES: tuple[str, ...] = (
    "dim_date",
    "dim_subject",
    "dim_source",
    "dim_context",
    "fact_observation",
    "fact_event",
)

#: 只写 Parquet 的中间产物
INTERMEDIATE_TABLES: tuple[str, ...] = ("latent_daily", "obs_daily", "obs_intraday")

#: 各表的确定性排序键（保证 Parquet 逐字节可复现、DuckDB 行序稳定）
ORDER_KEYS: dict[str, tuple[str, ...]] = {
    "dim_date": ("date_key",),
    "dim_subject": ("subject_sk",),
    "dim_source": ("source_id",),
    "dim_context": ("context_sk",),
    "fact_observation": ("observed_at", "channel"),
    "fact_event": ("event_id",),
    "latent_daily": ("date_key",),
    "obs_daily": ("date_key", "channel"),
    "obs_intraday": ("observed_at", "channel"),
}

_CONNECT_ATTEMPTS: int = 12
_CONNECT_BACKOFF_S: float = 0.5


def _fact_observation(daily: pd.DataFrame, intraday: pd.DataFrame) -> pd.DataFrame:
    """合并日粒度观测与需要进入事实表的日内观测，并分配 ``observation_id``。

    Args:
        daily: 全部日粒度通道。
        intraday: 日内通道（含仅作中间产物的通道）。

    Returns:
        列顺序与契约一致的 ``fact_observation``。
    """

    promoted = intraday.loc[intraday["channel"].isin(observations.PROMOTED_INTRADAY_CHANNELS)]
    frame = pd.concat([daily, promoted], ignore_index=True)
    frame = frame.sort_values(["observed_at", "channel"], kind="stable").reset_index(drop=True)
    frame.insert(0, "observation_id", range(1, frame.shape[0] + 1))
    return frame.loc[
        :,
        [
            "observation_id",
            "subject_id",
            "observed_at",
            "date_key",
            "channel",
            "value",
            "source_id",
            "recorded_at",
        ],
    ]


def build_tables(dates: pd.DatetimeIndex) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """构建 6 张契约表与 3 张中间产物表（纯内存，不落盘）。

    Args:
        dates: 连续日粒度日期索引。

    Returns:
        ``(contract_tables, intermediate_tables)``。
    """

    dim_date = dimensions.build_dim_date(dates)
    windows = events.build_windows(dates)
    latent_daily = latent.build_latent_daily(dates, dim_date, windows)
    daily_obs, intraday_obs = observations.build_observations(latent_daily)

    contract = {
        "dim_date": dim_date,
        "dim_subject": dimensions.build_dim_subject(windows.plan_start),
        "dim_source": dimensions.build_dim_source(),
        "dim_context": dimensions.build_dim_context(latent_daily, dim_date),
        "fact_observation": _fact_observation(daily_obs, intraday_obs),
        "fact_event": events.build_fact_event(dates, latent_daily, windows),
    }
    intermediate = {
        "latent_daily": latent_daily,
        "obs_daily": daily_obs,
        "obs_intraday": intraday_obs,
    }
    return contract, intermediate


def _quote_identifier(name: str) -> str:
    """双引号包裹标识符。"""

    return '"' + name.replace('"', '""') + '"'


def _quote_literal(text: str) -> str:
    """单引号包裹字符串字面量。"""

    return "'" + text.replace("'", "''") + "'"


def write_parquet(
    frames: dict[str, pd.DataFrame], synth_dir: Path, connection: duckdb.DuckDBPyConnection
) -> dict[str, Path]:
    """把表写成 Parquet（用 DuckDB 自带 writer，避免依赖 pyarrow）。

    Args:
        frames: 表名 -> DataFrame。
        synth_dir: 输出目录。
        connection: 用于 COPY 的连接（建议内存库）。

    Returns:
        表名 -> 文件路径。
    """

    synth_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, frame in frames.items():
        path = synth_dir / f"{name}.parquet"
        view = f"_synth_{name}"
        columns = ", ".join(_quote_identifier(col) for col in frame.columns)
        order = ", ".join(_quote_identifier(col) for col in ORDER_KEYS[name])
        connection.register(view, frame)
        try:
            connection.execute(
                f"COPY (SELECT {columns} FROM {_quote_identifier(view)} ORDER BY {order}) "
                f"TO {_quote_literal(path.as_posix())} (FORMAT PARQUET)"
            )
        finally:
            connection.unregister(view)
        written[name] = path
    return written


def _connect_with_retry(db_path: Path) -> duckdb.DuckDBPyConnection:
    """带重试地打开 DuckDB（并行 teammate 可能正持有写锁）。"""

    last_error: Exception | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return duckdb.connect(str(db_path))
        except duckdb.Error as error:  # pragma: no cover - 取决于并行进程
            last_error = error
            time.sleep(_CONNECT_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"无法打开 DuckDB：{db_path}（最后错误：{last_error}）")


def _apply_schema_sql(connection: duckdb.DuckDBPyConnection, schema_path: Path) -> list[str]:
    """执行唯一 DDL 来源 ``schema.sql``（存在时）。

    Args:
        connection: DuckDB 连接。
        schema_path: ``config.SCHEMA_SQL_PATH``。

    Returns:
        警告信息列表（正常为空）。
    """

    if not schema_path.exists():
        return [f"未找到 DDL 来源 {schema_path}，改为按 DataFrame 推断类型建表"]
    script = schema_path.read_text(encoding="utf-8")
    try:
        connection.execute(script)
        return []
    except duckdb.Error as error:
        warnings_list = [f"执行 {schema_path.name} 失败，逐条重试：{error}"]
        for statement in script.split(";"):
            stripped = "\n".join(
                line for line in statement.splitlines() if not line.strip().startswith("--")
            ).strip()
            if not stripped:
                continue
            try:
                connection.execute(stripped)
            except duckdb.Error as inner:  # pragma: no cover - 取决于他人 DDL
                warnings_list.append(f"跳过语句：{inner}")
        return warnings_list


def _ensure_tables(
    connection: duckdb.DuckDBPyConnection, frames: dict[str, pd.DataFrame]
) -> list[str]:
    """确保目标表存在；缺失时按 DataFrame 推断类型建空表。

    Returns:
        警告信息列表。
    """

    messages: list[str] = []
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
        ).fetchall()
    }
    for name, frame in frames.items():
        if name in existing:
            continue
        view = f"_synth_ddl_{name}"
        connection.register(view, frame)
        try:
            connection.execute(
                f"CREATE TABLE {_quote_identifier(name)} AS "
                f"SELECT * FROM {_quote_identifier(view)} LIMIT 0"
            )
        finally:
            connection.unregister(view)
        messages.append(f"{name} 不在 DDL 中，已按其 DataFrame 列类型创建")
    return messages


def load_into_duckdb(
    db_path: Path, frames: dict[str, pd.DataFrame]
) -> tuple[dict[str, int], list[str]]:
    """把表装入 DuckDB：只在契约表上 ``DELETE`` + ``INSERT ... BY NAME``。

    Args:
        db_path: ``data/warehouse.duckdb``。
        frames: 6 张契约表。

    Returns:
        ``(各表行数, 警告信息)``。
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    connection = _connect_with_retry(db_path)
    try:
        messages.extend(_apply_schema_sql(connection, config.SCHEMA_SQL_PATH))
        messages.extend(_ensure_tables(connection, frames))
        counts: dict[str, int] = {}
        # ⚠️ 刻意**不**用一个事务包住全部 6 张表（曾如此，实测在持久化库上会失败）：
        # DuckDB 里同一显式事务内 `DELETE` 不会让**上一会话持久化的唯一索引**条目失效，
        # 紧接着 `INSERT` 同一个键就撞上"已删除的键"：
        #     session1: CREATE TABLE + CREATE UNIQUE INDEX + INSERT → close
        #     session2: BEGIN; DELETE; INSERT; COMMIT  → Duplicate key（失败）
        #     session3: DELETE; INSERT（均自动提交）    → 成功
        # `dim_subject`（`ux_dim_subject_version`）与 `fact_observation`
        # （`ux_fact_observation_id` / `_natural_key`）都有唯一索引，所以那种写法会让
        # **第二次跑 `veriself synth` 直接失败**（见 AGENTS.md「易错点」）。
        # 代价：失去跨表原子性——每张表各自 DELETE→INSERT 自动提交。这是有意取舍：
        # synth 产出的是可重建的派生数据，且失败是响亮的（不会静默半写，重跑即可修复）。
        for name in CONTRACT_TABLES:
            frame = frames[name]
            view = f"_synth_load_{name}"
            connection.register(view, frame)
            try:
                connection.execute(f"DELETE FROM {_quote_identifier(name)}")
                connection.execute(
                    f"INSERT INTO {_quote_identifier(name)} BY NAME "
                    f"SELECT * FROM {_quote_identifier(view)}"
                )
            finally:
                connection.unregister(view)
            row = connection.execute(
                f"SELECT count(*) FROM {_quote_identifier(name)}"
            ).fetchone()
            counts[name] = int(row[0]) if row is not None else 0
        return counts, messages
    finally:
        connection.close()


def generate_all(
    db_path: Path | None = None, synth_dir: Path | None = None
) -> dict[str, int]:
    """一键生成全部合成数据（确定性：同一种子两次运行结果逐元素相等）。

    Args:
        db_path: DuckDB 路径，默认 :data:`veriself.config.WAREHOUSE_PATH`。
        synth_dir: Parquet 中间产物目录，默认 ``data/synth``。

    Returns:
        行数统计：6 张契约表 + 3 张中间产物表（``latent_daily`` / ``obs_daily`` /
        ``obs_intraday``，后者只落 Parquet）。
    """

    db_target = Path(db_path) if db_path is not None else config.WAREHOUSE_PATH
    parquet_dir = Path(synth_dir) if synth_dir is not None else config.DATA_DIR / "synth"
    dates = pd.date_range(config.SYNTH_START_DATE, config.SYNTH_END_DATE, freq="D")

    contract, intermediate = build_tables(dates)

    with duckdb.connect() as memory:
        write_parquet({**contract, **intermediate}, parquet_dir, memory)

    counts, messages = load_into_duckdb(db_target, contract)
    counts.update({name: int(frame.shape[0]) for name, frame in intermediate.items()})
    for message in messages:
        print(f"[synth] 警告：{message}", file=sys.stderr)
    return counts

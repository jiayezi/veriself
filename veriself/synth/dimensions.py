"""维表生成：``dim_date`` / ``dim_subject``（SCD2）/ ``dim_source`` / ``dim_context``。

列集合严格等于 ``docs/00-接口契约.md`` 第 1 节，不多不少（下游 JOIN 依赖列名）。
``dim_context`` 只登记"出现过的上下文状态"（出差 × 生病 × 地点），因为契约里的
``fact_observation`` / ``fact_event`` 都没有 context 外键，无法做按日关联。
"""

from __future__ import annotations

from datetime import date as Date

import numpy as np
import pandas as pd

from veriself import config

__all__ = [
    "CN_HOLIDAY_RANGES",
    "build_dim_context",
    "build_dim_date",
    "build_dim_source",
    "build_dim_subject",
    "winter_factor",
]

# CN 法定节假日近似（契约允许近似；不含调休上班日）
CN_HOLIDAY_RANGES: dict[int, tuple[tuple[str, str], ...]] = {
    2024: (
        ("2024-01-01", "2024-01-01"),   # 元旦
        ("2024-02-10", "2024-02-17"),   # 春节
        ("2024-04-04", "2024-04-06"),   # 清明
        ("2024-05-01", "2024-05-05"),   # 劳动节
        ("2024-06-10", "2024-06-10"),   # 端午
        ("2024-09-15", "2024-09-17"),   # 中秋
        ("2024-10-01", "2024-10-07"),   # 国庆
    ),
    2025: (
        ("2025-01-01", "2025-01-01"),
        ("2025-01-28", "2025-02-04"),
        ("2025-04-04", "2025-04-06"),
        ("2025-05-01", "2025-05-05"),
        ("2025-05-31", "2025-06-02"),
        ("2025-10-01", "2025-10-08"),   # 国庆 + 中秋（10-06）
    ),
    2026: (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-22"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
    ),
}

#: 主体基础属性（SCD2 第 1 版的取值）
BASE_WEIGHT_KG: float = 78.5
#: 减重计划结束后的登记体重（触发 SCD2 第 2 版）
PLAN_WEIGHT_KG: float = 72.0
BIRTH_DATE: Date = Date(1990, 5, 17)
TIMEZONE: str = "Asia/Shanghai"

#: 数据源（source_id 与契约示例一致）
SOURCE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("wearable", "Wearable Band", "high"),
    ("phone", "Smartphone", "medium"),
    ("bank", "Bank Feed", "high"),
    ("llm_client", "LLM Client", "low"),
)


def winter_factor(dates: pd.DatetimeIndex) -> np.ndarray:
    """季节性外生项：深冬(1 月中)为 +1，盛夏(7 月中)为 -1。

    Args:
        dates: 日期索引。

    Returns:
        与 ``dates`` 等长的取值区间 ``[-1, 1]`` 的季节因子。
    """

    day_of_year = pd.DatetimeIndex(dates).dayofyear.to_numpy(dtype=float)
    return np.cos(2.0 * np.pi * (day_of_year - 15.0) / 365.25)


def _holiday_date_set(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """把年份区间表展开成覆盖 ``dates`` 年份的节假日 DatetimeIndex。"""

    years = sorted({int(y) for y in dates.year.unique()})
    days: list[pd.Timestamp] = []
    for year in years:
        for start, end in CN_HOLIDAY_RANGES.get(year, ()):
            days.extend(pd.date_range(start, end, freq="D").tolist())
    return pd.DatetimeIndex(sorted(days))


def build_dim_date(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """构造 ``dim_date``。

    Args:
        dates: 连续日粒度日期索引（升序）。

    Returns:
        列与契约一致的日期维表，按 ``date_key`` 升序。
    """

    dates = pd.DatetimeIndex(dates)
    iso = dates.isocalendar()
    holidays = _holiday_date_set(dates)
    date_keys = dates.year * 10_000 + dates.month * 100 + dates.day

    frame = pd.DataFrame(
        {
            "date_key": date_keys.astype("int32"),
            "date": [ts.date() for ts in dates],
            "year": dates.year.astype("int32"),
            "quarter": dates.quarter.astype("int32"),
            "month": dates.month.astype("int32"),
            "week": iso["week"].to_numpy(dtype="int32"),
            "day_of_week": (dates.dayofweek + 1).astype("int32"),
            "weekday_name": pd.Series(dates.day_name(), dtype="str"),
            "is_weekend": (dates.dayofweek >= 5),
            "is_holiday": dates.normalize().isin(holidays),
        }
    )
    return frame.sort_values("date_key").reset_index(drop=True)


def build_dim_source() -> pd.DataFrame:
    """构造 ``dim_source``（4 个数据源）。"""

    return pd.DataFrame(
        {
            "source_id": pd.Series([row[0] for row in SOURCE_ROWS], dtype="str"),
            "display_name": pd.Series([row[1] for row in SOURCE_ROWS], dtype="str"),
            "reliability_tier": pd.Series([row[2] for row in SOURCE_ROWS], dtype="str"),
        }
    )


def build_dim_subject(plan_start: pd.Timestamp) -> pd.DataFrame:
    """构造 ``dim_subject``：减重计划开始时新增第 2 个版本（SCD2）。

    双时间轴语义：``valid_from`` / ``valid_to`` 是业务有效期，``recorded_at`` 是入账时间
    （第 2 版比生效日晚 1 天入账）。

    Args:
        plan_start: 减重计划开始日（第 2 版的 ``valid_from``）。

    Returns:
        两行版本记录；``is_current`` 恰好一行为真，当前行 ``valid_to`` 为 NULL。
    """

    start = pd.Timestamp(config.SYNTH_START_DATE)
    recorded_v2 = pd.Timestamp(plan_start) + pd.Timedelta(days=1)
    frame = pd.DataFrame(
        {
            "subject_sk": np.array([1, 2], dtype="int64"),
            "subject_id": pd.Series([config.SUBJECT_ID] * 2, dtype="str"),
            "name": pd.Series([config.SUBJECT_NAME] * 2, dtype="str"),
            "birth_date": [BIRTH_DATE, BIRTH_DATE],
            "sleep_need_h": np.array(
                [config.SUBJECT_SLEEP_NEED_H] * 2, dtype="float64"
            ),
            "base_weight_kg": np.array([BASE_WEIGHT_KG, PLAN_WEIGHT_KG], dtype="float64"),
            "timezone": pd.Series([TIMEZONE] * 2, dtype="str"),
            "valid_from": pd.to_datetime([start, pd.Timestamp(plan_start)]),
            "valid_to": pd.to_datetime([pd.Timestamp(plan_start), pd.NaT]),
            "is_current": np.array([False, True], dtype=bool),
            "version": np.array([1, 2], dtype="int32"),
            "recorded_at": pd.to_datetime([start, recorded_v2]),
        }
    )
    return frame


def build_dim_context(latent_daily: pd.DataFrame, dim_date: pd.DataFrame) -> pd.DataFrame:
    """构造 ``dim_context``：登记数据里真实出现过的 (出差 × 生病 × 地点) 组合。

    Args:
        latent_daily: 含 ``is_travel`` / ``is_illness`` 的日粒度潜在结构表。
        dim_date: ``dim_date``（取 ``is_weekend``）。

    Returns:
        去重后的上下文维表，``context_sk`` 从 1 开始、按状态排序，结果确定。
    """

    is_travel = latent_daily["is_travel"].to_numpy(dtype=bool)
    is_illness = latent_daily["is_illness"].to_numpy(dtype=bool)
    is_weekend = dim_date["is_weekend"].to_numpy(dtype=bool)

    location = np.where(
        is_travel,
        "other",
        np.where(is_weekend | is_illness, "home", "office"),
    )
    states = (
        pd.DataFrame(
            {
                "is_travel": is_travel,
                "is_illness": is_illness,
                "location_type": pd.Series(location, dtype="str"),
            }
        )
        .drop_duplicates()
        .sort_values(["is_travel", "is_illness", "location_type"], kind="stable")
        .reset_index(drop=True)
    )
    states.insert(0, "context_sk", np.arange(1, states.shape[0] + 1, dtype="int64"))
    context_ids = [
        f"CTX_T{int(t)}_I{int(i)}_{loc.upper()}"
        for t, i, loc in zip(
            states["is_travel"].to_numpy(),
            states["is_illness"].to_numpy(),
            states["location_type"].to_numpy(),
        )
    ]
    states.insert(1, "context_id", pd.Series(context_ids, dtype="str").to_numpy())
    return states.loc[
        :, ["context_sk", "context_id", "is_travel", "is_illness", "location_type"]
    ]

"""观测通道：把潜在真值转成带测量噪声的 ``fact_observation`` 行。

产物分两层：

* **日粒度通道**（进入 ``fact_observation``）：``sleep_hours``、``sleep_start_hour``、
  ``deep_sleep_hours``、``resting_hr``、``hrv``、``steps``、``exercise_minutes``、
  ``focus_score``、``screen_minutes``、``mood_score``；通道名严格取自
  ``docs/01-指标清单.md``，一天一行。
* **日内通道**：15 分钟 ``heart_rate``（进入 ``fact_observation`` —— 契约把
  ``heart_rate`` 列为示例通道，且没有任何指标会按日累加它），以及小时级
  ``steps_intraday`` / ``screen_minutes_intraday``（**只写 Parquet 中间产物、不进入
  ``fact_observation``**，避免与日粒度 ``steps`` / ``screen_minutes`` 行重复计数）。
  日内两条通道按日求和精确等于对应的日粒度通道值。

时间戳约定：``observed_at`` 的日历日 == ``date_key``；睡眠相关通道打在醒来时刻，
其余通道打在当日晚间固定时刻，``recorded_at = observed_at + 确定性同步延迟``。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from veriself import config
from veriself.synth.rng import child_rng

__all__ = [
    "DAILY_CHANNEL_NAMES",
    "INTRADAY_CHANNEL_NAMES",
    "PROMOTED_INTRADAY_CHANNELS",
    "build_observations",
]

OBSERVATION_COLUMNS: tuple[str, ...] = (
    "subject_id",
    "observed_at",
    "date_key",
    "channel",
    "value",
    "source_id",
    "recorded_at",
)

#: 进入 ``fact_observation`` 的日内通道
PROMOTED_INTRADAY_CHANNELS: tuple[str, ...] = ("heart_rate",)
#: 只作为中间产物保留的日内通道
INTERMEDIATE_INTRADAY_CHANNELS: tuple[str, ...] = ("steps_intraday", "screen_minutes_intraday")

SLOTS_PER_DAY: int = 96
SLOT_MINUTES: int = 15
HOURS_PER_DAY: int = 24

#: 数据源 -> ``recorded_at`` 的同步延迟区间（分钟）
SYNC_LAG_MINUTES: dict[str, tuple[int, int]] = {
    "wearable": (10, 95),
    "phone": (0, 20),
    "bank": (60, 1500),
    "llm_client": (0, 2),
}


@dataclass(frozen=True)
class DailyChannelSpec:
    """日粒度通道的定义。

    Attributes:
        name: ``fact_observation.channel``。
        source_id: 数据源。
        truth_column: 潜在真值列名。
        clock: ``"wake"`` / ``"wake+N"``（醒来后 N 分钟）或 ``"HH:MM"``（当日固定时刻）。
        noise_sd: 测量噪声标准差；``noise_kind="mul"`` 时为相对标准差。
        noise_kind: ``"add"`` 或 ``"mul"``。
        decimals: 保留小数位。
        low: 取值下限。
        high: 取值上限。
    """

    name: str
    source_id: str
    truth_column: str
    clock: str
    noise_sd: float
    noise_kind: str
    decimals: int
    low: float
    high: float


DAILY_CHANNELS: tuple[DailyChannelSpec, ...] = (
    DailyChannelSpec("sleep_hours", "wearable", "sleep_hours_true", "wake", 0.16, "add", 2, 2.5, 12.0),
    DailyChannelSpec("sleep_start_hour", "wearable", "sleep_start_hour_true", "wake", 0.10, "add", 2, 20.0, 28.0),
    DailyChannelSpec("deep_sleep_hours", "wearable", "deep_sleep_hours_true", "wake", 0.07, "add", 2, 0.30, 3.0),
    DailyChannelSpec("resting_hr", "wearable", "resting_hr_true", "wake+15", 0.90, "add", 1, 40.0, 90.0),
    DailyChannelSpec("hrv", "wearable", "hrv_true", "wake+20", 2.60, "add", 1, 15.0, 130.0),
    DailyChannelSpec("focus_score", "phone", "latent_focus", "20:30", 2.20, "add", 0, 0.0, 100.0),
    DailyChannelSpec("mood_score", "phone", "latent_mood", "21:30", 2.00, "add", 0, 0.0, 100.0),
    DailyChannelSpec("steps", "wearable", "steps_true", "22:00", 0.030, "mul", 0, 0.0, 60000.0),
    DailyChannelSpec("exercise_minutes", "wearable", "exercise_minutes_true", "22:05", 2.00, "add", 0, 0.0, 300.0),
    DailyChannelSpec("screen_minutes", "phone", "screen_minutes_true", "22:10", 12.00, "add", 0, 0.0, 900.0),
)

DAILY_CHANNEL_NAMES: tuple[str, ...] = tuple(spec.name for spec in DAILY_CHANNELS)
INTRADAY_CHANNEL_NAMES: tuple[str, ...] = PROMOTED_INTRADAY_CHANNELS + INTERMEDIATE_INTRADAY_CHANNELS

#: 小时权重：步数（通勤/午间/傍晚高峰）
STEP_HOUR_WEIGHTS: np.ndarray = np.array(
    [0.05, 0.02, 0.02, 0.02, 0.03, 0.20, 0.55, 1.20, 1.55, 1.10, 0.95, 1.05,
     1.35, 1.20, 0.95, 0.90, 1.05, 1.45, 1.60, 1.10, 0.75, 0.55, 0.35, 0.15],
    dtype=float,
)
#: 小时权重：屏幕时间（集中在 19:00-23:00）
SCREEN_HOUR_WEIGHTS: np.ndarray = np.array(
    [0.10, 0.03, 0.02, 0.02, 0.03, 0.10, 0.25, 0.45, 0.80, 1.10, 1.20, 1.15,
     1.05, 1.10, 1.15, 1.05, 1.10, 1.35, 1.75, 2.10, 2.20, 1.85, 1.10, 0.40],
    dtype=float,
)
#: 心率昼夜基线（插值锚点：小时 -> bpm 偏移）
CIRCADIAN_HOURS: np.ndarray = np.array([0.0, 4.0, 7.0, 9.0, 12.0, 14.0, 17.0, 20.0, 22.0, 23.99])
CIRCADIAN_BPM: np.ndarray = np.array([-5.0, -6.0, 3.0, 11.0, 13.0, 9.0, 10.0, 7.0, 3.0, -2.0])


def _clock_to_timestamp(latent_daily: pd.DataFrame, clock: str) -> pd.DatetimeIndex:
    """把 ``clock`` 说明解析成逐日时间戳。

    Args:
        latent_daily: 含 ``wake_at`` 的潜在结构表。
        clock: ``"wake"`` / ``"wake+15"`` / ``"HH:MM"``。

    Returns:
        与 ``latent_daily`` 等长的时间戳索引。
    """

    wake_at = pd.DatetimeIndex(latent_daily["wake_at"])
    if clock.startswith("wake"):
        if "+" in clock:
            offset = int(clock.split("+", 1)[1])
        else:
            offset = 0
        return pd.DatetimeIndex(wake_at + pd.Timedelta(minutes=offset))
    hour, minute = (int(part) for part in clock.split(":"))
    midnight = pd.DatetimeIndex(pd.to_datetime(latent_daily["date"]))
    return pd.DatetimeIndex(midnight + pd.Timedelta(hours=hour, minutes=minute))


def _observed_values(spec: DailyChannelSpec, truth: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """叠加测量噪声并裁剪到合理量程。"""

    if spec.noise_kind == "mul":
        values = truth * (1.0 + spec.noise_sd * rng.standard_normal(truth.shape[0]))
    else:
        values = truth + spec.noise_sd * rng.standard_normal(truth.shape[0])
    return np.round(np.clip(values, spec.low, spec.high), spec.decimals)


def _recorded_at(observed_at: pd.DatetimeIndex, source_id: str, tag: str) -> pd.DatetimeIndex:
    """确定性同步延迟：``observed_at + U(lag_lo, lag_hi)`` 分钟。"""

    lo, hi = SYNC_LAG_MINUTES[source_id]
    rng = child_rng(f"obs.lag.{tag}")
    lag = rng.integers(lo, max(hi, lo + 1), size=observed_at.shape[0]).astype("int64")
    return pd.DatetimeIndex(observed_at + pd.to_timedelta(lag, unit="m"))


def _finalize(
    channel: str,
    values: np.ndarray,
    observed_at: pd.DatetimeIndex,
    source_id: str,
) -> pd.DataFrame:
    """组装单通道长表（列与 ``fact_observation`` 对齐）。"""

    frame = pd.DataFrame(
        {
            "subject_id": pd.Series([config.SUBJECT_ID] * values.shape[0], dtype="str"),
            "observed_at": observed_at,
            "date_key": (
                observed_at.year * 10_000 + observed_at.month * 100 + observed_at.day
            ).astype("int32"),
            "channel": pd.Series([channel] * values.shape[0], dtype="str"),
            "value": values.astype("float64"),
            "source_id": pd.Series([source_id] * values.shape[0], dtype="str"),
            "recorded_at": _recorded_at(observed_at, source_id, channel),
        }
    )
    return frame.loc[:, list(OBSERVATION_COLUMNS)]


def build_daily_observations(latent_daily: pd.DataFrame) -> pd.DataFrame:
    """生成全部日粒度观测通道（长表）。

    Args:
        latent_daily: :func:`veriself.synth.latent.build_latent_daily` 的输出。

    Returns:
        列见 :data:`OBSERVATION_COLUMNS`，按 ``(date_key, channel)`` 排序。
    """

    frames: list[pd.DataFrame] = []
    observed_sleep: np.ndarray | None = None
    for spec in DAILY_CHANNELS:
        rng = child_rng(f"obs.{spec.name}")
        truth = latent_daily[spec.truth_column].to_numpy(dtype=float)
        values = _observed_values(spec, truth, rng)
        if spec.name == "sleep_hours":
            observed_sleep = values
        elif spec.name == "deep_sleep_hours" and observed_sleep is not None:
            values = np.round(np.minimum(values, 0.45 * observed_sleep), 2)
        observed_at = _clock_to_timestamp(latent_daily, spec.clock)
        frames.append(_finalize(spec.name, values, observed_at, spec.source_id))
    daily = pd.concat(frames, ignore_index=True)
    return daily.sort_values(["date_key", "channel"], kind="stable").reset_index(drop=True)


def _heart_rate(latent_daily: pd.DataFrame) -> pd.DataFrame:
    """15 分钟心率：昼夜基线 + 清醒/睡眠差异 + 运动尖峰 + AR(1) 噪声。"""

    rng = child_rng("obs.heart_rate")
    n = latent_daily.shape[0]
    slot_hour = (np.arange(SLOTS_PER_DAY, dtype=float) + 0.5) * (SLOT_MINUTES / 60.0)
    circadian = np.interp(slot_hour, CIRCADIAN_HOURS, CIRCADIAN_BPM)

    wake_share = STEP_HOUR_WEIGHTS / STEP_HOUR_WEIGHTS.sum()
    activity = 14.0 * (wake_share / wake_share.mean())
    activity_slots = np.repeat(activity, SLOTS_PER_DAY // HOURS_PER_DAY)

    resting = latent_daily["resting_hr_true"].to_numpy(dtype=float)
    onset = latent_daily["sleep_start_hour_true"].to_numpy(dtype=float)
    sleep_hours = latent_daily["sleep_hours_true"].to_numpy(dtype=float)
    is_illness = latent_daily["is_illness"].to_numpy(dtype=bool)
    is_travel = latent_daily["is_travel"].to_numpy(dtype=bool)
    exercise = latent_daily["exercise_minutes_true"].to_numpy(dtype=float)

    asleep = (slot_hour[None, :] >= onset[:, None]) | (
        slot_hour[None, :] <= (onset + sleep_hours - 24.0)[:, None]
    )

    innovations = rng.normal(0.0, 3.2 * np.sqrt(1.0 - 0.55**2), size=(n, SLOTS_PER_DAY))
    noise = np.empty_like(innovations)
    noise[:, 0] = innovations[:, 0]
    for slot in range(1, SLOTS_PER_DAY):
        noise[:, slot] = 0.55 * noise[:, slot - 1] + innovations[:, slot]

    heart_rate = (
        resting[:, None]
        + circadian[None, :]
        + activity_slots[None, :]
        - 4.5 * asleep
        + 3.0 * is_illness[:, None]
        + 2.0 * is_travel[:, None]
        + noise
    )

    workout_slots = np.ceil(exercise / SLOT_MINUTES).astype(int)
    workout_start = 18 * (SLOTS_PER_DAY // HOURS_PER_DAY)
    for day in np.flatnonzero(workout_slots > 0):
        length = min(int(workout_slots[day]), 6)
        end = min(workout_start + length, SLOTS_PER_DAY)
        heart_rate[day, workout_start:end] += 46.0 + rng.normal(0.0, 4.0, end - workout_start)

    heart_rate = np.round(np.clip(heart_rate, 38.0, 190.0), 1)
    midnight = pd.to_datetime(latent_daily["date"]).to_numpy()
    base = np.repeat(midnight, SLOTS_PER_DAY)
    offsets = np.tile(np.arange(SLOTS_PER_DAY) * SLOT_MINUTES, n).astype("timedelta64[m]")
    observed_at = pd.DatetimeIndex(base + offsets)
    return _finalize("heart_rate", heart_rate.reshape(-1), observed_at, "wearable")


def _daily_to_hourly(
    daily: np.ndarray,
    weights: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """把日粒度值分解到 24 小时：按权重加噪后归一化，最后一小时吸收舍入误差以保证按日和相等。"""

    n = daily.shape[0]
    jitter = 1.0 + 0.22 * rng.standard_normal((n, HOURS_PER_DAY))
    raw = np.clip(weights[None, :] * jitter, 1e-6, None)
    share = raw / raw.sum(axis=1, keepdims=True)
    hourly = daily[:, None] * share
    hourly[:, -1] = daily - hourly[:, :-1].sum(axis=1)
    return hourly


def _intraday_decomposition(
    latent_daily: pd.DataFrame,
    daily_long: pd.DataFrame,
    channel: str,
    hourly_channel: str,
    weights: np.ndarray,
    source_id: str,
) -> pd.DataFrame:
    """生成 "由日粒度值分解出的小时序列"（中间产物，不进入 ``fact_observation``）。"""

    rng = child_rng(f"obs.{hourly_channel}")
    daily_values = (
        daily_long.loc[daily_long["channel"] == channel, ["date_key", "value"]]
        .sort_values("date_key", kind="stable")["value"]
        .to_numpy(dtype=float)
    )
    hourly = _daily_to_hourly(daily_values, weights, rng)
    midnight = pd.to_datetime(latent_daily["date"]).to_numpy()
    base = np.repeat(midnight, HOURS_PER_DAY)
    offsets = np.tile(np.arange(HOURS_PER_DAY), midnight.shape[0]).astype("timedelta64[h]")
    observed_at = pd.DatetimeIndex(base + offsets)
    return _finalize(hourly_channel, hourly.reshape(-1), observed_at, source_id)


def build_observations(latent_daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成日粒度观测与日内观测。

    Args:
        latent_daily: :func:`veriself.synth.latent.build_latent_daily` 的输出。

    Returns:
        ``(daily_long, intraday_long)``；前者全部进入 ``fact_observation``，
        后者的 ``heart_rate`` 进入 ``fact_observation``，``steps_intraday`` /
        ``screen_minutes_intraday`` 仅作中间产物。
    """

    daily = build_daily_observations(latent_daily)
    parts = [
        _heart_rate(latent_daily),
        _intraday_decomposition(
            latent_daily, daily, "steps", "steps_intraday", STEP_HOUR_WEIGHTS, "wearable"
        ),
        _intraday_decomposition(
            latent_daily,
            daily,
            "screen_minutes",
            "screen_minutes_intraday",
            SCREEN_HOUR_WEIGHTS,
            "phone",
        ),
    ]
    intraday = pd.concat(parts, ignore_index=True)
    intraday = intraday.sort_values(["observed_at", "channel"], kind="stable").reset_index(drop=True)
    return daily, intraday

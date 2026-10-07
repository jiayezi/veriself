"""潜在结构生成：睡眠、恢复、专注、情绪、压力、活动与体重。

日期约定（**下游指标按此对齐**）
--------------------------------
* 日期 ``d`` 的睡眠 = 结束于 ``d`` 日早晨的那一觉（``d-1`` 晚入睡、``d`` 日晨醒），
  因此 ``sleep_hours_true[d]`` 就是 ``d`` 日的"前夜睡眠"，``focus`` 的当日值由它驱动。
* ``sleep_start_hour_true`` 用 ``[20, 28)`` 表示入睡钟点，``>24`` 表示次日凌晨
  （``24.5 = 00:30``），因此可以直接求均值/标准差，无需圆周统计。

植入效应清单（保真度测试只应检出这些）
--------------------------------------
1. 前夜睡眠 -> 当日专注 ``latent_focus``；并通过睡眠债与恢复度产生**滞后效应**；
2. 前夜睡眠 + 当日运动 -> ``latent_recovery``（AR(1) 状态变量）；
3. 恢复度 -> 静息心率（负向）、HRV（正向）；
4. 季节性（冬季）-> 情绪（负向）、步数（负向）、静息心率（正向）、睡眠时长（正向）；
5. 周内效应 -> 情绪（周末高、周一低）、睡眠时长（周末高）、步数（周末高）、
   屏幕时间（工作日高）；
6. 工作事件 -> ``latent_stress`` 脉冲（突发事件、冲刺周首日、出差/生病首日）；
7. 出差/生病 -> 睡眠、专注、情绪、步数、运动的系统性变化；
8. 减重计划（2025-03-03..2025-09-28）-> 体重下降、步数与运动增加、屏幕减少，
   并触发 ``dim_subject`` 的 SCD2 第 2 版；
9. 会议数 -> 专注（负向）、压力（正向）、屏幕时间（正向）。

**未植入**：消费/笔记与任何潜在变量之间没有因果关系；步数与屏幕时间之间、
静息心率与消费之间也没有共享驱动（测试会验证这些相关性不显著）。

**已知的非因果相关**：周内效应同时作用在入睡时间与屏幕时间上，因此
``sleep_start_hour`` 与 ``screen_minutes`` 会呈现由共同日历结构导致的负相关
（约 -0.5），它不是植入的因果链路，只是两个已植入周内效应的共同结果。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from veriself import config
from veriself.synth.dimensions import BASE_WEIGHT_KG, PLAN_WEIGHT_KG, winter_factor
from veriself.synth.events import ExogenousWindows
from veriself.synth.rng import ar1, child_rng, starts_of

__all__ = ["build_latent_daily"]

NEED_H: float = config.SUBJECT_SLEEP_NEED_H
SLEEP_MIN_H: float = 3.6
SLEEP_MAX_H: float = 10.4
ONSET_MIN_H: float = 20.6
ONSET_MAX_H: float = 26.8
ONSET_BASE_H: float = 23.15

#: 睡眠债留存系数与专注度惩罚（产生"睡眠不足会连续拖累几天"的滞后效应）
SLEEP_DEBT_DECAY: float = 0.62
FOCUS_SLEEP_BETA: float = 2.8
FOCUS_DEBT_BETA: float = 0.7
#: 恢复度 AR(1) 系数
RECOVERY_DECAY: float = 0.55
#: 压力脉冲留存系数（半衰期约 2.8 天）
STRESS_DECAY: float = 0.78


def _weekend_night(dow: np.ndarray) -> np.ndarray:
    """周末夜（周五、周六入睡 -> 周六、周日晨醒）。"""

    return np.isin(dow, (5, 6))


def _sleep(
    dow: np.ndarray,
    winter: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """生成前夜睡眠时长与入睡钟点。

    Args:
        dow: 星期（0=周一）。
        winter: 季节因子。
        is_travel: 出差掩码。
        is_illness: 生病掩码。

    Returns:
        ``(sleep_hours, sleep_start_hour)``，入睡钟点用 ``[20, 28)`` 表示。
    """

    n = dow.shape[0]
    onset_rng = child_rng("latent.sleep_onset")
    onset_dev = ar1(onset_rng.normal(0.0, 0.40, n), phi=0.30)
    onset = (
        ONSET_BASE_H
        + 0.85 * _weekend_night(dow)
        + 0.12 * winter
        + 1.10 * is_travel
        - 0.15 * is_illness
        + onset_dev
    )
    onset = np.clip(onset, ONSET_MIN_H, ONSET_MAX_H)

    sleep_rng = child_rng("latent.sleep")
    deviation = ar1(sleep_rng.normal(0.0, 0.78, n), phi=0.36)
    short_night = sleep_rng.random(n) < 0.055
    short_penalty = np.where(short_night, -(1.1 + 1.4 * sleep_rng.random(n)), 0.0)
    sleep_hours = (
        NEED_H
        + deviation
        + 0.55 * _weekend_night(dow)
        - 0.30 * (dow == 6)
        + 0.20 * winter
        - 0.85 * is_travel
        + 0.65 * is_illness
        - 0.30 * (onset - ONSET_BASE_H)
        + short_penalty
    )
    sleep_hours = np.clip(sleep_hours, SLEEP_MIN_H, SLEEP_MAX_H)
    return sleep_hours, onset


def _deep_sleep(
    sleep_hours: np.ndarray, onset: np.ndarray, is_illness: np.ndarray, is_plan: np.ndarray
) -> np.ndarray:
    """深睡时长（小时）：约为睡眠时长的 20%，早睡与减重计划略增，生病略减。"""

    rng = child_rng("latent.deep_sleep")
    deep = (
        0.205 * sleep_hours
        + 0.045 * (ONSET_BASE_H - onset)
        + 0.06 * is_plan
        - 0.10 * is_illness
        + rng.normal(0.0, 0.13, sleep_hours.shape[0])
    )
    return np.clip(np.minimum(deep, 0.36 * sleep_hours), 0.45, 2.4)


def _meetings(
    dow: np.ndarray, is_crunch: np.ndarray, is_incident: np.ndarray, is_illness: np.ndarray
) -> np.ndarray:
    """当日会议数：工作日为主，冲刺周与突发事件显著增加，生病时大幅减少。"""

    rng = child_rng("latent.meetings")
    workday = dow < 5
    lam = np.where(workday, 3.2, 0.15) + 0.9 * is_crunch + 1.4 * is_incident
    lam = np.where(is_illness, 0.25 * lam, lam)
    return np.minimum(rng.poisson(lam), 12)


def _stress(
    windows: ExogenousWindows, meetings: np.ndarray, dow: np.ndarray
) -> np.ndarray:
    """压力：工作事件驱动的脉冲过程（AR(1) 衰减 + 周内基线）。

    Args:
        windows: 外生窗口。
        meetings: 当日会议数。
        dow: 星期。

    Returns:
        压力水位（均值约 24，脉冲可到 45+）。
    """

    rng = child_rng("latent.stress")
    n = meetings.shape[0]
    jumps = np.zeros(n, dtype=float)
    np.add.at(jumps, starts_of(windows.incident), 6.0)
    np.add.at(jumps, starts_of(windows.crunch), 3.2)
    np.add.at(jumps, starts_of(windows.travel), 7.5)
    np.add.at(jumps, starts_of(windows.illness), 5.0)
    jumps = jumps + 0.9 * np.maximum(meetings - 5, 0)

    weekend = dow >= 5
    weekly = np.where(dow == 0, 2.4, np.where(dow == 4, -1.6, np.where(weekend, -3.2, 0.0)))
    innovations = jumps + weekly + rng.normal(0.0, 1.7, n)
    return 24.0 + ar1(innovations, phi=STRESS_DECAY)


def _activity(
    dow: np.ndarray,
    winter: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
    is_plan: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """步数、运动分钟与"是否运动日"。

    Returns:
        ``(steps, exercise_minutes, workout_day)``。
    """

    rng = child_rng("latent.activity")
    n = dow.shape[0]
    weekend = dow >= 5
    workout_day = rng.random(n) < np.where(weekend, 0.45, 0.33)
    duration = 28.0 + 40.0 * rng.random(n)
    exercise = np.where(
        workout_day,
        duration * (1.0 + 0.25 * is_plan) * np.where(is_travel, 0.45, 1.0),
        0.0,
    )
    exercise = np.where(is_illness, 0.0, exercise)
    exercise = np.round(exercise, 1)

    steps = (
        7400.0
        + 1150.0 * weekend
        - 1250.0 * winter
        + 2200.0 * is_travel
        + 2600.0 * workout_day
        + 600.0 * is_plan
        + ar1(rng.normal(0.0, 1500.0, n), phi=0.25)
        + rng.normal(0.0, 900.0, n)
    )
    steps = np.where(is_illness, 0.5 * steps, steps)
    steps = np.clip(steps, 300.0, 45000.0)
    return steps, exercise, workout_day


def _recovery(
    sleep_hours: np.ndarray,
    exercise: np.ndarray,
    stress: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
) -> np.ndarray:
    """恢复度（AR(1)，约 50±5）：受前夜睡眠、当日运动、压力、出差与生病影响。"""

    rng = child_rng("latent.recovery")
    innovations = (
        2.45 * (sleep_hours - NEED_H)
        + 0.055 * np.minimum(exercise, 90.0)
        - 0.30 * (stress - 24.0)
        - 4.2 * is_illness
        - 1.8 * is_travel
        + rng.normal(0.0, 2.8, sleep_hours.shape[0])
    )
    return 50.0 + ar1(innovations, phi=RECOVERY_DECAY)


def _cardio(
    recovery: np.ndarray,
    winter: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
    is_plan: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """静息心率（bpm，越低越好）与 HRV（ms，越高越好）。"""

    rng = child_rng("latent.cardio")
    n = recovery.shape[0]
    resting_hr = (
        55.0
        - 0.22 * (recovery - 50.0)
        + 0.75 * winter
        + 1.10 * is_illness
        - 0.30 * is_plan
        + rng.normal(0.0, 1.0, n)
    )
    hrv = (
        62.0
        + 0.78 * (recovery - 50.0)
        - 3.20 * is_illness
        - 1.40 * is_travel
        + 1.60 * is_plan
        + rng.normal(0.0, 3.2, n)
    )
    return np.round(resting_hr, 1), np.round(hrv, 1)


def _focus(
    sleep_hours: np.ndarray,
    sleep_debt: np.ndarray,
    recovery: np.ndarray,
    meetings: np.ndarray,
    dow: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
) -> np.ndarray:
    """专注度真值：前夜睡眠（核心效应）+ 恢复度 - 睡眠债 - 会议干扰 - 出差/生病。"""

    rng = child_rng("latent.focus")
    weekend = dow >= 5
    focus = (
        50.0
        + FOCUS_SLEEP_BETA * (sleep_hours - NEED_H)
        + 0.25 * (recovery - 50.0)
        - FOCUS_DEBT_BETA * sleep_debt
        - 0.50 * meetings
        + 0.90 * weekend
        - 7.00 * is_illness
        - 2.00 * is_travel
        + rng.normal(0.0, 4.0, sleep_hours.shape[0])
    )
    return np.clip(focus, 3.0, 99.0)


def _mood(
    sleep_hours: np.ndarray,
    recovery: np.ndarray,
    stress: np.ndarray,
    winter: np.ndarray,
    dow: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
    is_plan: np.ndarray,
) -> np.ndarray:
    """情绪真值：季节项（冬季低）+ 周内效应（周末高、周一低）+ 睡眠/恢复/压力。"""

    rng = child_rng("latent.mood")
    n = sleep_hours.shape[0]
    mood = (
        62.0
        - 4.50 * winter
        + 2.60 * (dow >= 5)
        - 1.80 * (dow == 0)
        + 1.50 * (sleep_hours - NEED_H)
        + 0.12 * (recovery - 50.0)
        + 1.40 * is_plan
        - 6.50 * is_illness
        - 2.20 * is_travel
        - 0.10 * (stress - 24.0)
        + ar1(rng.normal(0.0, 3.0, n), phi=0.42)
    )
    return np.clip(mood, 5.0, 99.0)


def _screen(
    meetings: np.ndarray,
    stress: np.ndarray,
    dow: np.ndarray,
    is_travel: np.ndarray,
    is_illness: np.ndarray,
    is_plan: np.ndarray,
) -> np.ndarray:
    """屏幕时间真值：工作日与会议多则高，出差/减重期低，生病略高。"""

    rng = child_rng("latent.screen")
    screen = (
        235.0
        + 95.0 * (dow < 5)
        + 8.5 * meetings
        + 1.50 * (stress - 24.0)
        - 45.0 * is_travel
        + 35.0 * is_illness
        - 25.0 * is_plan
        + rng.normal(0.0, 38.0, meetings.shape[0])
    )
    return np.clip(screen, 25.0, 700.0)


def _weight(dates: pd.DatetimeIndex, windows: ExogenousWindows) -> np.ndarray:
    """体重曲线：计划前平稳，计划期内线性下降 6.5kg，计划后小幅回升。"""

    rng = child_rng("latent.weight")
    n = dates.shape[0]
    noise = ar1(rng.normal(0.0, 0.22, n), phi=0.85)
    plan_days = float((windows.plan_end - windows.plan_start).days)
    progress = np.clip((dates - windows.plan_start).days.to_numpy(dtype=float) / plan_days, 0.0, 1.0)
    after = np.clip(
        (dates - windows.plan_end).days.to_numpy(dtype=float)
        / max(float((dates[-1] - windows.plan_end).days), 1.0),
        0.0,
        1.0,
    )
    during = BASE_WEIGHT_KG - (BASE_WEIGHT_KG - PLAN_WEIGHT_KG) * progress
    weight = np.where(
        dates < windows.plan_start,
        BASE_WEIGHT_KG + noise,
        np.where(
            windows.weight_plan,
            during + 0.5 * noise,
            PLAN_WEIGHT_KG + 1.1 * after + 0.6 * noise,
        ),
    )
    return np.round(weight, 2)


def _wake_at(dates: pd.DatetimeIndex, onset: np.ndarray, sleep_hours: np.ndarray) -> pd.DatetimeIndex:
    """醒来时刻：``d-1`` 晚入睡 + 睡眠时长，落点始终在 ``d`` 日 00:00..13:30。"""

    wake_offset = pd.to_timedelta(onset + sleep_hours, unit="h")
    return pd.DatetimeIndex((dates - pd.Timedelta(days=1) + wake_offset).floor("min"))


def build_latent_daily(
    dates: pd.DatetimeIndex, dim_date: pd.DataFrame, windows: ExogenousWindows
) -> pd.DataFrame:
    """生成日粒度潜在结构与真值通道。

    Args:
        dates: 连续日粒度日期索引。
        dim_date: ``dim_date``（取 ``is_weekend``）。
        windows: 外生窗口。

    Returns:
        每行一天，含潜在变量（``latent_*``）、真值通道（``*_true``）与外生标志。
    """

    dates = pd.DatetimeIndex(dates)
    dow = dates.dayofweek.to_numpy(dtype=int)
    winter = winter_factor(dates)
    is_travel = np.asarray(windows.travel, dtype=bool)
    is_illness = np.asarray(windows.illness, dtype=bool)
    is_crunch = np.asarray(windows.crunch, dtype=bool)
    is_incident = np.asarray(windows.incident, dtype=bool)
    is_plan = np.asarray(windows.weight_plan, dtype=bool)

    sleep_hours, onset = _sleep(dow, winter, is_travel, is_illness)
    deficit = np.maximum(NEED_H - sleep_hours, 0.0)
    sleep_debt = ar1(deficit, phi=SLEEP_DEBT_DECAY)
    deep_sleep = _deep_sleep(sleep_hours, onset, is_illness, is_plan)
    meetings = _meetings(dow, is_crunch, is_incident, is_illness)
    stress = _stress(windows, meetings, dow)
    steps, exercise, workout_day = _activity(dow, winter, is_travel, is_illness, is_plan)
    recovery = _recovery(sleep_hours, exercise, stress, is_travel, is_illness)
    resting_hr, hrv = _cardio(recovery, winter, is_travel, is_illness, is_plan)
    focus = _focus(sleep_hours, sleep_debt, recovery, meetings, dow, is_travel, is_illness)
    mood = _mood(sleep_hours, recovery, stress, winter, dow, is_travel, is_illness, is_plan)
    screen = _screen(meetings, stress, dow, is_travel, is_illness, is_plan)
    weight = _weight(dates, windows)

    date_keys = dates.year * 10_000 + dates.month * 100 + dates.day
    frame = pd.DataFrame(
        {
            "date_key": date_keys.astype("int32"),
            "date": [ts.date() for ts in dates],
            "day_of_week": (dow + 1).astype("int32"),
            "is_weekend": dow >= 5,
            "winter_factor": np.round(winter, 4),
            "is_travel": is_travel,
            "is_illness": is_illness,
            "is_crunch": is_crunch,
            "is_incident": is_incident,
            "is_weight_plan": is_plan,
            "wake_at": _wake_at(dates, onset, sleep_hours),
            "meetings_count": meetings.astype("int32"),
            "sleep_hours_true": np.round(sleep_hours, 3),
            "sleep_start_hour_true": np.round(onset, 3),
            "deep_sleep_hours_true": np.round(deep_sleep, 3),
            "sleep_deficit_true": np.round(deficit, 3),
            "sleep_debt_latent": np.round(sleep_debt, 3),
            "resting_hr_true": resting_hr,
            "hrv_true": hrv,
            "steps_true": np.round(steps, 1),
            "exercise_minutes_true": exercise,
            "workout_day": workout_day,
            "screen_minutes_true": np.round(screen, 1),
            "latent_recovery": np.round(recovery, 3),
            "latent_focus": np.round(focus, 3),
            "latent_mood": np.round(mood, 3),
            "latent_stress": np.round(stress, 3),
            "weight_kg": weight,
        }
    )
    return frame

"""事件与外生冲击：出差/生病/项目冲刺窗口，以及 ``fact_event``（交易、笔记、AI 对话、运动）。

外生窗口先于潜在变量生成（潜在变量受它们驱动）；事件行同时受潜在变量
（运动日 -> ``workout`` 事件）与外生窗口（出差日 -> 更多差旅消费）影响。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from veriself import config
from veriself.synth.dimensions import winter_factor
from veriself.synth.rng import child_rng

__all__ = [
    "DISCRETIONARY_CATEGORIES",
    "PLAN_END",
    "PLAN_START",
    "ExogenousWindows",
    "build_fact_event",
    "build_windows",
]

#: 减重计划窗口（固定常量，便于测试断言 SCD2 第 2 版的生效日）
PLAN_START: pd.Timestamp = pd.Timestamp("2025-03-03")
PLAN_END: pd.Timestamp = pd.Timestamp("2025-09-28")

TRANSACTION_CATEGORIES: tuple[str, ...] = (
    "groceries",
    "dining",
    "transport",
    "shopping",
    "entertainment",
    "utilities",
    "health",
    "subscription",
    "travel",
    "other",
)
_CATEGORY_PROBS = np.array(
    [0.22, 0.20, 0.09, 0.12, 0.08, 0.07, 0.05, 0.06, 0.03, 0.08], dtype=float
)
_CATEGORY_AMOUNT_FACTOR = {
    "groceries": 1.0,
    "dining": 0.55,
    "transport": 0.35,
    "shopping": 1.6,
    "entertainment": 1.1,
    "utilities": 2.4,
    "health": 1.4,
    "subscription": 0.5,
    "travel": 4.5,
    "other": 0.9,
}

#: 非必要支出类别（供 ``subject.discretionary_spending_ratio`` 使用）
DISCRETIONARY_CATEGORIES: frozenset[str] = frozenset(
    {"dining", "shopping", "entertainment", "subscription", "travel"}
)

NOTE_TEMPLATES: tuple[str, ...] = (
    "今天状态还行，晚上争取早点睡。",
    "会议太多，专注被打断了好几次。",
    "跑了 5 公里，心情不错。",
    "出差回来有点累，先补一觉。",
    "这周睡得不太规律，要调整一下。",
    "尝试新的工作节奏：上午深度工作，下午开会。",
    "有点感冒迹象，多喝水。",
    "月底了，把预算再核一遍。",
)
LLM_TURN_TEMPLATES: tuple[str, ...] = (
    "我这周的睡眠怎么样？",
    "最近专注度是不是下降了？",
    "帮我看看这个月的非必要支出占比。",
    "出差那几天步数为啥这么高？",
    "我的静息心率有变化吗？",
    "把最近的情绪波动总结一下。",
    "上周运动和恢复的关系如何？",
    "睡眠规律性这周有改善吗？",
)
WORKOUT_CATEGORIES: tuple[str, ...] = ("run", "cycle", "strength", "swim", "yoga")

#: 各事件类型的数据源
SOURCE_BY_EVENT_TYPE: dict[str, str] = {
    "transaction": "bank",
    "note": "phone",
    "llm_turn": "llm_client",
    "workout": "wearable",
}

_EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "subject_id",
    "occurred_at",
    "date_key",
    "event_type",
    "amount",
    "category",
    "text",
    "source_id",
    "recorded_at",
)


@dataclass(frozen=True)
class ExogenousWindows:
    """逐日外生状态（各数组长度均为 ``n_days``）。"""

    travel: np.ndarray
    illness: np.ndarray
    crunch: np.ndarray
    incident: np.ndarray
    weight_plan: np.ndarray
    plan_start: pd.Timestamp
    plan_end: pd.Timestamp

    @property
    def n_days(self) -> int:
        """天数。"""

        return int(self.travel.shape[0])

    def summary(self) -> dict[str, int]:
        """窗口天数与脉冲次数统计（用于日志与保真度测试）。"""

        return {
            "travel_days": int(self.travel.sum()),
            "illness_days": int(self.illness.sum()),
            "crunch_days": int(self.crunch.sum()),
            "incident_days": int(self.incident.sum()),
            "weight_plan_days": int(self.weight_plan.sum()),
        }


def _place_windows(
    rng: np.random.Generator,
    n: int,
    count: int,
    min_len: int,
    max_len: int,
    min_gap: int,
    forbidden: np.ndarray | None = None,
) -> np.ndarray:
    """随机放置 ``count`` 个互不重叠（含最小间隔）的窗口。

    Args:
        rng: 随机流。
        n: 序列长度。
        count: 目标窗口数。
        min_len: 最短窗口天数。
        max_len: 最长窗口天数。
        min_gap: 窗口之间的最小间隔天数。
        forbidden: 不可占用的布尔掩码。

    Returns:
        长度 ``n`` 的布尔掩码；放置失败时窗口数会少于 ``count``。
    """

    mask = np.zeros(n, dtype=bool)
    blocked = np.zeros(n, dtype=bool) if forbidden is None else np.asarray(forbidden, dtype=bool)
    for _ in range(count):
        for _attempt in range(200):
            length = int(rng.integers(min_len, max_len + 1))
            if length >= n:
                break
            start = int(rng.integers(0, n - length + 1))
            end = start + length
            lo = max(0, start - min_gap)
            hi = min(n, end + min_gap)
            if mask[lo:hi].any() or blocked[start:end].any():
                continue
            mask[start:end] = True
            break
    return mask


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """把布尔掩码向两侧扩张 ``radius`` 天。"""

    if radius <= 0:
        return np.asarray(mask, dtype=bool)
    grown = (
        pd.Series(np.asarray(mask, dtype=np.int8))
        .rolling(2 * radius + 1, center=True, min_periods=1)
        .max()
        .to_numpy()
    )
    return grown > 0


def _place_illness(
    rng: np.random.Generator,
    winter: np.ndarray,
    count: int,
    min_len: int,
    max_len: int,
    min_gap: int,
    forbidden: np.ndarray,
) -> np.ndarray:
    """放置生病窗口：起点按 ``1 + 2.5 * 冬季因子`` 加权（冬季更易发病）。

    Args:
        rng: 随机流。
        winter: 季节因子序列。
        count: 目标窗口数。
        min_len: 最短天数。
        max_len: 最长天数。
        min_gap: 窗口之间的最小间隔。
        forbidden: 不可占用的布尔掩码（如出差窗口及其邻域）。

    Returns:
        布尔掩码。
    """

    n = winter.shape[0]
    mask = np.zeros(n, dtype=bool)
    blocked = np.asarray(forbidden, dtype=bool).copy()
    day_weight = 1.0 + 2.5 * np.clip(winter, 0.0, None)
    for _ in range(count):
        length = int(rng.integers(min_len, max_len + 1))
        if length >= n:
            break
        eligible = ~blocked
        cumulative = np.concatenate(([0], np.cumsum(eligible.astype(np.int64))))
        starts = np.arange(0, n - length + 1)
        window_ok = (cumulative[starts + length] - cumulative[starts]) == length
        candidates = starts[window_ok]
        if candidates.size == 0:
            break
        weights = np.array(
            [float(day_weight[s : s + length].mean()) for s in candidates], dtype=float
        )
        chosen = int(rng.choice(candidates, p=weights / weights.sum()))
        mask[chosen : chosen + length] = True
        blocked = blocked | _dilate(mask, min_gap)
    return mask


def build_windows(dates: pd.DatetimeIndex) -> ExogenousWindows:
    """生成逐日出差、生病、项目冲刺、工作突发事件与减重计划窗口。

    Args:
        dates: 连续日粒度日期索引。

    Returns:
        :class:`ExogenousWindows`。
    """

    dates = pd.DatetimeIndex(dates)
    n = dates.shape[0]
    winter = winter_factor(dates)
    dow = dates.dayofweek.to_numpy()

    travel = _place_windows(
        child_rng("events.travel"), n, count=10, min_len=2, max_len=7, min_gap=12
    )
    dilated_travel = _dilate(travel, 3)
    illness = _place_illness(
        child_rng("events.illness"),
        winter,
        count=6,
        min_len=2,
        max_len=5,
        min_gap=18,
        forbidden=dilated_travel,
    )

    crunch_rng = child_rng("events.crunch")
    monday_idx = np.flatnonzero(dow == 0)
    crunch = np.zeros(n, dtype=bool)
    for _ in range(9):
        for _attempt in range(60):
            start = int(monday_idx[int(crunch_rng.integers(0, monday_idx.shape[0]))])
            end = start + 5
            if end > n:
                continue
            lo, hi = max(0, start - 25), min(n, end + 25)
            if crunch[lo:hi].any():
                continue
            crunch[start:end] = True
            break

    incident_rng = child_rng("events.incident")
    workdays = np.flatnonzero(dow < 5)
    incident = np.zeros(n, dtype=bool)
    blocked = dilated_travel | illness | crunch
    for _ in range(36):
        for _attempt in range(60):
            day = int(workdays[int(incident_rng.integers(0, workdays.shape[0]))])
            lo, hi = max(0, day - 4), min(n, day + 5)
            if incident[lo:hi].any() or blocked[day]:
                continue
            incident[day] = True
            break

    weight_plan = (dates >= PLAN_START) & (dates <= PLAN_END)
    return ExogenousWindows(
        travel=travel,
        illness=illness,
        crunch=crunch,
        incident=incident,
        weight_plan=weight_plan,
        plan_start=PLAN_START,
        plan_end=PLAN_END,
    )


def _expand_daily_counts(
    dates: pd.DatetimeIndex,
    counts: np.ndarray,
    minutes_lo: int,
    minutes_hi: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """把"每日条数"展开成逐条时间戳。

    Args:
        dates: 日期索引。
        counts: 每日条数。
        minutes_lo: 当天最早时刻（自 00:00 起的分钟数）。
        minutes_hi: 当天最晚时刻（分钟数，不含）。
        rng: 随机流。

    Returns:
        ``(day_index, occurred_at)``。
    """

    day_index = np.repeat(np.arange(dates.shape[0], dtype=np.int64), counts.astype(np.int64))
    total = int(day_index.shape[0])
    if total == 0:
        return day_index, pd.DatetimeIndex([])
    minutes = rng.integers(minutes_lo, minutes_hi, size=total).astype("int64")
    occurred_at = pd.DatetimeIndex(dates[day_index] + pd.to_timedelta(minutes, unit="m"))
    return day_index, occurred_at


def _empty_event_frame() -> pd.DataFrame:
    """空事件表（列与 ``fact_event`` 对齐）。"""

    return pd.DataFrame(
        {
            "occurred_at": pd.DatetimeIndex([]),
            "event_type": pd.Series([], dtype="str"),
            "amount": np.array([], dtype="float64"),
            "category": pd.Series([], dtype="str"),
            "text": pd.Series([], dtype="str"),
            "source_id": pd.Series([], dtype="str"),
            "recorded_at": pd.DatetimeIndex([]),
        }
    )


def _transactions(dates: pd.DatetimeIndex, windows: ExogenousWindows) -> pd.DataFrame:
    """构造 ``transaction`` 事件（金额 lognormal，出差日更多且更贵）。"""

    rng = child_rng("events.transaction")
    weekend = dates.dayofweek.to_numpy() >= 5
    lam = np.clip(
        np.where(weekend, 4.3, 3.1) + 2.4 * windows.travel - 1.1 * windows.illness,
        0.2,
        None,
    )
    day_index, occurred_at = _expand_daily_counts(dates, rng.poisson(lam), 450, 1350, rng)
    total = int(day_index.shape[0])
    if total == 0:
        return _empty_event_frame()

    categories = rng.choice(np.array(TRANSACTION_CATEGORIES), size=total, p=_CATEGORY_PROBS)
    force_travel = windows.travel[day_index] & (rng.random(total) < 0.40)
    categories = np.where(force_travel, "travel", categories)

    factors = np.array([_CATEGORY_AMOUNT_FACTOR[str(c)] for c in categories], dtype=float)
    amounts = np.round(
        np.clip(np.exp(rng.normal(3.0, 0.80, size=total)) * factors, 3.0, 3000.0), 2
    )
    lag = rng.integers(60, 1500, size=total).astype("int64")
    return pd.DataFrame(
        {
            "occurred_at": occurred_at,
            "event_type": pd.Series(["transaction"] * total, dtype="str"),
            "amount": amounts,
            "category": pd.Series(categories, dtype="str"),
            "text": pd.Series([None] * total, dtype="str"),
            "source_id": pd.Series([SOURCE_BY_EVENT_TYPE["transaction"]] * total, dtype="str"),
            "recorded_at": occurred_at + pd.to_timedelta(lag, unit="m"),
        }
    )


def _dedupe_minutes_within_day(
    occurred_at: pd.DatetimeIndex, minutes_lo: int, minutes_hi: int
) -> pd.DatetimeIndex:
    """把同一天内重复的时刻挪到当天最近的空闲分钟（确定性、保序）。

    仅用于 `note`：同一天两条笔记落在同一分钟会让它们在**所有业务列**上完全相同
    （正文只含模板 + 日期），那是真正的重复行。

    只改动发生碰撞的那些行；其余行的时刻逐字节不变，因此对下游指标的影响面最小。
    """
    minutes = occurred_at.hour.to_numpy() * 60 + occurred_at.minute.to_numpy()
    day_keys = occurred_at.normalize().to_numpy()
    used: dict[object, set[int]] = {}
    out: list[int] = []
    width = minutes_hi - minutes_lo
    for key, minute in zip(day_keys, minutes):
        taken = used.setdefault(key, set())
        candidate = int(minute)
        offset = 0
        while candidate in taken and offset < width:
            offset += 1
            candidate = minutes_lo + (int(minute) - minutes_lo + offset) % width
        taken.add(candidate)
        out.append(candidate)
    shifted = np.array(out, dtype="int64")
    return pd.DatetimeIndex(
        occurred_at.normalize() + pd.to_timedelta(shifted, unit="m")
    )


def _notes(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """构造 ``note`` 事件（文本 = 模板 + 日期 + 精确时刻）。

    文本里带**时刻**（不只是日期）是刻意的：同一天可能记两条笔记，只写日期的话
    它们会逐列完全相同，`event_id` 之外无法区分。
    """
    rng = child_rng("events.note")
    day_index, occurred_at = _expand_daily_counts(
        dates, rng.poisson(0.55, size=dates.shape[0]), 480, 1380, rng
    )
    total = int(day_index.shape[0])
    if total == 0:
        return _empty_event_frame()
    # 同一天内的时刻去重：否则"文本含时刻"也挡不住两行完全相同
    occurred_at = _dedupe_minutes_within_day(occurred_at, 480, 1380)
    picks = rng.integers(0, len(NOTE_TEMPLATES), size=total)
    texts = [
        f"{NOTE_TEMPLATES[int(pick)]}（{ts.strftime('%Y-%m-%d %H:%M')}）"
        for pick, ts in zip(picks, occurred_at)
    ]
    lag = rng.integers(0, 8, size=total).astype("int64")
    return pd.DataFrame(
        {
            "occurred_at": occurred_at,
            "event_type": pd.Series(["note"] * total, dtype="str"),
            "amount": np.full(total, np.nan, dtype="float64"),
            "category": pd.Series([None] * total, dtype="str"),
            "text": pd.Series(texts, dtype="str"),
            "source_id": pd.Series([SOURCE_BY_EVENT_TYPE["note"]] * total, dtype="str"),
            "recorded_at": occurred_at + pd.to_timedelta(lag, unit="m"),
        }
    )


def _llm_turns(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """构造 ``llm_turn`` 事件（文本 = 模板）。"""

    rng = child_rng("events.llm_turn")
    day_index, occurred_at = _expand_daily_counts(
        dates, rng.poisson(1.6, size=dates.shape[0]), 420, 1420, rng
    )
    total = int(day_index.shape[0])
    if total == 0:
        return _empty_event_frame()
    picks = rng.integers(0, len(LLM_TURN_TEMPLATES), size=total)
    texts = [LLM_TURN_TEMPLATES[int(pick)] for pick in picks]
    lag = rng.integers(0, 2, size=total).astype("int64")
    return pd.DataFrame(
        {
            "occurred_at": occurred_at,
            "event_type": pd.Series(["llm_turn"] * total, dtype="str"),
            "amount": np.full(total, np.nan, dtype="float64"),
            "category": pd.Series(["query"] * total, dtype="str"),
            "text": pd.Series(texts, dtype="str"),
            "source_id": pd.Series([SOURCE_BY_EVENT_TYPE["llm_turn"]] * total, dtype="str"),
            "recorded_at": occurred_at + pd.to_timedelta(lag, unit="m"),
        }
    )


def _workouts(dates: pd.DatetimeIndex, latent_daily: pd.DataFrame) -> pd.DataFrame:
    """构造 ``workout`` 事件：与潜在结构中的运动日一一对应，时长即运动分钟。"""

    rng = child_rng("events.workout")
    exercise = latent_daily["exercise_minutes_true"].to_numpy(dtype=float)
    day_index = np.flatnonzero(exercise > 0.0)
    total = int(day_index.shape[0])
    if total == 0:
        return _empty_event_frame()
    minutes = rng.integers(360, 1200, size=total).astype("int64")
    occurred_at = pd.DatetimeIndex(dates[day_index] + pd.to_timedelta(minutes, unit="m"))
    sports = rng.choice(np.array(WORKOUT_CATEGORIES), size=total)
    lag = rng.integers(5, 180, size=total).astype("int64")
    return pd.DataFrame(
        {
            "occurred_at": occurred_at,
            "event_type": pd.Series(["workout"] * total, dtype="str"),
            "amount": np.round(exercise[day_index], 1),
            "category": pd.Series(sports, dtype="str"),
            "text": pd.Series([None] * total, dtype="str"),
            "source_id": pd.Series([SOURCE_BY_EVENT_TYPE["workout"]] * total, dtype="str"),
            "recorded_at": occurred_at + pd.to_timedelta(lag, unit="m"),
        }
    )


def build_fact_event(
    dates: pd.DatetimeIndex, latent_daily: pd.DataFrame, windows: ExogenousWindows
) -> pd.DataFrame:
    """构造完整 ``fact_event`` 表（transaction / note / llm_turn / workout）。

    Args:
        dates: 日期索引。
        latent_daily: 日粒度潜在结构表（取 ``exercise_minutes_true``）。
        windows: 外生窗口。

    Returns:
        含 ``event_id`` 的事件表，按 ``occurred_at`` 升序、``event_id`` 从 1 连续。
    """

    dates = pd.DatetimeIndex(dates)
    frame = pd.concat(
        [
            _transactions(dates, windows),
            _notes(dates),
            _llm_turns(dates),
            _workouts(dates, latent_daily),
        ],
        ignore_index=True,
    )
    frame = frame.sort_values(["occurred_at", "event_type"], kind="stable").reset_index(drop=True)
    frame.insert(0, "event_id", np.arange(1, frame.shape[0] + 1, dtype="int64"))
    frame.insert(1, "subject_id", pd.Series([config.SUBJECT_ID] * frame.shape[0], dtype="str"))
    frame.insert(
        3,
        "date_key",
        (
            frame["occurred_at"].dt.year * 10_000
            + frame["occurred_at"].dt.month * 100
            + frame["occurred_at"].dt.day
        ).astype("int32"),
    )
    return frame.loc[:, list(_EVENT_COLUMNS)]

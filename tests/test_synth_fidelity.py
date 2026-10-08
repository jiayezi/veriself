"""合成数据保真度测试（``synth`` 的验收标准）。

四类断言：

1. **植入的效应存在**：睡眠 ≤6h 的次日专注显著更低（Welch t 检验 p<0.05，方向正确）、
   恢复度是受睡眠与运动驱动的 AR(1)、情绪有季节项与周内效应、压力由工作事件脉冲驱动、
   周末/出差/生病/减重计划效应、减重计划触发 ``dim_subject`` 的 SCD2 第 2 版；
2. **未植入的效应不存在**：若干"明显无关"的序列对相关系数 ``|r| < 0.15``；
3. **确定性**：同一 ``config.SYNTH_SEED`` 连跑两次，关键序列逐元素相等、Parquet 逐字节相等；
4. **规模与通道覆盖**：行数落在合理区间，18 个指标所需的原始通道逐一核验非空。

运行：``python -m pytest tests/test_synth_fidelity.py -q``
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import duckdb
import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from scipy import stats

from veriself import config
from veriself.synth import generate_all
from veriself.synth.dimensions import BASE_WEIGHT_KG
from veriself.synth.events import PLAN_END, PLAN_START
from veriself.synth.generator import CONTRACT_TABLES, INTERMEDIATE_TABLES, ORDER_KEYS
from veriself.synth.observations import (
    DAILY_CHANNEL_NAMES,
    INTERMEDIATE_INTRADAY_CHANNELS,
    PROMOTED_INTRADAY_CHANNELS,
    SLOTS_PER_DAY,
)

#: 18 个指标 -> 必须非空的原始通道（对照 ``docs/01-指标清单.md``）
METRIC_SOURCE_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "subject.sleep_duration_daily": (("fact_observation", "sleep_hours"),),
    "subject.sleep_need_deviation_daily": (
        ("fact_observation", "sleep_hours"),
        ("dim_subject", "sleep_need_h"),
    ),
    "subject.sleep_debt_7d": (
        ("fact_observation", "sleep_hours"),
        ("dim_subject", "sleep_need_h"),
    ),
    "subject.sleep_regularity_7d": (("fact_observation", "sleep_start_hour"),),
    "subject.recovery_score_daily": (
        ("fact_observation", "resting_hr"),
        ("fact_observation", "hrv"),
    ),
    "subject.deep_sleep_ratio_daily": (
        ("fact_observation", "deep_sleep_hours"),
        ("fact_observation", "sleep_hours"),
    ),
    "subject.avg_resting_hr_7d": (("fact_observation", "resting_hr"),),
    "subject.daily_steps": (("fact_observation", "steps"),),
    "subject.exercise_minutes_daily": (("fact_observation", "exercise_minutes"),),
    "subject.focus_score_daily": (("fact_observation", "focus_score"),),
    "subject.focus_score_weekly": (("fact_observation", "focus_score"),),
    "subject.screen_minutes_daily": (("fact_observation", "screen_minutes"),),
    "subject.spending_daily": (("fact_event", "amount_transaction"),),
    "subject.spending_monthly": (("fact_event", "amount_transaction"),),
    "subject.discretionary_spending_ratio": (("fact_event", "category_transaction"),),
    "subject.mood_score_daily": (("fact_observation", "mood_score"),),
    "subject.mood_volatility_7d": (("fact_observation", "mood_score"),),
    "subject.note_count_weekly": (("fact_event", "note"),),
}

#: 未植入因果关系的序列对（用于"未植入的效应不存在"断言）
UNPLANTED_PAIRS: tuple[tuple[str, str], ...] = (
    ("deep_sleep_hours", "screen_minutes"),
    ("focus_score", "spending_daily"),
    ("steps", "note_count"),
    ("sleep_start_hour", "exercise_minutes"),
)

#: 契约第 1 节的列（断言"存在"而非"相等"，允许他人 DDL 追加列）
CONTRACT_COLUMNS: dict[str, tuple[str, ...]] = {
    "dim_date": (
        "date_key", "date", "year", "quarter", "month", "week",
        "day_of_week", "weekday_name", "is_weekend", "is_holiday",
    ),
    "dim_subject": (
        "subject_sk", "subject_id", "name", "birth_date", "sleep_need_h",
        "base_weight_kg", "timezone", "valid_from", "valid_to", "is_current",
        "version", "recorded_at",
    ),
    "dim_source": ("source_id", "display_name", "reliability_tier"),
    "dim_context": ("context_sk", "context_id", "is_travel", "is_illness", "location_type"),
    "fact_observation": (
        "observation_id", "subject_id", "observed_at", "date_key", "channel",
        "value", "source_id", "recorded_at",
    ),
    "fact_event": (
        "event_id", "subject_id", "occurred_at", "date_key", "event_type",
        "amount", "category", "text", "source_id", "recorded_at",
    ),
}


def _welch(first: np.ndarray, second: np.ndarray) -> tuple[float, float]:
    """Welch t 检验，返回 ``(t, p)``。"""

    result = stats.ttest_ind(np.asarray(first, dtype=float), np.asarray(second, dtype=float), equal_var=False)
    # TtestResult 由 _make_tuple_bunch 动态生成，存根看不到 statistic/pvalue。
    # 运行时它是 tuple，顺序固定为 (statistic, pvalue, df)。
    statistic, pvalue = cast(tuple[float, float], result[:2])
    return statistic, pvalue


def _cohen_d(first: np.ndarray, second: np.ndarray) -> float:
    """合并标准差下的 Cohen's d。"""

    a = np.asarray(first, dtype=float)
    b = np.asarray(second, dtype=float)
    pooled = np.sqrt(((a.size - 1) * a.var(ddof=1) + (b.size - 1) * b.var(ddof=1)) / (a.size + b.size - 2))
    return float((a.mean() - b.mean()) / pooled)


#: 一次性测试目录的根（**必须**在工作区内：沙箱禁止写系统临时目录，
#: 也禁止向 ``tempfile.mkdtemp`` 建的 0700 目录里再建子目录）
TEST_SCRATCH_ROOT: Path = config.DATA_DIR / "synth_test_runs"


def _sha256(path: Path) -> str:
    """文件 sha256。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reset_scratch_dir(tag: str) -> Path:
    """准备一个干净的一次性目录。

    目录名带进程号：多个 pytest 进程（并行 teammate）同时跑本文件时互不干扰。
    """

    path = TEST_SCRATCH_ROOT / f"{tag}_{os.getpid()}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    (TEST_SCRATCH_ROOT / "DO_NOT_DELETE.txt").write_text(
        "本目录是 tests/test_synth_fidelity.py 的一次性运行目录（每次运行自建、结束自删）。\n"
        "并行开发时请勿清理这里的内容：正在跑的测试会因为产物被删而中途失败。\n",
        encoding="utf-8",
    )
    return path


def _cleanup_scratch_dir(path: Path) -> None:
    """清理本次进程的一次性目录。"""

    shutil.rmtree(path, ignore_errors=True)


@dataclass
class SynthRun:
    """一次生成的结果（DuckDB + Parquet + 行数统计）。

    产物可能被并行的 workspace 清理误删，因此读取时具备自愈能力：
    产物丢失就用同一种子重建（内容与首次完全相同）后重试一次。
    """

    root: Path
    counts: dict[str, int]

    @property
    def db_path(self) -> Path:
        """DuckDB 文件路径。"""

        return self.root / "warehouse.duckdb"

    @property
    def synth_dir(self) -> Path:
        """Parquet 目录。"""

        return self.root / "synth"

    def regenerate(self) -> None:
        """重建本次运行的产物（固定种子 → 内容与首次逐元素相同）。"""

        self.counts = generate_all(db_path=self.db_path, synth_dir=self.synth_dir)

    def _artifacts_lost(self) -> bool:
        """产物是否被外部删除（区别于锁冲突等其他 DuckDB 错误）。"""

        if not self.db_path.exists() or not self.synth_dir.exists():
            return True
        return not any(self.synth_dir.glob("*.parquet"))

    def query(self, sql: str) -> pd.DataFrame:
        """只读查询 DuckDB；产物被外部删除时重建后重试一次。"""

        for attempt in (1, 2):
            try:
                with duckdb.connect(str(self.db_path), read_only=True) as connection:
                    return connection.execute(sql).fetchdf()
            except duckdb.Error:
                if attempt == 2 or not self._artifacts_lost():
                    raise
                self.regenerate()
        raise AssertionError("不可达：query 重试逻辑异常")  # pragma: no cover

    def table(self, name: str) -> pd.DataFrame:
        """整表读出。"""

        return self.query(f'SELECT * FROM "{name}"')

    def n_days(self) -> int:
        """``dim_date`` 行数（即日粒度天数）。"""

        return int(self.counts["dim_date"])

    def daily_wide(self) -> pd.DataFrame:
        """日粒度观测宽表（``date_key`` 为索引）+ 事件派生的日粒度序列。"""

        observed = self.query(
            "SELECT date_key, channel, value FROM fact_observation "
            f"WHERE channel IN ({', '.join(repr(name) for name in DAILY_CHANNEL_NAMES)})"
        )
        wide = (
            observed.pivot_table(index="date_key", columns="channel", values="value", aggfunc="sum")
            .sort_index()
        )
        events = self.query("SELECT date_key, event_type, amount FROM fact_event")
        transactions = events.loc[events["event_type"] == "transaction"]
        notes = events.loc[events["event_type"] == "note"]
        wide["spending_daily"] = (
            wide.index.map(transactions.groupby("date_key")["amount"].sum()).astype(float).fillna(0.0)
        )
        wide["note_count"] = (
            wide.index.map(notes.groupby("date_key").size()).astype(float).fillna(0.0)
        )
        return wide

    def latent(self) -> pd.DataFrame:
        """潜在结构表（``date_key`` 为索引）。"""

        return self.query(
            f"SELECT * FROM read_parquet('{(self.synth_dir / 'latent_daily.parquet').as_posix()}')"
        ).set_index("date_key")

    def intraday(self) -> pd.DataFrame:
        """日内观测量表（Parquet 中间产物）。"""

        return self.query(
            f"SELECT * FROM read_parquet('{(self.synth_dir / 'obs_intraday.parquet').as_posix()}')"
        )


@pytest.fixture(scope="session")
def synth() -> Iterator[SynthRun]:
    """生成一份隔离的合成数据供全部断言使用（会话结束清理）。"""

    root = _reset_scratch_dir("primary")
    try:
        counts = generate_all(db_path=root / "warehouse.duckdb", synth_dir=root / "synth")
        yield SynthRun(root=root, counts=counts)
    finally:
        _cleanup_scratch_dir(root)


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
    """第二个一次性目录（用于确定性对比）。"""

    path = _reset_scratch_dir("second")
    try:
        yield path
    finally:
        _cleanup_scratch_dir(path)


# --------------------------------------------------------------------- 1. 植入效应存在


def test_planted_sleep_le_6h_lowers_next_day_focus(synth: SynthRun) -> None:
    """核心效应：睡眠 ≤6h 的"次日"专注度显著低于 ≥7.5h（Welch t 检验 p<0.05）。

    日期约定：``date_key=d`` 的 ``sleep_hours`` 是结束于 d 日早晨的那一觉，
    因此 d 日就是这一觉的"次日"。
    """

    wide = synth.daily_wide()
    sleep = wide["sleep_hours"]
    focus = wide["focus_score"]
    short = focus[sleep <= 6.0].to_numpy(dtype=float)
    long = focus[sleep >= 7.5].to_numpy(dtype=float)
    assert short.size >= 25, f"短睡眠样本过少：{short.size}"
    assert long.size >= 200, f"长睡眠样本过少：{long.size}"

    t_stat, p_value = _welch(short, long)
    effect = _cohen_d(short, long)
    assert short.mean() < long.mean(), f"效应方向错误：短睡眠 {short.mean():.2f} vs 长睡眠 {long.mean():.2f}"
    assert p_value < 0.05, f"p={p_value:.3g} 不显著（t={t_stat:.2f}）"
    assert effect < -0.3, f"效应量过小：d={effect:.2f}"


def test_planted_sleep_effect_carries_over_to_following_day(synth: SynthRun) -> None:
    """滞后效应：睡眠债与恢复度是 AR(1)，所以 d 日睡眠对 d+1 日专注仍有可检出影响。"""

    wide = synth.daily_wide()
    sleep = wide["sleep_hours"]
    focus_next = wide["focus_score"].shift(-1)
    mask = focus_next.notna()
    short = focus_next[mask & (sleep <= 6.0)].to_numpy(dtype=float)
    long = focus_next[mask & (sleep >= 7.5)].to_numpy(dtype=float)
    t_stat, p_value = _welch(short, long)
    assert short.mean() < long.mean(), "滞后效应方向错误"
    assert p_value < 0.05, f"滞后效应不显著：p={p_value:.3g}（t={t_stat:.2f}）"


def test_planted_recovery_is_ar1_driven_by_sleep_and_exercise(synth: SynthRun) -> None:
    """恢复度：AR(1) 状态变量，受前夜睡眠与当日运动正向驱动，并传导到静息心率/HRV。"""

    latent = synth.latent()
    recovery = latent["latent_recovery"]
    assert recovery.autocorr(1) > 0.4, f"AR(1) 自相关过低：{recovery.autocorr(1):.3f}"

    corr_sleep = float(np.corrcoef(latent["sleep_hours_true"], recovery)[0, 1])
    assert corr_sleep > 0.2, f"睡眠 -> 恢复度相关性过低：{corr_sleep:.3f}"

    workout_days = latent["exercise_minutes_true"] > 0
    t_stat, p_value = _welch(recovery[workout_days], recovery[~workout_days])
    assert recovery[workout_days].mean() > recovery[~workout_days].mean()
    assert p_value < 0.01, f"运动 -> 恢复度不显著：p={p_value:.3g}（t={t_stat:.2f}）"

    wide = synth.daily_wide()
    assert float(wide["resting_hr"].corr(wide["hrv"])) < -0.3, "恢复度未传导到静息心率/HRV"
    assert float(wide["sleep_hours"].corr(wide["resting_hr"])) < -0.15, "睡眠不足未抬高静息心率"


def test_planted_mood_seasonality_and_weekday_effect(synth: SynthRun) -> None:
    """情绪：冬季显著低于夏季（季节项），周末显著高于周一（周内效应）。"""

    wide = synth.daily_wide()
    months = pd.to_datetime(wide.index.astype(str), format="%Y%m%d").month.to_numpy()
    weekday = pd.to_datetime(wide.index.astype(str), format="%Y%m%d").dayofweek.to_numpy()
    mood = wide["mood_score"].to_numpy(dtype=float)

    winter = mood[np.isin(months, (12, 1, 2))]
    summer = mood[np.isin(months, (6, 7, 8))]
    _, p_season = _welch(winter, summer)
    assert winter.mean() < summer.mean(), "季节项方向错误（应为冬季低）"
    assert p_season < 0.01, f"季节效应不显著：p={p_season:.3g}"

    weekend = mood[weekday >= 5]
    monday = mood[weekday == 0]
    _, p_week = _welch(weekend, monday)
    assert weekend.mean() > monday.mean(), "周内效应方向错误（应为周末高于周一）"
    assert p_week < 0.01, f"周内效应不显著：p={p_week:.3g}"


def test_planted_stress_pulses_follow_work_events(synth: SynthRun) -> None:
    """压力：由工作事件驱动的脉冲过程（突发/冲刺/出差/生病首日），且按 AR(1) 衰减。"""

    latent = synth.latent()
    stress = latent["latent_stress"]
    incident = latent["is_incident"].to_numpy(dtype=bool)
    assert incident.sum() >= 20, "突发事件过少"

    t_stat, p_value = _welch(stress[incident], stress[~incident])
    assert stress[incident].mean() > stress[~incident].mean()
    assert p_value < 0.001, f"突发事件未抬高压力：p={p_value:.3g}（t={t_stat:.2f}）"
    assert stress.autocorr(1) > 0.4, f"压力未体现衰减（AR(1)={stress.autocorr(1):.3f}）"

    travel_start = latent["is_travel"].to_numpy(dtype=bool) & ~np.concatenate(
        ([False], latent["is_travel"].to_numpy(dtype=bool)[:-1])
    )
    assert stress[travel_start].mean() > stress[~latent["is_travel"].to_numpy(dtype=bool)].mean(), (
        "出差首日未抬高压力"
    )


def test_planted_weekend_activity_and_screen_effects(synth: SynthRun) -> None:
    """活动/屏幕的周内效应：周末步数更高、工作日屏幕时间更长（均为植入效应）。"""

    wide = synth.daily_wide()
    weekday = pd.to_datetime(wide.index.astype(str), format="%Y%m%d").dayofweek.to_numpy()
    steps = wide["steps"].to_numpy(dtype=float)
    screen = wide["screen_minutes"].to_numpy(dtype=float)

    _, p_steps = _welch(steps[weekday >= 5], steps[weekday < 5])
    assert steps[weekday >= 5].mean() > steps[weekday < 5].mean()
    assert p_steps < 0.01, f"周末步数效应不显著：p={p_steps:.3g}"

    _, p_screen = _welch(screen[weekday < 5], screen[weekday >= 5])
    assert screen[weekday < 5].mean() > screen[weekday >= 5].mean()
    assert p_screen < 0.01, f"工作日屏幕效应不显著：p={p_screen:.3g}"


def test_planted_travel_and_illness_effects(synth: SynthRun) -> None:
    """出差与生病：睡眠、专注、步数按植入方向变化，且差异显著。"""

    latent = synth.latent()
    travel = latent["is_travel"].to_numpy(dtype=bool)
    illness = latent["is_illness"].to_numpy(dtype=bool)
    assert travel.sum() >= 20 and illness.sum() >= 10, "出差/生病窗口过少"

    _, p_travel_focus = _welch(latent["latent_focus"][travel], latent["latent_focus"][~travel])
    assert latent["latent_focus"][travel].mean() < latent["latent_focus"][~travel].mean()
    assert p_travel_focus < 0.01, f"出差未降低专注：p={p_travel_focus:.3g}"
    assert latent["sleep_hours_true"][travel].mean() < latent["sleep_hours_true"][~travel].mean()
    assert latent["steps_true"][travel].mean() > latent["steps_true"][~travel].mean()

    _, p_illness_steps = _welch(latent["steps_true"][illness], latent["steps_true"][~illness])
    assert latent["steps_true"][illness].mean() < latent["steps_true"][~illness].mean()
    assert p_illness_steps < 0.01, f"生病未降低步数：p={p_illness_steps:.3g}"
    assert latent["latent_mood"][illness].mean() < latent["latent_mood"][~illness].mean()


def test_planted_weight_plan_triggers_scd2_second_version(synth: SynthRun) -> None:
    """减重计划：触发 ``dim_subject`` 的 SCD2 第 2 版，并改变体重/活动/屏幕。"""

    subject = synth.table("dim_subject").sort_values("version").reset_index(drop=True)
    assert subject.shape[0] >= 2, "SCD2 版本数不足 2"
    assert subject["version"].is_unique
    assert int(subject["is_current"].sum()) == 1, "is_current 必须恰好一行为真"

    current = subject.loc[subject["is_current"]].iloc[0]
    assert pd.isna(current["valid_to"]), "当前版本 valid_to 必须为 NULL"
    second = subject.loc[subject["version"] == 2].iloc[0]
    assert pd.Timestamp(second["valid_from"]) == PLAN_START, "第 2 版生效日必须等于减重计划开始日"
    assert float(second["base_weight_kg"]) < float(subject.loc[subject["version"] == 1, "base_weight_kg"].iloc[0])
    assert pd.Timestamp(second["recorded_at"]) >= pd.Timestamp(second["valid_from"])

    previous_end = pd.Timestamp(subject.loc[subject["version"] == 1, "valid_to"].iloc[0])
    assert previous_end == PLAN_START, "SCD2 区间必须首尾相接且不重叠"

    latent = synth.latent()
    plan = latent["is_weight_plan"].to_numpy(dtype=bool)
    assert plan.sum() >= 100, "减重计划窗口过短"
    assert latent["weight_kg"][plan].min() < BASE_WEIGHT_KG - 5.0, "计划期内体重未下降"
    assert latent["weight_kg"][plan].iloc[-1] < latent["weight_kg"][plan].iloc[0], "体重未随计划下降"

    plan_start_key = int(PLAN_START.strftime("%Y%m%d"))
    plan_end_key = int(PLAN_END.strftime("%Y%m%d"))
    plan_window = latent.loc[(latent.index >= plan_start_key) & (latent.index <= plan_end_key)]
    before = latent.loc[latent.index < plan_start_key]
    assert plan_window.shape[0] >= 100
    assert plan_window["steps_true"].mean() > before["steps_true"].mean(), "减重期步数未上升"
    assert plan_window["screen_minutes_true"].mean() < before["screen_minutes_true"].mean(), "减重期屏幕未下降"


# ----------------------------------------------------------- 2. 未植入的效应不存在


def test_unplanted_pairs_have_margin(synth: SynthRun) -> None:
    """未植入的序列对不仅低于阈值，还应留有余量（``|r| < 0.10``）。"""

    wide = synth.daily_wide()
    correlations = {f"{a}~{b}": float(wide[a].corr(wide[b])) for a, b in UNPLANTED_PAIRS}
    worst = max(correlations.items(), key=lambda item: abs(item[1]))
    assert abs(worst[1]) < 0.10, f"余量不足：{worst[0]} r={worst[1]:+.3f}（{correlations}）"


# ------------------------------------------------------------------------ 3. 确定性


def _sorted(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """按主键稳定排序并重置索引（用于逐元素比较）。"""

    return frame.sort_values(keys, kind="stable").reset_index(drop=True)


def test_rerun_on_existing_warehouse_is_idempotent(scratch_dir: Path) -> None:
    """同一个数仓文件上**连续两次** `generate_all` 必须都成功且行数一致（见 AGENTS.md「易错点」）。

    为什么需要这条：`synth` 的装载是「DELETE 全部行 → INSERT 新行」。
    曾经这一步包在一个显式事务里，而 DuckDB 在**持久化库**上，同一事务内 `DELETE`
    不会让**上一会话建好的唯一索引**条目失效——紧接着 `INSERT` 同一个键就撞"已删除的键"。
    `dim_subject`（`ux_dim_subject_version`）与 `fact_observation`
    （`ux_fact_observation_id` / `_natural_key`）都有唯一索引，
    所以那种写法让 `veriself synth` **第二次运行直接失败**。

    ⚠️ 关键在「两次调用之间连接已关闭」：索引由第一次调用的会话持久化，
    第二次调用是新会话——这正是生产里 `veriself synth && veriself demo` 的形态，
    也是只在同一会话内生成的测试发现不了它的原因。
    """

    db_path = scratch_dir / "warehouse.duckdb"
    synth_dir = scratch_dir / "synth"

    first = generate_all(db_path=db_path, synth_dir=synth_dir)
    second = generate_all(db_path=db_path, synth_dir=synth_dir)  # ← 曾经在这里抛 ConstraintException

    assert first == second, "重跑同一数仓时行数统计发生变化"
    for name in CONTRACT_TABLES:
        rows = duckdb.connect(str(db_path), read_only=True).execute(
            f'SELECT count(*) FROM "{name}"'
        ).fetchone()
        assert rows is not None and int(rows[0]) == first[name], f"{name} 重跑后行数不符"


def test_two_runs_are_elementwise_identical(synth: SynthRun, scratch_dir: Path) -> None:
    """同种子跑两次：6 张表 + 潜在结构逐元素相等，9 个 Parquet 逐字节相等。

    注意 DuckDB **文件**字节不保证相同（页分配/统计信息属实现细节），
    因此这里断言的是表内容逐元素相等 + 中间产物逐字节相等。
    """

    counts = generate_all(db_path=scratch_dir / "warehouse.duckdb", synth_dir=scratch_dir / "synth")
    second = SynthRun(root=scratch_dir, counts=counts)
    assert counts == synth.counts, "两次运行的行数统计不一致"

    for name in CONTRACT_TABLES:
        keys = list(ORDER_KEYS[name])
        assert_frame_equal(
            _sorted(synth.table(name), keys),
            _sorted(second.table(name), keys),
            check_exact=True,
            obj=f"两次运行的 {name}",
        )

    assert_frame_equal(synth.latent(), second.latent(), check_exact=True, obj="两次运行的 latent_daily")

    for name in CONTRACT_TABLES + INTERMEDIATE_TABLES:
        first_path = synth.synth_dir / f"{name}.parquet"
        second_path = second.synth_dir / f"{name}.parquet"
        assert _sha256(first_path) == _sha256(second_path), f"{name}.parquet 两次生成不是逐字节相同"

    # 中间产物与装载进 DuckDB 的事实表必须一致（日粒度通道）
    daily_from_db = synth.query(
        "SELECT date_key, channel, value FROM fact_observation "
        f"WHERE channel IN ({', '.join(repr(name) for name in DAILY_CHANNEL_NAMES)}) "
        "ORDER BY date_key, channel"
    )
    assert_frame_equal(
        daily_from_db, _sorted(second.query(
            f"SELECT date_key, channel, value FROM read_parquet("
            f"'{(second.synth_dir / 'obs_daily.parquet').as_posix()}')"
        ), ["date_key", "channel"]),
        check_exact=True,
        obj="fact_observation 与 obs_daily.parquet",
    )


# ------------------------------------------------------------- 4. 规模与通道覆盖


def test_row_counts_within_expected_ranges(synth: SynthRun) -> None:
    """行数落在合理区间：3 年日粒度 + 百万级以内的观测。"""

    counts = synth.counts
    expected_days = len(pd.date_range(config.SYNTH_START_DATE, config.SYNTH_END_DATE, freq="D"))
    assert counts["dim_date"] == expected_days == 1004
    assert counts["dim_subject"] >= 2
    assert counts["dim_source"] == 4
    assert counts["dim_context"] >= 4
    assert 50_000 <= counts["fact_observation"] <= 1_000_000, counts["fact_observation"]
    assert 3_000 <= counts["fact_event"] <= 20_000, counts["fact_event"]
    assert counts["latent_daily"] == expected_days
    assert counts["obs_daily"] == expected_days * len(DAILY_CHANNEL_NAMES)
    assert counts["obs_intraday"] == expected_days * (SLOTS_PER_DAY + 2 * 24)


def test_metric_requirements_are_all_non_empty(synth: SynthRun) -> None:
    """18 个指标依赖的原始通道逐一核验：存在且非空。"""

    assert len(METRIC_SOURCE_REQUIREMENTS) == 18, "指标清单应为 18 个"

    observation = synth.query("SELECT channel, count(*) AS rows, count(value) AS filled FROM fact_observation GROUP BY channel")
    rows_by_channel = dict(zip(observation["channel"], observation["rows"]))
    filled_by_channel = dict(zip(observation["channel"], observation["filled"]))
    events = synth.table("fact_event")
    transactions = events.loc[events["event_type"] == "transaction"]
    notes = events.loc[events["event_type"] == "note"]
    weeks_with_notes = set(
        pd.to_datetime(notes["occurred_at"]).dt.strftime("%G-%V").tolist()
    ) if notes.shape[0] else set()
    total_weeks = len(pd.date_range(config.SYNTH_START_DATE, config.SYNTH_END_DATE, freq="W"))

    subject = synth.table("dim_subject")

    for metric_id, requirements in METRIC_SOURCE_REQUIREMENTS.items():
        for table, key in requirements:
            if table == "fact_observation":
                assert rows_by_channel.get(key, 0) > 0, f"{metric_id}: 通道 {key} 无数据"
                assert filled_by_channel.get(key, 0) == rows_by_channel[key], f"{metric_id}: 通道 {key} 有 NULL"
            elif table == "dim_subject":
                present = np.asarray(subject[key].notna(), dtype=bool)
                assert present.all(), f"{metric_id}: dim_subject.{key} 有 NULL"
            elif key == "amount_transaction":
                assert transactions.shape[0] > 0, f"{metric_id}: 没有 transaction 事件"
                assert transactions["amount"].notna().all(), f"{metric_id}: transaction.amount 有 NULL"
                assert float(transactions["amount"].sum()) > 0, f"{metric_id}: 消费金额全为 0"
            elif key == "category_transaction":
                assert transactions["category"].notna().all(), f"{metric_id}: transaction.category 有 NULL"
                assert transactions["category"].nunique() >= 5, f"{metric_id}: 消费类别过少"
            elif key == "note":
                assert notes.shape[0] >= 100, f"{metric_id}: note 事件过少"
                assert len(weeks_with_notes) >= 0.5 * total_weeks, f"{metric_id}: note 周覆盖不足"
            else:  # pragma: no cover - 防御性分支
                raise AssertionError(f"未知的需求键：{table}.{key}")


def test_observation_channels_and_timestamps(synth: SynthRun) -> None:
    """通道覆盖与时间戳不变式：一天一行、``date_key == date(observed_at)``、``recorded_at >= observed_at``。"""

    days = synth.n_days()
    coverage = synth.query(
        "SELECT channel, count(*) AS rows, count(DISTINCT date_key) AS days, "
        "min(value) AS min_value, max(value) AS max_value, "
        "sum(CASE WHEN observed_at IS NULL THEN 1 ELSE 0 END) AS null_ts "
        "FROM fact_observation GROUP BY channel ORDER BY channel"
    )
    coverage = coverage.set_index("channel")
    for channel in DAILY_CHANNEL_NAMES:
        assert channel in coverage.index, f"缺少日粒度通道 {channel}"
        assert int(coverage.loc[channel, "rows"]) == days, channel
        assert int(coverage.loc[channel, "days"]) == days, channel
        assert int(coverage.loc[channel, "null_ts"]) == 0
        assert float(coverage.loc[channel, "min_value"]) < float(coverage.loc[channel, "max_value"])
    for channel in PROMOTED_INTRADAY_CHANNELS:
        assert channel in coverage.index, f"缺少日内通道 {channel}"
        assert int(coverage.loc[channel, "rows"]) == days * SLOTS_PER_DAY, channel

    mismatch = synth.query(
        "SELECT count(*) AS n FROM fact_observation "
        "WHERE date_key <> CAST(strftime(observed_at, '%Y%m%d') AS INTEGER)"
    )["n"].iloc[0]
    assert int(mismatch) == 0, "date_key 与 observed_at 的日历日不一致"
    late = synth.query("SELECT count(*) AS n FROM fact_observation WHERE recorded_at < observed_at")["n"].iloc[0]
    assert int(late) == 0, "recorded_at 早于 observed_at"

    wide = synth.daily_wide()
    deep_ratio = (wide["deep_sleep_hours"] / wide["sleep_hours"]).max()
    assert deep_ratio < 0.5, f"深睡占比异常：{deep_ratio:.2f}"


def test_intraday_decomposition_sums_to_daily_channels(synth: SynthRun) -> None:
    """小时级 ``steps_intraday`` / ``screen_minutes_intraday`` 按日求和等于日粒度通道值。"""

    intraday = synth.intraday()
    assert set(INTERMEDIATE_INTRADAY_CHANNELS) <= set(intraday["channel"].unique())

    daily = synth.query(
        "SELECT date_key, channel, value FROM fact_observation "
        "WHERE channel IN ('steps', 'screen_minutes')"
    )
    for hourly_channel, daily_channel in (
        ("steps_intraday", "steps"),
        ("screen_minutes_intraday", "screen_minutes"),
    ):
        hourly_sum = (
            intraday.loc[intraday["channel"] == hourly_channel]
            .groupby("date_key")["value"]
            .sum()
            .sort_index()
        )
        expected = (
            daily.loc[daily["channel"] == daily_channel]
            .set_index("date_key")["value"]
            .sort_index()
        )
        assert np.allclose(hourly_sum.to_numpy(), expected.to_numpy(), rtol=0.0, atol=1e-6), (
            f"{hourly_channel} 按日求和与 {daily_channel} 不一致"
        )


def test_contract_columns_exist_in_all_six_tables(synth: SynthRun) -> None:
    """6 张契约表都存在，且契约列全部存在（多出的列不影响断言）。"""

    tables = synth.query("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'")
    present = set(tables["table_name"])
    for name in CONTRACT_TABLES:
        assert name in present, f"缺少契约表 {name}"
        columns = set(synth.query(f'SELECT * FROM "{name}" LIMIT 0').columns)
        missing = set(CONTRACT_COLUMNS[name]) - columns
        assert not missing, f"{name} 缺少契约列：{sorted(missing)}"
        assert synth.counts[name] > 0, f"{name} 行数为 0"


# ================================================================ 事件唯一性

#: 事件的**业务列**——决定"两行是不是同一件事"。排除 `event_id`（代理键，天然唯一）、
#: `subject_id`（单主体）、`date_key`（`occurred_at` 的派生）、`recorded_at`（写入滞后，非业务语义）。
EVENT_BUSINESS_COLUMNS: tuple[str, ...] = (
    "occurred_at",
    "event_type",
    "amount",
    "category",
    "text",
    "source_id",
)


def test_no_duplicate_event_rows() -> None:
    """`fact_event` 不得存在**所有业务列完全相同**的行。

    为什么需要这条：`note` 的正文若只含"模板 + 日期"，同一天多条 note 的时刻又独立随机，
    两者都撞上就产生逐列完全相同的两行，下游 `note_count_weekly` 会多算 1 且**没有任何报错**。

    注意这条**不是**在禁止"同一分钟两笔交易"——那是金额/分类不同的合法数据，
    所以判重必须用**全部**业务列，不能只用时间戳。
    """
    from veriself.synth.generator import build_tables

    dates = pd.date_range(config.SYNTH_START_DATE, config.SYNTH_END_DATE, freq="D")
    contract, _intermediate = build_tables(dates)
    events = contract["fact_event"]

    columns = [c for c in EVENT_BUSINESS_COLUMNS if c in events.columns]
    duplicated = events[events.duplicated(subset=columns, keep=False)]
    assert duplicated.empty, (
        f"存在 {len(duplicated)} 行在所有业务列上完全相同（真正的重复事件）：\n"
        f"{duplicated[columns].to_string(index=False)}"
    )


def test_note_occurred_at_is_unique_within_day() -> None:
    """`note` 在同一天内的时刻必须唯一——这是"文本含时刻"能起作用的**前提**。

    如果两个 note 同一天同一分钟，即便正文含时刻也仍然逐列相同；因此
    `events._dedupe_minutes_within_day` 会把碰撞的时刻挪到当天最近的空闲分钟。
    这里把该不变量固定下来，防止有人删掉去重步骤后只留"文本含时刻"而以为已修好。
    """
    from veriself.synth.generator import build_tables

    dates = pd.date_range(config.SYNTH_START_DATE, config.SYNTH_END_DATE, freq="D")
    contract, _intermediate = build_tables(dates)
    notes = contract["fact_event"]
    notes = notes[notes["event_type"] == "note"]

    per_day = notes.groupby(notes["occurred_at"].dt.normalize())["occurred_at"].nunique()
    counts = notes.groupby(notes["occurred_at"].dt.normalize()).size()
    assert (per_day == counts).all(), (
        "同一分钟内出现多条 note：\n"
        f"{counts[per_day != counts].to_string()}"
    )

    # 去重不得把时刻挪出当天、也不得越出生成区间
    minutes = notes["occurred_at"].dt.hour * 60 + notes["occurred_at"].dt.minute
    assert minutes.between(480, 1379).all(), "去重后时刻越出 480..1380 分钟区间"

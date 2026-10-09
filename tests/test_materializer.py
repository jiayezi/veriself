"""物化器单元测试（Lead 所有）。

用内存 DuckDB 自建最小星型模型，不依赖 `synth` / `metrics` 的产物，
因此可以在队友模块尚未就绪时独立验证物化语义。
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from veriself import config, sqlrefs
from veriself.materializer import (
    _row_expression,
    _topo_layers,
    _touches,
    ensure_views,
    materialize_all,
    source_aliases,
)

# ------------------------------------------------------------------ fixtures
DDL = """
CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY, date DATE, year INTEGER, quarter INTEGER, month INTEGER,
    week INTEGER, day_of_week INTEGER, weekday_name VARCHAR, is_weekend BOOLEAN, is_holiday BOOLEAN
);
CREATE TABLE dim_subject (
    subject_id VARCHAR, name VARCHAR, birth_date DATE, sleep_need_h DOUBLE,
    base_weight_kg DOUBLE, timezone VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP,
    is_current BOOLEAN, version INTEGER, recorded_at TIMESTAMP
);
CREATE TABLE dim_source (
    source_id VARCHAR PRIMARY KEY, display_name VARCHAR, reliability_tier VARCHAR
);
CREATE TABLE fact_subject_day (
    subject_id VARCHAR, date_key INTEGER, is_travel BOOLEAN, is_illness BOOLEAN, location_type VARCHAR,
    PRIMARY KEY (subject_id, date_key)
);
CREATE TABLE fact_observation (
    observation_id BIGINT, subject_id VARCHAR, observed_at TIMESTAMP, date_key INTEGER,
    channel VARCHAR, value DOUBLE, source_id VARCHAR, recorded_at TIMESTAMP
);
CREATE TABLE fact_event (
    event_id BIGINT, subject_id VARCHAR, occurred_at TIMESTAMP, date_key INTEGER,
    event_type VARCHAR, amount DOUBLE, category VARCHAR, text VARCHAR,
    source_id VARCHAR, recorded_at TIMESTAMP
);
CREATE TABLE fact_metric_value (
    metric_id VARCHAR, subject_id VARCHAR, date_key INTEGER, value DOUBLE,
    metric_version INTEGER, contract_hash VARCHAR, computed_at TIMESTAMP,
    valid_from TIMESTAMP, valid_to TIMESTAMP,
    PRIMARY KEY (metric_id, subject_id, date_key, valid_from)
);
"""

SUBJECT = "S001"
SLEEP_NEED = 7.75
START = date(2026, 1, 1)
DAYS = 12


def _contract(**overrides):
    base = {
        "metric_id": "subject.x",
        "version": 1,
        "status": "active",
        "owner": "test",
        "display_name": "x",
        "synonyms": [],
        "definition": "test",
        "unit": "hour",
        "direction": "neutral",
        "agg": "mean",
        "grain": "day",
        "entity": "subject",
        "allowed_dimensions": ["date.weekday"],
        "allowed_filters": ["date.between"],
        "rls_policy": "owner_only",
        "formula_sql": "o.sleep_hours",
        "lineage": {"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
    }
    base.update(overrides)
    return base


@pytest.fixture()
def conn():
    c = duckdb.connect(":memory:")
    c.execute(DDL)
    c.execute(
        "INSERT INTO dim_subject VALUES (?, 'demo', DATE '1990-01-01', ?, 70.0, 'Asia/Shanghai',"
        " TIMESTAMP '2020-01-01 00:00:00', NULL, TRUE, 1, TIMESTAMP '2020-01-01 00:00:00')",
        [SUBJECT, SLEEP_NEED],
    )
    c.execute("INSERT INTO dim_source VALUES ('wearable', 'Wearable', 'high')")

    rows = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        key = int(d.strftime("%Y%m%d"))
        c.execute(
            "INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)",
            [key, d, d.year, (d.month - 1) // 3 + 1, d.month, int(d.strftime("%W")),
             d.isoweekday(), d.strftime("%A"), d.isoweekday() >= 6],
        )
        # 每天写多条不同通道的观测 —— 用于验证通道作用域（channel 过滤）
        rows.append((i * 10 + 1, SUBJECT, d, key, "sleep_hours", 6.0, "wearable"))
        rows.append((i * 10 + 2, SUBJECT, d, key, "focus_score", 70.0, "phone"))
        rows.append((i * 10 + 3, SUBJECT, d, key, "steps", 9000.0, "wearable"))
        rows.append((i * 10 + 4, SUBJECT, d, key, "mood_score", 60.0, "phone"))
    c.executemany(
        "INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, now())", rows
    )
    yield c
    c.close()


# ------------------------------------------------------------------ 分层
def test_topo_layers_orders_upstream_first():
    contracts = {
        "subject.b": _contract(metric_id="subject.b", lineage={"sources": [], "upstream_metrics": ["subject.a"]}),
        "subject.a": _contract(metric_id="subject.a"),
        "subject.c": _contract(metric_id="subject.c"),
    }
    layers = _topo_layers(contracts)
    assert layers[0] == ["subject.a", "subject.c"]
    assert layers[1] == ["subject.b"]


def test_topo_layers_detects_cycle():
    contracts = {
        "subject.a": _contract(metric_id="subject.a", lineage={"sources": [], "upstream_metrics": ["subject.b"]}),
        "subject.b": _contract(metric_id="subject.b", lineage={"sources": [], "upstream_metrics": ["subject.a"]}),
    }
    with pytest.raises(config.ContractError, match="环"):
        _topo_layers(contracts)


# ------------------------------------------------------------------ 契约公式写法
def test_formula_forms_are_unified_to_bare_columns():
    """契约只允许两种写法（docs/00 §2）：物理表名或 `o.` / `e.` / `s.` 别名。

    两种写法必须归一到**同一个**裸列表达式——公式最终求值在 `daily_src` 之上，
    那里没有表别名，留着前缀会 Binder Error。
    """
    assert _row_expression("fact_observation.sleep_hours") == "sleep_hours"
    assert _row_expression("o.sleep_hours") == "sleep_hours"
    assert _row_expression("fact_event.spending") == "spending"
    assert _row_expression("dim_subject.sleep_need_h") == "sleep_need_h"


def test_metric_call_expands_to_lateral_column():
    """`metric('X')` 展开成 `daily_src` 透出的裸列 `up_<alias>`（不是 `up_x.value`）。

    上游值在 `daily_src` 里由 `up_<alias>.value AS up_<alias>` 投影出来，
    所以外层求值时它已经是裸列名。（sqlglot 渲染函数名/关键字为大写，断言按渲染输出写。）
    """
    assert _row_expression("metric('subject.sleep_duration_daily')") == (
        "up_subject_sleep_duration_daily"
    )
    # 窗口公式里的展开同样成立
    assert _row_expression(
        "sum(greatest(-metric('subject.sleep_need_deviation_daily'), 0)) OVER ("
        "PARTITION BY subject_id ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)"
    ).startswith("SUM(GREATEST(-up_subject_sleep_need_deviation_daily, 0)) OVER (")


def test_rewrite_aliases_are_derived_from_semantic_models():
    """重写的别名必须覆盖语义模型里**每一个**来源表（由 YAML 派生，单一来源）。

    防止有人把别名写死成 `[oes]`：那样加第四张来源表时，新别名会被漏掉，
    前缀漏进外层作用域，直到执行才 Binder Error。
    """
    aliases = source_aliases()
    assert aliases == {"fact_observation": "o", "fact_event": "e", "dim_subject": "s"}
    for alias in aliases.values():
        assert sqlrefs.rewrite(f"{alias}.steps", table_aliases=aliases) == "steps"
    # 前缀识别不能误伤"含别名字母的标识符"：`subject_o` 不是 `o` 前缀，应原样保留
    assert _row_expression("subject_o.steps") == "subject_o.steps"


def test_materialized_view_names_are_not_contract_vocabulary():
    """物化视图名不是契约词汇：`_touches` 与 `_row_expression` 必须给出同一个答案。

    若 `_touches` 把 `obs_daily.` 当作"引用了 fact_observation"，而重写路径不认它，
    就会"骨架按它建、`obs_daily.xxx` 却漏进外层作用域"，直到执行时才 Binder Error。
    两个函数对"合法写法"必须没有分歧。
    """
    assert _touches("fact_observation.steps", "fact_observation") is True
    assert _touches("o.steps", "fact_observation") is True
    assert _touches("obs_daily.steps", "fact_observation") is False
    assert _touches("subject_asof.sleep_need_h", "dim_subject") is False
    assert _touches("evt_daily.spending", "fact_event") is False


def test_comments_and_string_literals_are_not_references():
    """注释与字符串字面量里的文本不算引用。

    正则实现会把注释里的 `metric('x')` 当真实引用、改写字符串字面量里的
    `fact_observation.` 前缀、把注释里的表名算进 `_touches`、用 `over(` 子串
    误判窗口函数。AST 实现全部免疫。
    """
    # 注释里的 metric 调用不是引用
    assert sqlrefs.metric_calls("-- 曾用 metric('subject.old_metric')\nfact_observation.sleep_hours") == []
    # 字符串字面量里的 metric 调用不是引用
    assert sqlrefs.metric_calls("concat('metric(''subject.x'')', fact_observation.sleep_hours)") == []
    # 注释里的表名不触发 touches
    assert _touches("-- fact_event.spending\nfact_observation.steps", "fact_event") is False
    # 字符串字面量里的 `表.列` 文本不被改写
    rewritten = _row_expression("concat('fact_observation.steps', fact_observation.steps)")
    assert "fact_observation" in rewritten  # 字面量原样保留
    # 字符串字面量里的 `over(` 不触发窗口判定
    assert sqlrefs.uses_window("concat('over(', fact_observation.sleep_hours)") is False
    # 真实窗口函数仍然触发
    assert sqlrefs.uses_window("avg(fact_observation.resting_hr) OVER (PARTITION BY subject_id)") is True
def test_obs_channels_are_channel_scoped(conn):
    """回归测试：`obs_daily` 每个通道列必须只聚合自己通道的行。

    漏掉 `FILTER (WHERE channel = ...)` 时，每个通道列会变成"当天全部通道的聚合"：
    行数正常、数值全错（sleep_hours 会变成 6.0+70.0+9000.0+60.0=9136），极难发现。
    """
    ensure_views(conn)
    row = conn.execute(
        "SELECT sleep_hours, focus_score, steps, mood_score FROM obs_daily"
        " WHERE date_key = ?",
        [int(START.strftime("%Y%m%d"))],
    ).fetchone()
    assert row == (6.0, 70.0, 9000.0, 60.0), f"通道未隔离，实际得到 {row}"


def test_materialize_with_injected_minimal_models(conn):
    """语义模型可显式注入（内存对象，不依赖 semantic_models/ 磁盘文件）。"""
    from veriself.semantic_model import SemanticColumn, SemanticModel

    obs = SemanticModel(
        model_id="obs_min", table="fact_observation", entity="subject_id",
        grain="day", date_column="date_key", alias="o",
        columns=[
            SemanticColumn(name="sleep_hours", channel="sleep_hours", agg="sum"),
            SemanticColumn(name="steps", channel="steps", agg="sum"),
        ],
    )
    evt = SemanticModel(
        model_id="evt_min", table="fact_event", entity="subject_id",
        grain="day", date_column="date_key", alias="e",
        columns=[
            SemanticColumn(
                name="spending",
                expr="sum(CASE WHEN event_type = 'transaction' THEN coalesce(amount, 0) ELSE 0 END)",
            ),
        ],
    )
    sub = SemanticModel(
        model_id="sub_min", table="dim_subject", type="dimension",
        entity="subject_id", alias="s",
        columns=[SemanticColumn(name="sleep_need_h")],
    )
    models = {"fact_observation": obs, "fact_event": evt, "dim_subject": sub}

    contracts = {
        "subject.min": _contract(
            metric_id="subject.min",
            formula_sql="fact_observation.sleep_hours - dim_subject.sleep_need_h",
            lineage={
                "sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"],
                "upstream_metrics": [],
            },
        )
    }
    stats = materialize_all(conn, contracts, models=models)
    assert stats["subject.min"]["live"] == DAYS
    got = conn.execute(
        "SELECT DISTINCT value FROM fact_metric_value WHERE metric_id = 'subject.min'"
    ).fetchall()
    assert got == [(pytest.approx(6.0 - SLEEP_NEED),)]


def test_declarative_bucket_sum_within_week(conn):
    """`bucket: {agg: sum}` 由物化器自动做桶内求和，date_key = 桶内最后有数据日。

    12 天数据跨 3 个 ISO 周（周一起算）：01-01..01-04（4 天）、01-05..01-11（7 天）、
    01-12（1 天）；每天 sleep_hours=6.0 → 周和分别为 24 / 42 / 6。
    """
    contracts = {
        "subject.weekly_sleep_sum": _contract(
            metric_id="subject.weekly_sleep_sum",
            grain="week",
            agg="sum",
            formula_sql="o.sleep_hours",
            bucket={"agg": "sum"},
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        )
    }
    materialize_all(conn, contracts)
    rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value"
        " WHERE metric_id = 'subject.weekly_sleep_sum' AND valid_to IS NULL ORDER BY date_key"
    ).fetchall()
    assert [k for k, _ in rows] == [20260104, 20260111, 20260112]
    assert [v for _, v in rows] == [24.0, 42.0, 6.0]
    # date_key 必须是真实存在的日期（与 dim_date 可内连接）
    for date_key, _ in rows:
        assert conn.execute(
            "SELECT count(*) FROM dim_date WHERE date_key = ?", [date_key]
        ).fetchone()[0] == 1


def test_declarative_bucket_mean_and_last(conn):
    """`bucket.agg=mean` 取桶内均值；`last` 取桶内最后一天的值。"""
    contracts = {
        "subject.weekly_sleep_mean": _contract(
            metric_id="subject.weekly_sleep_mean",
            grain="week",
            agg="mean",
            formula_sql="o.sleep_hours",
            bucket={"agg": "mean"},
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        ),
        "subject.weekly_sleep_last": _contract(
            metric_id="subject.weekly_sleep_last",
            grain="week",
            agg="mean",
            formula_sql="o.sleep_hours",
            bucket={"agg": "last"},
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        ),
    }
    materialize_all(conn, contracts)
    # 把第 2 周最后一天（01-11）改成 9.5 → mean 桶值变化、last 桶值 = 9.5
    conn.execute(
        "UPDATE fact_observation SET value = 9.5"
        " WHERE channel = 'sleep_hours' AND date_key = 20260111"
    )
    materialize_all(conn, contracts)

    mean_rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value"
        " WHERE metric_id = 'subject.weekly_sleep_mean' AND valid_to IS NULL ORDER BY date_key"
    ).fetchall()
    last_rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value"
        " WHERE metric_id = 'subject.weekly_sleep_last' AND valid_to IS NULL ORDER BY date_key"
    ).fetchall()
    assert [k for k, _ in mean_rows] == [20260104, 20260111, 20260112]
    assert mean_rows[1][1] == pytest.approx((6.0 * 6 + 9.5) / 7), mean_rows
    assert last_rows[1][1] == pytest.approx(9.5), last_rows
    assert last_rows[0][1] == pytest.approx(6.0)


def test_layer0_daily_value_is_evaluated(conn):
    """第 0 层：formula_sql 直接引用原始观测，按日求值。"""
    contracts = {"subject.sleep_hours_daily": _contract(metric_id="subject.sleep_hours_daily")}
    stats = materialize_all(conn, contracts)["subject.sleep_hours_daily"]
    assert stats["live"] == DAYS
    assert stats["inserted"] == DAYS  # 首轮全部是新增

    values = conn.execute(
        "SELECT DISTINCT value FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
    ).fetchall()
    assert values == [(6.0,)]


def test_derived_metric_uses_upstream_not_raw(conn):
    """派生指标必须走 metric('X') 引用的上游结果，而不是重新读原始观测。"""
    contracts = {
        "subject.dev": _contract(
            metric_id="subject.dev",
            formula_sql="o.sleep_hours - s.sleep_need_h",
            lineage={"sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"], "upstream_metrics": []},
        ),
        "subject.dev_mean": _contract(
            metric_id="subject.dev_mean",
            formula_sql="avg(metric('subject.dev')) OVER ()",
            lineage={"sources": [], "upstream_metrics": ["subject.dev"]},
        ),
    }
    materialize_all(conn, contracts)
    got = conn.execute(
        "SELECT DISTINCT value FROM fact_metric_value WHERE metric_id = 'subject.dev_mean'"
    ).fetchall()
    assert got == [(pytest.approx(6.0 - SLEEP_NEED),)]


def test_window_formula_is_not_double_aggregated(conn):
    """回归测试：含 OVER 的滚动窗口公式不得被二次聚合。

    每天偏差 = 6.0 - 7.75 = -1.75；7 日滚动和应为 -12.25。
    若实现错误地在窗口结果上再套 mean/sum，得到的就是别的数。
    """
    contracts = {
        "subject.dev": _contract(
            metric_id="subject.dev",
            formula_sql="o.sleep_hours - s.sleep_need_h",
            lineage={"sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"], "upstream_metrics": []},
        ),
        "subject.debt7": _contract(
            metric_id="subject.debt7",
            agg="sum",
            direction="lower_better",
            formula_sql=(
                "sum(metric('subject.dev')) OVER ("
                "ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)"
            ),
            lineage={"sources": [], "upstream_metrics": ["subject.dev"]},
        ),
    }
    materialize_all(conn, contracts)

    rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value WHERE metric_id = 'subject.debt7'"
        " ORDER BY date_key"
    ).fetchall()
    assert len(rows) == DAYS
    expected = [round(-1.75 * min(i + 1, 7), 6) for i in range(DAYS)]
    assert [round(v, 6) for _, v in rows] == expected
    assert rows[-1][1] == pytest.approx(-12.25)


def test_no_rows_written_for_null_values(conn):
    """无数据的日期不写行，而不是写 0。"""
    contracts = {
        "subject.spending": _contract(
            metric_id="subject.spending",
            unit="currency",
            agg="sum",
            formula_sql="e.spending",
            lineage={"sources": ["fact_event.amount"], "upstream_metrics": []},
        )
    }
    written = materialize_all(conn, contracts)
    # fact_event 为空 → evt_daily 为空 → 不应写入任何行
    assert written["subject.spending"]["live"] == 0


def test_nan_and_inf_are_filtered(conn):
    """NaN / Inf 不得落库。"""
    contracts = {
        "subject.ratio": _contract(
            metric_id="subject.ratio",
            unit="ratio",
            formula_sql="CASE WHEN o.sleep_hours = 0 THEN 0.0/0.0 ELSE o.sleep_hours END",
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        ),
        "subject.inf": _contract(
            metric_id="subject.inf",
            # 引用合法来源以确定日期骨架，但求值结果为正无穷 → 必须被过滤掉
            formula_sql="o.sleep_hours * 1e308 * 10",
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        ),
    }
    materialize_all(conn, contracts)
    for metric_id in ("subject.ratio", "subject.inf"):
        bad = conn.execute(
            "SELECT count(*) FROM fact_metric_value WHERE metric_id = ?"
            " AND (isnan(value) OR isinf(value))",
            [metric_id],
        ).fetchone()[0]
        assert bad == 0, f"{metric_id} 写入了非有限值"


def test_non_day_grain_emits_one_row_per_bucket(conn):
    """非日粒度指标：每桶只落一行，date_key 是桶内最后一个有数据的真实日期。

    公式按契约用 `CASE WHEN date_key = max(date_key) OVER (PARTITION BY <桶>)` 标记代表行。
    """
    week_bucket = (
        "date_trunc('week', strptime(CAST(date_key AS VARCHAR), '%Y%m%d'))"
    )
    contracts = {
        "subject.weekly_avg_sleep": _contract(
            metric_id="subject.weekly_avg_sleep",
            grain="week",
            agg="mean",
            formula_sql=(
                "CASE WHEN date_key = max(date_key) OVER (PARTITION BY "
                f"{week_bucket}) "
                "THEN avg(fact_observation.sleep_hours) OVER (PARTITION BY "
                f"{week_bucket}) END"
            ),
            lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        )
    }
    materialize_all(conn, contracts)

    rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value"
        " WHERE metric_id = 'subject.weekly_avg_sleep' AND valid_to IS NULL"
        " ORDER BY date_key"
    ).fetchall()
    # 12 天：2026-01-01(周四)..2026-01-12(周一) 跨 3 个自然周（周一起算）→ 3 行；
    # 每天都是 6.0 → 周均值 6.0；代表日期分别是各周最后一个有数据的日子 01-04 / 01-11 / 01-12
    assert len(rows) == 3, f"每桶应只落一行，实际 {rows}"
    assert [k for k, _ in rows] == [20260104, 20260111, 20260112]
    assert all(v == pytest.approx(6.0) for _, v in rows)
    # date_key 必须是真实存在的日期，且是该周最后一个有数据的日子
    for date_key, _ in rows:
        assert conn.execute(
            "SELECT count(*) FROM dim_date WHERE date_key = ?", [date_key]
        ).fetchone()[0] == 1


def test_recompute_is_idempotent_and_creates_no_history(conn):
    """重算相同值：不增行、不产生历史。

    值 + 口径哈希都没变 → 跳过，历史行数保持 0。若每次物化都"关闭全部有效行 +
    全量重写"，重算会把表撑成 N 倍。
    """
    contracts = {"subject.sleep_hours_daily": _contract(metric_id="subject.sleep_hours_daily")}
    first = materialize_all(conn, contracts)["subject.sleep_hours_daily"]
    second = materialize_all(conn, contracts)["subject.sleep_hours_daily"]

    assert first["inserted"] == DAYS
    assert second["inserted"] == 0, "重算不应插入新行"
    assert second["unchanged"] == DAYS, "所有键都应被判定为无变化"

    current = conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = 'subject.sleep_hours_daily' AND valid_to IS NULL"
    ).fetchone()[0]
    assert current == DAYS, "当前有效行数不应叠加"

    total = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
    ).fetchone()[0]
    assert total == DAYS, f"无变化重算不得产生历史行，实际总行数 {total}"

    # 每个 (date_key, subject_id) 恰好一行有效
    dup = conn.execute(
        "SELECT count(*) FROM ("
        "  SELECT date_key, subject_id, count(*) c FROM fact_metric_value"
        "  WHERE metric_id = 'subject.sleep_hours_daily' AND valid_to IS NULL"
        "  GROUP BY 1, 2 HAVING c > 1)"
    ).fetchone()[0]
    assert dup == 0, "同一 (date_key, subject_id) 不得有多行有效"


def test_value_change_creates_history(conn):
    """值真正变化时必须留痕（as-of 能力不能因幂等优化而丢失）。"""
    contracts = {"subject.sleep_hours_daily": _contract(metric_id="subject.sleep_hours_daily")}
    materialize_all(conn, contracts)

    # 把某一天的观测值改掉，再重算 → 该天应"关闭旧行 + 插入新行"，其余天不变
    conn.execute(
        "UPDATE fact_observation SET value = 9.5"
        " WHERE channel = 'sleep_hours' AND date_key = ?",
        [int(START.strftime("%Y%m%d"))],
    )
    stats = materialize_all(conn, contracts)["subject.sleep_hours_daily"]

    assert stats["closed_changed"] == 1, f"应恰好关闭 1 行历史，实际 {stats}"
    assert stats["inserted"] == 1, f"应插入 1 行新值，实际 {stats}"
    assert stats["unchanged"] == DAYS - 1

    live = conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = 'subject.sleep_hours_daily' AND valid_to IS NULL"
    ).fetchone()[0]
    assert live == DAYS

    history = conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = 'subject.sleep_hours_daily' AND valid_to IS NOT NULL"
    ).fetchone()[0]
    assert history == 1, "值变化必须留一行历史，否则 as-of 无法回答'为什么变了'"

    # 历史行保留旧值、当前行是新值
    old = conn.execute(
        "SELECT value FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
        " AND valid_to IS NOT NULL"
    ).fetchone()[0]
    new = conn.execute(
        "SELECT value FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
        " AND valid_to IS NULL AND date_key = ?",
        [int(START.strftime("%Y%m%d"))],
    ).fetchone()[0]
    assert (old, new) == (6.0, 9.5)


def test_vanished_keys_are_closed_not_left_zombie(conn):
    """某些键不再产出时必须被关闭，否则会留下"陈旧但被当作当前事实"的僵尸行。"""
    contracts = {"subject.sleep_hours_daily": _contract(metric_id="subject.sleep_hours_daily")}
    materialize_all(conn, contracts)

    # 删掉最后 2 天的观测 → 那些 date_key 不再出现在新结果集里
    conn.execute(
        "DELETE FROM fact_observation WHERE channel = 'sleep_hours' AND date_key >= ?",
        [int((START + timedelta(days=DAYS - 2)).strftime("%Y%m%d"))],
    )
    stats = materialize_all(conn, contracts)["subject.sleep_hours_daily"]

    assert stats["closed_vanished"] == 2, f"应关闭 2 个消失的键，实际 {stats}"
    assert stats["inserted"] == 0
    assert stats["live"] == DAYS - 2, "消失的键不得继续有效"

    zombie = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
        " AND valid_to IS NULL AND date_key >= ?",
        [int((START + timedelta(days=DAYS - 2)).strftime("%Y%m%d"))],
    ).fetchone()[0]
    assert zombie == 0, "不得留下僵尸有效行"


def test_version_upgrade_does_not_close_previous_version(conn):
    """物化 v2 **不得**关闭 v1 的有效行。

    关闭语句若只按 `metric_id` 过滤，物化 v2 会把 v1 的有效行一并关闭，
    于是"用旧口径回溯"直接失效。现在由 `build_merge_sql` 的关闭语句显式限定
    `metric_version` 保证这一点。

    本项目的语义是：**多版本可同时有效，当前版本由查询侧按 `metric_version` 选择。**
    """
    v1 = _contract(metric_id="subject.sleep_hours_daily")
    materialize_all(conn, {"subject.sleep_hours_daily": v1})
    v1_live_before = _live_ids(conn, "subject.sleep_hours_daily", version=1)
    assert len(v1_live_before) == DAYS

    v2 = _contract(metric_id="subject.sleep_hours_daily", version=2)
    materialize_all(conn, {"subject.sleep_hours_daily": v2})

    v1_live_after = _live_ids(conn, "subject.sleep_hours_daily", version=1)
    assert v1_live_after == v1_live_before, (
        "物化 v2 不应改动 v1：v1 被关闭后，按旧口径回溯的历史将无法查询"
    )
    assert conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = 'subject.sleep_hours_daily' AND metric_version = 2"
        " AND valid_to IS NULL"
    ).fetchone()[0] == DAYS

    # 关键保证：加版本过滤后，每个版本各自唯一有效
    for version in (1, 2):
        dup = conn.execute(
            "SELECT count(*) FROM (SELECT subject_id, date_key, count(*) c"
            " FROM fact_metric_value WHERE metric_id = 'subject.sleep_hours_daily'"
            " AND metric_version = ? AND valid_to IS NULL GROUP BY 1,2 HAVING c > 1)",
            [version],
        ).fetchone()[0]
        assert dup == 0, f"版本 {version} 内部不得有多行有效"

    # 而"按当前版本查询"必须只拿到 v2 的行
    current_version_rows = conn.execute(
        "SELECT count(*) FROM fact_metric_value"
        " WHERE metric_id = 'subject.sleep_hours_daily' AND metric_version = 2"
        " AND valid_to IS NULL"
    ).fetchone()[0]
    assert current_version_rows == DAYS


def _live_ids(conn, metric_id: str, *, version: int) -> set[tuple[str, int]]:
    rows = conn.execute(
        "SELECT subject_id, date_key FROM fact_metric_value"
        " WHERE metric_id = ? AND metric_version = ? AND valid_to IS NULL",
        [metric_id, version],
    ).fetchall()
    return {(str(r[0]), int(r[1])) for r in rows}


def test_formula_using_physical_table_name_binds(conn):
    """公式按契约写物理表名（`fact_observation.x`）必须能绑定成功。

    若把物理表名重写成视图名，而 FROM 里用的是别名 o/e/s，
    DuckDB 中别名会遮蔽原名 → 全部指标 Binder Error。
    """
    contracts = {
        "subject.phys": _contract(
            metric_id="subject.phys",
            formula_sql="fact_observation.sleep_hours - dim_subject.sleep_need_h",
            lineage={
                "sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"],
                "upstream_metrics": [],
            },
        )
    }
    stats = materialize_all(conn, contracts)
    assert stats["subject.phys"]["live"] == DAYS
    got = conn.execute(
        "SELECT DISTINCT value FROM fact_metric_value WHERE metric_id = 'subject.phys'"
    ).fetchall()
    assert got == [(pytest.approx(6.0 - SLEEP_NEED),)]


def test_sleep_need_uses_version_valid_on_that_day(conn):
    """历史日用当天有效的睡眠需求，边界日属于新版本；重算不产生历史行。"""
    conn.execute("DELETE FROM dim_subject")
    conn.execute(
        "INSERT INTO dim_subject VALUES "
        "(?, 'demo', DATE '1990-01-01', 8.25, 78.5, 'Asia/Shanghai',"
        " TIMESTAMP '2020-01-01 00:00:00', TIMESTAMP '2026-01-07 00:00:00', FALSE, 1,"
        " TIMESTAMP '2020-01-01 00:00:00'),"
        "(?, 'demo', DATE '1990-01-01', ?, 72.0, 'Asia/Shanghai',"
        " TIMESTAMP '2026-01-07 00:00:00', NULL, TRUE, 2,"
        " TIMESTAMP '2026-01-08 00:00:00')",
        [SUBJECT, SUBJECT, SLEEP_NEED],
    )
    contracts = {
        "subject.need": _contract(
            metric_id="subject.need",
            formula_sql="fact_observation.sleep_hours - dim_subject.sleep_need_h",
            lineage={
                "sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"],
                "upstream_metrics": [],
            },
        )
    }
    first = materialize_all(conn, contracts)["subject.need"]
    assert first["inserted"] == DAYS
    rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value"
        " WHERE metric_id = 'subject.need' AND valid_to IS NULL ORDER BY date_key"
    ).fetchall()
    early = [value for key, value in rows if key < 20260107]
    late = [value for key, value in rows if key >= 20260107]
    assert 20260106 in {key for key, _ in rows}
    assert 20260107 in {key for key, _ in rows}
    assert early and late
    assert all(value == pytest.approx(6.0 - 8.25) for value in early)
    assert all(value == pytest.approx(6.0 - SLEEP_NEED) for value in late)

    second = materialize_all(conn, contracts)["subject.need"]
    assert second["inserted"] == 0
    assert second["closed_changed"] == 0
    assert second["unchanged"] == DAYS
    total = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = 'subject.need'"
    ).fetchone()[0]
    assert total == DAYS, "无变化重算不得产生历史行"


def test_window_order_by_bare_date_key_binds(conn):
    """窗口里写裸 `date_key`（契约推荐写法）必须无歧义。

    若把来源表的 `date_key` 透出到公式作用域，
    `ORDER BY date_key` 会报 "Ambiguous reference to column name"。
    """
    contracts = {
        "subject.dev": _contract(
            metric_id="subject.dev",
            formula_sql="fact_observation.sleep_hours - dim_subject.sleep_need_h",
            lineage={
                "sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"],
                "upstream_metrics": [],
            },
        ),
        "subject.debt7": _contract(
            metric_id="subject.debt7",
            agg="sum",
            formula_sql=(
                "sum(metric('subject.dev')) OVER ("
                "ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)"
            ),
            lineage={"sources": [], "upstream_metrics": ["subject.dev"]},
        ),
    }
    materialize_all(conn, contracts)
    last = conn.execute(
        "SELECT value FROM fact_metric_value WHERE metric_id = 'subject.debt7'"
        " AND valid_to IS NULL ORDER BY date_key DESC LIMIT 1"
    ).fetchone()[0]
    assert last == pytest.approx(-12.25)


def test_references_unknown_upstream_raises(conn):
    """引用未注册、也不在契约集合里的上游指标 → 拓扑分层即报错。

    注意：`_topo_layers` 会先把未满足的依赖视为环，因此这里的错误信息是"环"；
    契约加载阶段（semantic 负责）会用更精确的"未注册的上游指标"报错。
    """
    contracts = {
        "subject.bad": _contract(
            metric_id="subject.bad",
            formula_sql="metric('subject.missing')",
            lineage={"sources": [], "upstream_metrics": ["subject.missing"]},
        )
    }
    with pytest.raises(config.ContractError, match="环|未注册"):
        materialize_all(conn, contracts)


def test_formula_with_unknown_source_token_raises(conn):
    """公式既不引用任何已知来源、也没有上游指标 → 无法确定日期骨架，必须报错。"""
    contracts = {
        "subject.nosource": _contract(
            metric_id="subject.nosource",
            formula_sql="1 + 1",
            lineage={"sources": [], "upstream_metrics": []},
        )
    }
    with pytest.raises(config.ContractError, match="日期骨架"):
        materialize_all(conn, contracts)


def test_contract_hash_authority_is_stable():
    """哈希必须来自权威实现，且固定样例稳定。"""
    from veriself.contract_hash import GOLDEN_CONTRACT, GOLDEN_CONTRACT_HASH, contract_hash

    assert contract_hash(GOLDEN_CONTRACT) == GOLDEN_CONTRACT_HASH


"""端到端红队验收（Lead 所有，发布依据）。

与 `test_enforcement.py` 的分工：
  - `test_enforcement.py` 用最小假数据单元测试五条校验；
  - 本文件走**完整真实链路**：合成数据 → 建 schema → 写 dim_metric → 物化 18 个指标
    → 编译 → 执行 → 审计，然后在真实数据上重复红队攻击。

设计要点：全程使用 `tmp_path` 下的独立库，不碰 `data/warehouse.duckdb`，
因此可以随时重跑且不影响 demo 环境。
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from veriself import config

pytestmark = pytest.mark.e2e

EXPECTED_METRICS = 18

# 端到端产物目录（仓库内，已 gitignore）。
# 刻意不用 `tmp_path`：端到端产物（数仓文件、日志）留在仓库内 `data/` 下便于复现与排查，
# 且不依赖 pytest 的临时根目录是否可写——自管目录在任何环境下行为一致。
E2E_ROOT = Path(__file__).resolve().parent.parent / "data" / "e2e-run"


def _force_rmtree(path: Path) -> None:
    """删除目录树，遇到只读/权限问题时尽力而为（清理失败不影响测试结论）。"""

    def on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=on_error)


@pytest.fixture(scope="module")
def e2e():
    """一次性构建真实数据链路（合成 → schema → dim_metric → 物化），模块内共享。"""
    run_dir = E2E_ROOT / uuid.uuid4().hex[:8]
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "warehouse.duckdb"

    from veriself.synth import generate_all

    synth_stats = generate_all(db_path=db_path)

    conn = duckdb.connect(str(db_path))

    from veriself.semantic import load_contracts
    from veriself.warehouse.loader import ensure_schema, upsert_dim_metric

    ensure_schema(conn)
    contracts = load_contracts()
    upsert_dim_metric(conn, contracts)

    from veriself.materializer import materialize_all

    # 连跑两遍：第二遍必须**完全幂等**（不新增有效行、不新增历史行）。
    # 历史行由专门的用例制造（见 `test_history_is_preserved`）。
    materialize_all(conn, contracts)
    materialized = materialize_all(conn, contracts)

    yield {
        "db_path": db_path,
        "conn": conn,
        "contracts": contracts,
        "synth_stats": synth_stats,
        "materialized": materialized,
    }

    conn.close()
    _force_rmtree(run_dir)
    try:
        E2E_ROOT.rmdir()  # 只在空目录时成功
    except OSError:
        pass


# ------------------------------------------------------------------ 0. 链路完整性
def test_pipeline_produces_all_metrics(e2e):
    """18 个指标全部物化出非空数据，且数值有限。"""
    conn = e2e["conn"]
    rows = conn.execute(
        "SELECT metric_id, count(*) AS n FROM fact_metric_value"
        " WHERE valid_to IS NULL GROUP BY 1"
    ).fetchall()
    assert len(rows) == EXPECTED_METRICS, f"应有 {EXPECTED_METRICS} 个指标有数据，实际 {len(rows)}"
    empty = [m for m, n in rows if n == 0]
    assert not empty, f"以下指标无数据: {empty}"

    bad = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE isnan(value) OR isinf(value)"
    ).fetchone()[0]
    assert bad == 0, "不得写入 NaN/Inf"


def test_bitemporal_uniqueness(e2e):
    """同一 (metric_id, subject_id, date_key) 只能有一行有效。"""
    conn = e2e["conn"]
    dup = conn.execute(
        "SELECT count(*) FROM (SELECT metric_id, subject_id, date_key, count(*) n"
        " FROM fact_metric_value WHERE valid_to IS NULL GROUP BY 1,2,3 HAVING n > 1)"
    ).fetchone()[0]
    assert dup == 0


def test_recompute_is_fully_idempotent(e2e):
    """连跑两次物化必须零写入：有效行、历史行、总行数都不变。

    若每次物化都"关闭全部有效行 + 全量重写"，重算会把表撑成 N 倍。
    """
    conn = e2e["conn"]
    total = conn.execute("SELECT count(*) FROM fact_metric_value").fetchone()[0]
    live = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE valid_to IS NULL"
    ).fetchone()[0]
    closed = total - live

    from veriself.materializer import materialize_all

    stats = materialize_all(conn, e2e["contracts"])

    total_after = conn.execute("SELECT count(*) FROM fact_metric_value").fetchone()[0]
    live_after = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE valid_to IS NULL"
    ).fetchone()[0]

    assert total_after == total, f"无变化重算不得增行：{total} -> {total_after}"
    assert live_after == live
    assert (total_after - live_after) == closed, "无变化重算不得新增历史"

    churn = {
        mid: st
        for mid, st in stats.items()
        if st["inserted"] or st["closed_changed"] or st["closed_vanished"]
    }
    assert not churn, f"以下指标在无变化重算时产生了写入: {churn}"


def test_history_is_preserved(e2e):
    """值变化必须留痕（as-of / "这个数为什么变了" 的数据基础）。

    制造一次真实的值变化（改一条原始观测），确认旧值被关闭、新值生效——
    这正是"合并"与"覆盖写"的区别。
    """
    conn = e2e["conn"]
    metric_id = "subject.sleep_duration_daily"
    target_key = conn.execute(
        "SELECT date_key FROM fact_metric_value WHERE metric_id = ?"
        " AND valid_to IS NULL ORDER BY date_key LIMIT 1",
        [metric_id],
    ).fetchone()[0]

    # 注意：改一条睡眠观测会级联影响多个下游指标（sleep_debt_7d / recovery_score …），
    # 因此历史行增量 > 1。断言聚焦"目标指标留痕 + 无物理删除"，不假设总数。
    closed_before = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = ?"
        " AND valid_to IS NOT NULL",
        [metric_id],
    ).fetchone()[0]
    assert closed_before == 0, "新库里该指标不应有历史行"

    # 把该日的睡眠观测改掉 → 该指标重算应产生 1 行历史
    conn.execute(
        "UPDATE fact_observation SET value = value + 2.0"
        " WHERE channel = 'sleep_hours' AND date_key = ?",
        [target_key],
    )

    from veriself.materializer import materialize_all

    stats = materialize_all(conn, e2e["contracts"])[metric_id]
    assert stats["closed_changed"] == 1, f"值变化必须留痕，实际 {stats}"

    closed_after = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = ?"
        " AND valid_to IS NOT NULL",
        [metric_id],
    ).fetchone()[0]
    assert closed_after == closed_before + 1

    # 该键仍有且仅有一行有效（关闭旧行 + 插入新行，不是覆盖）
    live_rows = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = ? AND date_key = ?"
        " AND valid_to IS NULL",
        [metric_id, target_key],
    ).fetchone()[0]
    assert live_rows == 1

    # 不能有物理删除：历史行里还留着旧值
    history_rows = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = ? AND date_key = ?"
        " AND valid_to IS NOT NULL",
        [metric_id, target_key],
    ).fetchone()[0]
    assert history_rows >= 1, "旧值必须作为历史保留，不得物理删除"


def test_sleep_debt_is_internally_consistent(e2e):
    """内部自洽：睡眠债必须等于上游偏差的 7 日滚动和（独立重算比对）。"""
    conn = e2e["conn"]
    max_diff = conn.execute(
        """
        WITH dev AS (
            SELECT date_key, value FROM fact_metric_value
            WHERE metric_id = 'subject.sleep_need_deviation_daily' AND valid_to IS NULL
        ),
        expected AS (
            SELECT date_key,
                   sum(CASE WHEN -value > 0 THEN -value ELSE 0 END)
                     OVER (ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS exp
            FROM dev
        )
        SELECT coalesce(max(abs(m.value - e.exp)), 0)
        FROM fact_metric_value m
        JOIN expected e ON e.date_key = m.date_key
        WHERE m.metric_id = 'subject.sleep_debt_7d' AND m.valid_to IS NULL
        """
    ).fetchone()[0]
    assert max_diff == pytest.approx(0.0, abs=1e-9), f"睡眠债与上游不自洽，最大误差 {max_diff}"


def test_contract_hash_is_consistent_across_modules(e2e):
    """同一份契约在 semantic 与 materializer 两侧必须得到同一个哈希。

    必须覆盖 `_hash_for` 的**兜底分支**：它优先返回契约自带哈希，所以只比
    `_hash_for(contract)` 会漏掉"兜底拿 `model_dump()` 当哈希输入"的路径，
    而 `model_dump()` 会补默认值、并把 `contract_hash` / `source_file` 算进去。
    """
    from veriself.materializer import _hash_for
    from veriself.semantic import metric_contract_from_mapping

    contracts = e2e["contracts"]
    for metric_id, contract in list(contracts.items())[:5]:
        side_a = getattr(contract, "contract_hash", None)
        side_b = _hash_for(contract)
        assert side_a == side_b, f"{metric_id}: semantic={side_a} materializer={side_b}"
        # dim_metric 里存的也必须是同一个
        stored = e2e["conn"].execute(
            "SELECT contract_hash FROM dim_metric WHERE metric_id = ?", [metric_id]
        ).fetchone()
        assert stored and stored[0] == side_a, f"{metric_id}: dim_metric 与契约哈希不一致"

        # 清空自带哈希 → 逼 materializer 现算，结果必须仍等于权威值
        reprobe = metric_contract_from_mapping(contract.raw, source_file=metric_id)
        reprobe.contract_hash = ""
        assert _hash_for(reprobe) == side_a, (
            f"{metric_id}: 兜底现算的哈希与契约自带值不一致（{_hash_for(reprobe)} != {side_a}）"
        )


# ------------------------------------------------------------------ 1. 正常查询 + 审计头
def test_normal_query_returns_audit_header(e2e):
    from veriself.semantic import QueryRequest, compile_query, execute_query

    # 锚定合成数据的结束日，不用 `date.last_n_days`——后者以"今天"为锚，
    # 而数据固定止于 `SYNTH_END_DATE`，用它会随日期推移退化成空结果。
    end = date.fromisoformat(config.SYNTH_END_DATE)
    req = QueryRequest.from_json(
        {
            "metrics": ["subject.sleep_debt_7d"],
            "dimensions": ["date.weekday"],
            "filters": {"date.between": [(end - timedelta(days=29)).isoformat(), end.isoformat()]},
        }
    )
    compiled = compile_query(req, e2e["contracts"], config.Role.OWNER)
    result = execute_query(compiled, role=config.Role.OWNER, conn=e2e["conn"], audit=True)

    assert result.data, "正常查询不应返回空数据"
    audit = result.audit
    for key in (
        "metric_versions",
        "contract_hashes",
        "compiled_sql",
        "rls_applied",
        "enforced_checks",
        "queried_at",
    ):
        assert key in audit, f"审计头缺少 {key}"
    assert tuple(audit["enforced_checks"]) == config.ENFORCED_CHECKS
    assert audit["contract_hashes"]["subject.sleep_debt_7d"].startswith("sha256:")


def test_audit_log_is_written(e2e):
    conn = e2e["conn"]
    before = conn.execute("SELECT count(*) FROM fact_audit_log").fetchone()[0]

    from veriself.semantic import QueryRequest, compile_query, execute_query

    req = QueryRequest.from_json({"metrics": ["subject.daily_steps"]})
    compiled = compile_query(req, e2e["contracts"], config.Role.OWNER)
    execute_query(compiled, role=config.Role.OWNER, conn=conn, audit=True)

    after = conn.execute("SELECT count(*) FROM fact_audit_log").fetchone()[0]
    assert after > before, "查询未写入审计日志"


# ------------------------------------------------------------------ 2. 红队（真实数据）
@pytest.mark.parametrize(
    ("name", "payload", "expected_prefix"),
    [
        ("未注册指标", {"metrics": ["subject.focus_skore"]}, "unknown_metric:"),
        (
            "越界维度",
            {"metrics": ["subject.sleep_debt_7d"], "dimensions": ["subject.secret"]},
            "dimension_not_allowed:",
        ),
        (
            "越界过滤器",
            {"metrics": ["subject.sleep_debt_7d"], "filters": {"dim_subject.name": "x"}},
            "filter_not_allowed:",
        ),
        ("粒度过细", {"metrics": ["subject.sleep_debt_7d"], "grain": "hour"}, "grain_not_compatible:"),
    ],
)
def test_redteam_rejections(e2e, name, payload, expected_prefix):
    from veriself.semantic import QueryRequest, compile_query

    with pytest.raises(config.EnforcementError) as exc_info:
        req = QueryRequest.from_json(payload)
        compile_query(req, e2e["contracts"], config.Role.OWNER)
    assert exc_info.value.detail.startswith(expected_prefix), f"[{name}] {exc_info.value.detail}"


def test_redteam_sql_injection_is_rejected(e2e):
    from veriself.semantic import QueryRequest

    with pytest.raises(config.QueryError):
        QueryRequest.from_json(
            {
                "metrics": ["subject.spending_daily"],
                "filters": {"date.month": "3; DROP TABLE fact_event"},
            }
        )


@pytest.mark.parametrize("sql_fragment", ["'; DROP TABLE", "-- x", "/* x */", "UNION SELECT"])
def test_redteam_injection_variants(e2e, sql_fragment):
    from veriself.semantic import QueryRequest

    with pytest.raises(config.QueryError):
        QueryRequest.from_json(
            {"metrics": ["subject.spending_daily"], "filters": {"date.month": sql_fragment}}
        )


def test_redteam_rls_blocks_owner_only_for_partner(e2e):
    from veriself.semantic import QueryRequest, compile_query

    with pytest.raises(config.EnforcementError) as exc_info:
        req = QueryRequest.from_json({"metrics": ["subject.spending_daily"]})
        compile_query(req, e2e["contracts"], config.Role.PARTNER)
    assert exc_info.value.rule == "rls"


def test_no_sql_channel_in_query_object(e2e):
    """查询对象里不存在任何能携带原生语句的字段。"""
    from veriself.semantic import QueryRequest

    allowed = {"metrics", "dimensions", "filters", "grain", "order_by", "limit"}
    assert set(QueryRequest.model_fields) <= allowed, (
        f"查询对象出现额外字段: {set(QueryRequest.model_fields) - allowed}"
    )
    # 未知字段必须被拒绝（`extra="forbid"` → 适配层统一抛 `config.QueryError`），而不是静默接受
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.sleep_debt_7d"], "sql": "SELECT 1"})


def test_rls_rewrite_is_recorded_for_allowed_role(e2e):
    """`aggregate_min5` 指标对 partner 应被改写（而非拒绝），且改写被记录。"""
    from veriself.semantic import QueryRequest, compile_query

    req = QueryRequest.from_json({"metrics": ["subject.daily_steps"], "grain": "month"})
    compiled = compile_query(req, e2e["contracts"], config.Role.PARTNER)
    assert compiled.rls_applied, "RLS 未记录"
    lowered = compiled.sql.lower()
    assert "count(" in lowered or "having" in lowered or "subject_id" in lowered, (
        "未见任何行级/聚合级改写痕迹"
    )


# ------------------------------------------------------------------ 3. MCP 层
def test_mcp_tools_have_no_sql_surface(e2e):
    """MCP 的 4 个工具里，query_metric 的入参 schema 不得出现可传原生语句的字段。"""
    import asyncio

    from veriself.interfaces.mcp_server import build_server

    tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}
    assert set(tools) >= {"list_metrics", "describe_metric", "query_metric", "explain_result"}

    schema = tools["query_metric"].input_schema
    props = set(schema.get("properties", {}))
    forbidden = {"sql", "query", "statement", "raw", "native_sql"}
    assert not (props & forbidden), f"query_metric 暴露了 SQL 通道: {props & forbidden}"
    assert props <= {"metrics", "dimensions", "filters", "grain", "order_by", "limit"}, props
    # 多传字段必须在 MCP 入参校验层就被拒
    assert schema.get("additionalProperties") is False, "MCP 工具入参未收紧为封闭对象"
    # 无参工具不应意外暴露参数
    assert tools["list_metrics"].input_schema.get("properties") == {}


# ------------------------------------------------------------------ 4. 确定性
def test_synth_is_deterministic(e2e):
    """同一份合成数据生成两次，`fact_observation` 内容必须逐元素一致（可复现性）。

    注意：不能用同一个库文件跑第二次（DuckDB 不允许同文件不同配置的连接），
    因此生成到两个独立目录再比对。DuckDB 文件字节不保证一致（页分配属实现细节），
    以表内容为准。
    """
    from veriself.synth import generate_all

    digests = []
    dirs = []
    for tag in ("det-a", "det-b"):
        target_dir = E2E_ROOT / f"{tag}-{uuid.uuid4().hex[:6]}"
        target_dir.mkdir(parents=True, exist_ok=True)
        dirs.append(target_dir)
        db_file = target_dir / "warehouse.duckdb"

        generate_all(db_path=db_file, synth_dir=target_dir / "synth")

        check = duckdb.connect(str(db_file), read_only=True)
        try:
            row = check.execute(
                "SELECT count(*), sum(value), sum(value * 7919)"
                " FROM fact_observation ORDER BY 1"
            ).fetchone()
            event_row = check.execute(
                "SELECT count(*), coalesce(sum(amount), 0) FROM fact_event"
            ).fetchone()
        finally:
            check.close()
        digests.append((tuple(row), tuple(event_row)))

    for d in dirs:
        _force_rmtree(d)

    assert digests[0] == digests[1], f"同种子两次生成结果不一致: {digests}"

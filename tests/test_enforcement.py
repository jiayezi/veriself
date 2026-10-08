"""`semantic` 层验收测试（IFACE-v1 第 2/3/4/5/7 节）。

自包含：用 `contracts_dir` 自建最小契约、用内存 DuckDB 自建最小星型模型，
**不依赖** `metrics/*.yml`、`synth`、`warehouse` 的产物（并行开发期间也能全绿）。

红队六项（可 `-k redteam` 单独跑）：
1. 未注册指标 → `unknown_metric:`
2. 越界维度/过滤器 → `dimension_not_allowed:` / `filter_not_allowed:`
3. 粒度不兼容（日指标求分钟）→ `grain_not_compatible:`
4. 字段值含 `; DROP TABLE` → `QueryError`
5. `partner` 查 `owner_only` → `rule == "rls"`
6. 手工构造的越权 SQL（子查询/未声明表）→ `ast_violation:`
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import sys
import tempfile
import types
import uuid
from pathlib import Path

import duckdb
import pytest
import yaml

from veriself import config
from veriself.contract_hash import GOLDEN_CONTRACT, GOLDEN_CONTRACT_HASH, contract_hash
from veriself.semantic import (
    AS_OF_DEFINITION,
    CompiledQuery,
    QueryRequest,
    check_ast_join_path,
    compile_query,
    describe_metric,
    enforcement,
    execute_query,
    list_metrics,
    load_contracts,
    metric_contract_from_mapping,
    validate_ast,
)
from veriself.semantic import compiler as compiler_mod

# ------------------------------------------------------------------ 最小数仓
_DIM_CONTEXT_WITH_KEY = """
CREATE TABLE dim_context (
    context_sk BIGINT, context_id VARCHAR, date_key INTEGER, is_travel BOOLEAN,
    is_illness BOOLEAN, location_type VARCHAR
);
"""
#: 冻结 DDL 的 dim_context：没有任何日期/主体键（用于 fail-closed 回归测试）
_DIM_CONTEXT_WITHOUT_KEY = """
CREATE TABLE dim_context (
    context_sk BIGINT, context_id VARCHAR, is_travel BOOLEAN, is_illness BOOLEAN,
    location_type VARCHAR
);
"""
DDL = """
CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY, date DATE, year INTEGER, quarter INTEGER, month INTEGER,
    week INTEGER, day_of_week INTEGER, weekday_name VARCHAR, is_weekend BOOLEAN, is_holiday BOOLEAN
);
CREATE TABLE dim_subject (
    subject_sk BIGINT, subject_id VARCHAR, name VARCHAR, birth_date DATE, sleep_need_h DOUBLE,
    base_weight_kg DOUBLE, timezone VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP,
    is_current BOOLEAN, version INTEGER, recorded_at TIMESTAMP
);
CREATE TABLE fact_metric_value (
    metric_id VARCHAR, subject_id VARCHAR, date_key INTEGER, value DOUBLE, metric_version INTEGER,
    contract_hash VARCHAR, computed_at TIMESTAMP, valid_from TIMESTAMP, valid_to TIMESTAMP,
    PRIMARY KEY (metric_id, subject_id, date_key, valid_from)
);
CREATE TABLE fact_audit_log (
    audit_id BIGINT PRIMARY KEY, queried_at TIMESTAMP NOT NULL, actor_role VARCHAR NOT NULL,
    request_json VARCHAR NOT NULL, compiled_sql VARCHAR, metric_versions VARCHAR,
    contract_hashes VARCHAR, rls_applied VARCHAR, checks_passed VARCHAR, outcome VARCHAR NOT NULL
);
"""

HASH = "sha256:test"
JAN1 = dt.date(2026, 1, 1)


def _key(day: dt.date) -> int:
    return int(day.strftime("%Y%m%d"))


def _insert(conn, metric_id, subject_id, day, value, version=1, valid_to=None):
    conn.execute(
        "INSERT INTO fact_metric_value VALUES (?,?,?,?,?,?, now(), now(), ?)",
        [metric_id, subject_id, _key(day), float(value), version, HASH, valid_to],
    )


def _make_conn(with_context_key: bool = True) -> duckdb.DuckDBPyConnection:
    """内存星型模型 + 确定性数据。`with_context_key=False` 模拟冻结 DDL 的 dim_context（无 date_key）。"""
    conn = duckdb.connect(":memory:")
    context_ddl = _DIM_CONTEXT_WITH_KEY if with_context_key else _DIM_CONTEXT_WITHOUT_KEY
    conn.execute(DDL + context_ddl)

    day = dt.date(2026, 1, 1)
    rows = []
    for _ in range(90):
        rows.append(
            (
                _key(day), day, day.year, (day.month - 1) // 3 + 1, day.month,
                int(day.strftime("%W")), day.isoweekday(), day.strftime("%A"),
                day.isoweekday() >= 6, False,
            )
        )
        day += dt.timedelta(days=1)
    conn.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    # dim_context：只有 1 月 2 日标记为旅行
    if with_context_key:
        conn.execute("INSERT INTO dim_context VALUES (1, 'C1', ?, TRUE, FALSE, 'other')", [_key(dt.date(2026, 1, 2))])
        conn.execute("INSERT INTO dim_context VALUES (2, 'C2', ?, FALSE, FALSE, 'home')", [_key(JAN1)])

    # dim_subject：SCD2，S001 有两条历史（只有 is_current=TRUE 那条该被用）
    subjects = []
    for index in range(1, 6):
        subjects.append((index, f"S00{index}", "demo", dt.date(1990, 1, 1), 7.75, 70.0,
                         "Asia/Shanghai", dt.datetime(2026, 1, 1), None, True, 1, dt.datetime(2026, 1, 1)))
    subjects.append((99, "S001", "old", dt.date(1990, 1, 1), 99.0, 70.0,
                     "Asia/Shanghai", dt.datetime(2020, 1, 1), dt.datetime(2025, 12, 31), False, 1,
                     dt.datetime(2020, 1, 1)))
    conn.executemany("INSERT INTO dim_subject VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", subjects)

    # 1) 日粒度 mean（owner_only）；S002 有巨值，用于验证 owner 行级过滤
    for offset, value in enumerate([6.0, 7.0, 8.0, 6.0, 7.0]):
        _insert(conn, "subject.sleep_daily", "S001", JAN1 + dt.timedelta(days=offset), value)
        _insert(conn, "subject.sleep_daily", "S002", JAN1 + dt.timedelta(days=offset), 100.0)
    # 2) 日粒度 sum
    for offset, value in enumerate([10.0, 20.0, 30.0]):
        _insert(conn, "subject.spending_daily", "S001", JAN1 + dt.timedelta(days=offset), value)
    # 3) 月粒度 sum：date_key = 桶内最后一个有数据的真实日期（Lead materializer 约定）
    _insert(conn, "subject.spending_monthly", "S001", dt.date(2026, 1, 31), 100.0)
    _insert(conn, "subject.spending_monthly", "S001", dt.date(2026, 2, 28), 200.0)
    # 4) 周粒度 mean
    _insert(conn, "subject.focus_weekly", "S001", dt.date(2026, 1, 4), 50.0)
    _insert(conn, "subject.focus_weekly", "S001", dt.date(2026, 1, 11), 70.0)
    # 5) last / max / min 三兄弟：同一批数据（5,1,3）区分 last 与 max
    for offset, value in enumerate([5.0, 1.0, 3.0]):
        day = JAN1 + dt.timedelta(days=offset)
        _insert(conn, "subject.last_demo", "S001", day, value)
        _insert(conn, "subject.max_demo", "S001", day, value)
        _insert(conn, "subject.min_demo", "S001", day, value)
    # 6) aggregate_min5：5 个主体
    for index, value in enumerate([10.0, 20.0, 30.0, 40.0, 50.0], start=1):
        _insert(conn, "subject.steps_daily", f"S00{index}", JAN1, value)
    # 7) no_pii
    _insert(conn, "subject.pii_demo", "S001", JAN1, 42.0)
    # 8) 版本：v2 当前（5.0）、v2 历史（99.0，valid_to 非 NULL）、v1 残留（100.0，valid_to NULL）
    _insert(conn, "subject.versioned", "S001", JAN1, 5.0, version=2)
    _insert(conn, "subject.versioned", "S001", JAN1, 99.0, version=2, valid_to=dt.datetime(2026, 2, 1))
    _insert(conn, "subject.versioned", "S001", JAN1, 100.0, version=1)
    # 9) dim_subject 维度（非 PII）
    _insert(conn, "subject.need_demo", "S001", JAN1, 6.0)
    # 10) dim_date 里不存在的 date_key（inner join 必须丢弃）
    _insert(conn, "subject.orphan", "S001", dt.date(2099, 12, 31), 777.0)
    # 11) is_travel 过滤器
    _insert(conn, "subject.is_travel_demo", "S001", JAN1, 1.0)
    _insert(conn, "subject.is_travel_demo", "S001", dt.date(2026, 1, 2), 2.0)
    return conn


@pytest.fixture()
def conn():
    connection = _make_conn()
    yield connection
    connection.close()


@contextlib.contextmanager
def _temp_dir(label: str):
    """测试用临时目录，退出时清理。

    优先 `tempfile.mkdtemp()`；若系统临时目录不可用（只读/受限环境），
    退回到"仓库内 `data/` 下的自管目录"，保证测试在任何环境下都能跑。
    """
    holder: Path | None = None
    try:
        holder = Path(tempfile.mkdtemp(prefix=f"veriself-{label}-"))
        (holder / ".probe").write_text("ok", encoding="utf-8")
    except OSError:
        if holder is not None:
            shutil.rmtree(holder, ignore_errors=True)
        holder = Path(__file__).resolve().parent / ".veriself-tmp" / f"{label}-{uuid.uuid4().hex[:8]}"
        holder.mkdir(parents=True, exist_ok=True)
    try:
        yield holder
    finally:
        shutil.rmtree(holder, ignore_errors=True)


@pytest.fixture()
def contracts_dir():
    """空契约目录（自建最小契约，不依赖 `metrics/`）。"""
    with _temp_dir("contracts") as directory:
        yield directory


# ------------------------------------------------------------------ 最小契约
def _contract(**over):
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
        "allowed_dimensions": ["date.weekday", "date.month"],
        "allowed_filters": ["date.between", "date.last_n_days", "date.month"],
        "rls_policy": "owner_only",
        "formula_sql": "o.sleep_hours",
        "lineage": {"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        "deprecation": {"replaced_by": None, "sunset_at": None},
    }
    base.update(over)
    return metric_contract_from_mapping(base, source_file="<test>")


@pytest.fixture()
def contracts():
    items = [
        _contract(metric_id="subject.sleep_daily", agg="mean"),
        _contract(metric_id="subject.spending_daily", agg="sum", unit="currency"),
        _contract(metric_id="subject.spending_monthly", agg="sum", unit="currency", grain="month"),
        _contract(metric_id="subject.focus_weekly", agg="mean", unit="score", grain="week",
                  rls_policy="aggregate_min5"),
        _contract(metric_id="subject.last_demo", agg="last"),
        _contract(metric_id="subject.max_demo", agg="max"),
        _contract(metric_id="subject.min_demo", agg="min"),
        _contract(metric_id="subject.steps_daily", agg="mean", unit="count", rls_policy="aggregate_min5"),
        _contract(metric_id="subject.pii_demo", agg="mean", rls_policy="no_pii",
                  allowed_dimensions=["subject.timezone"]),
        _contract(metric_id="subject.versioned", version=2, agg="mean"),
        _contract(metric_id="subject.need_demo", allowed_dimensions=["subject.sleep_need_h"]),
        _contract(metric_id="subject.orphan", agg="mean"),
        _contract(metric_id="subject.is_travel_demo", allowed_filters=["date.between", "dim_context.is_travel"]),
        _contract(metric_id="subject.bad_dim", allowed_dimensions=["widget.foo"]),
        _contract(metric_id="subject.bad_column", allowed_dimensions=["date.not_a_col"]),
        _contract(metric_id="subject.bad_filter", allowed_filters=["metric.value"]),
        _contract(metric_id="subject.deprecated_demo", status="deprecated",
                  deprecation={"replaced_by": "subject.sleep_daily"}),
    ]
    return {item.metric_id: item for item in items}


def _compile(payload, contracts, role=config.Role.OWNER):
    return compile_query(QueryRequest.from_json(payload), contracts, role=role)


def _run(payload, contracts, conn, role=config.Role.OWNER):
    return execute_query(_compile(payload, contracts, role=role), role=role, conn=conn, audit=False)


# ================================================================== contract_hash
def test_contract_hash_matches_golden():
    """固定样例必须复现 Lead 的权威值（两个模块不能各算一份）。"""
    assert GOLDEN_CONTRACT_HASH == "sha256:015c2525e5106928"
    assert contract_hash(GOLDEN_CONTRACT) == GOLDEN_CONTRACT_HASH


def test_contract_hash_fixed_sample_and_loader_consistency(contracts_dir):
    """固定 YAML → 固定哈希；loader 的 contract_hash 必须等于对原始 YAML dict 调用权威函数。"""
    (contracts_dir / "a.yml").write_text(SAMPLE_YAML, encoding="utf-8")
    (contracts_dir / "b.yml").write_text(UPSTREAM_YAML, encoding="utf-8")
    expected = "sha256:61fc70bf013f7cab"
    raw = yaml.safe_load(SAMPLE_YAML)
    assert contract_hash(raw) == expected
    loaded = load_contracts(contracts_dir)
    assert loaded["subject.sleep_debt_7d"].contract_hash == expected
    assert loaded["subject.sleep_debt_7d"].contract_hash == contract_hash(raw)


# ================================================================== 契约加载五条规则
def _write(contracts_dir, name, **over):
    base = {
        "metric_id": "subject.a", "version": 1, "status": "active", "owner": "t",
        "display_name": "a", "unit": "hour", "direction": "neutral", "agg": "mean", "grain": "day",
        "allowed_dimensions": [], "allowed_filters": [], "rls_policy": "owner_only",
        "formula_sql": "o.sleep_hours", "lineage": {"sources": ["fact_observation.sleep_hours"]},
    }
    base.update(over)
    (contracts_dir / name).write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")


def test_load_contracts_duplicate_metric_id(contracts_dir):
    _write(contracts_dir, "a.yml", metric_id="subject.dup")
    _write(contracts_dir, "b.yml", metric_id="subject.dup")
    with pytest.raises(config.ContractError, match="重复"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_undeclared_source(contracts_dir):
    _write(contracts_dir, "a.yml", formula_sql="o.not_declared")
    with pytest.raises(config.ContractError, match="lineage.sources"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_undeclared_upstream(contracts_dir):
    _write(contracts_dir, "a.yml", formula_sql="sum(metric('subject.missing'))",
           lineage={"sources": [], "upstream_metrics": []})
    with pytest.raises(config.ContractError, match="upstream_metrics"):
        load_contracts(contracts_dir)


def test_load_contracts_accepts_alias_formula_like_materializer(contracts_dir):
    """materializer 用 `o.xxx` / `s.xxx` 别名写法，必须被接受。"""
    _write(contracts_dir, "a.yml", metric_id="subject.alias_form",
           formula_sql="o.sleep_hours - s.sleep_need_h",
           lineage={"sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"]})
    contracts = load_contracts(contracts_dir)
    assert "subject.alias_form" in contracts


def test_load_contracts_ignores_metric_calls_in_comments(contracts_dir):
    """注释里的 `metric('x')` 不是引用，加载不应误拒。"""
    _write(contracts_dir, "a.yml",
           formula_sql="-- 曾用过 metric('subject.old_metric')\no.sleep_hours",
           lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []})
    contracts = load_contracts(contracts_dir)
    assert "subject.a" in contracts


@pytest.mark.parametrize(
    "bad_id",
    [
        "Subject.x",      # 大写
        "subject.X",      # 段内大写
        "subject.x-y",    # 连字符
        "subject. x",     # 空格
        "subject..x",     # 空段
        ".subject",       # 前导点
        "subject.",       # 尾点
        "1subject",       # 前导数字
        "subject.1x",     # 段内数字开头
        "_subject",       # 段首下划线
    ],
)
def test_load_contracts_rejects_invalid_metric_id_format(contracts_dir, bad_id):
    """metric_id 必须是点分小写，任何偏离在加载期拒绝（fail-closed）。"""
    _write(contracts_dir, "a.yml", metric_id=bad_id)
    with pytest.raises(config.ContractError, match="点分小写"):
        load_contracts(contracts_dir)


def test_metric_contract_from_mapping_rejects_missing_or_non_string_metric_id():
    """缺失/空串/非字符串的 metric_id 在程序化构造路径同样拒绝。"""
    with pytest.raises(config.ContractError, match="metric_id"):
        metric_contract_from_mapping({}, source_file="<test>")
    with pytest.raises(config.ContractError, match="metric_id"):
        metric_contract_from_mapping({"metric_id": 123}, source_file="<test>")
    with pytest.raises(config.ContractError, match="点分小写"):
        metric_contract_from_mapping({"metric_id": "Subject.X"}, source_file="<test>")


def test_load_contracts_rejects_column_not_in_semantic_model(contracts_dir):
    """公式引用的列必须在语义模型里真实存在（typo 在加载期拒绝）。"""
    _write(contracts_dir, "a.yml",
           formula_sql="o.sleep_hour",  # typo：模型里只有 sleep_hours
           lineage={"sources": ["fact_observation.sleep_hour"], "upstream_metrics": []})
    with pytest.raises(config.ContractError, match="没有逻辑列"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_unknown_source_qualifier(contracts_dir):
    """公式引用的限定名必须是语义模型的表名或别名。"""
    _write(contracts_dir, "a.yml",
           formula_sql="mystery.sleep_hours",
           lineage={"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []})
    with pytest.raises(config.ContractError, match="不是任何语义模型"):
        load_contracts(contracts_dir)


def test_load_contracts_accepts_declarative_bucket(contracts_dir):
    """`bucket: {agg: sum}` + 日粒度标量公式是合法声明。"""
    _write(contracts_dir, "a.yml", grain="week",
           formula_sql="o.sleep_hours", bucket={"agg": "sum"})
    contracts = load_contracts(contracts_dir)
    assert contracts["subject.a"].bucket.agg == "sum"


def test_load_contracts_rejects_invalid_bucket_agg(contracts_dir):
    """bucket.agg 必须是枚举值。"""
    _write(contracts_dir, "a.yml", grain="week", bucket={"agg": "mode"})
    with pytest.raises(config.ContractError, match="bucket"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_bucket_on_day_grain(contracts_dir):
    """grain=day 不允许声明 bucket（桶聚合只用于非日粒度指标）。"""
    _write(contracts_dir, "a.yml", bucket={"agg": "sum"})  # grain 默认 day
    with pytest.raises(config.ContractError, match="bucket"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_bucket_with_window_formula(contracts_dir):
    """声明 bucket 后 formula_sql 不得再写窗口函数（两套机制互斥）。"""
    _write(contracts_dir, "a.yml", grain="week",
           formula_sql="sum(o.sleep_hours) OVER (PARTITION BY subject_id)",
           bucket={"agg": "sum"})
    with pytest.raises(config.ContractError, match="窗口函数"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_bad_enum(contracts_dir):
    _write(contracts_dir, "a.yml", agg="average")
    with pytest.raises(config.ContractError, match="agg"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_bad_grain_and_rls(contracts_dir):
    _write(contracts_dir, "a.yml", grain="hour")
    with pytest.raises(config.ContractError, match="grain"):
        load_contracts(contracts_dir)
    (contracts_dir / "a.yml").unlink()
    _write(contracts_dir, "b.yml", rls_policy="allow_all")
    with pytest.raises(config.ContractError, match="rls_policy"):
        load_contracts(contracts_dir)


def test_load_contracts_deprecated_requires_replaced_by(contracts_dir):
    _write(contracts_dir, "a.yml", status="deprecated")
    with pytest.raises(config.ContractError, match="replaced_by"):
        load_contracts(contracts_dir)


def test_load_contracts_replaced_by_must_exist(contracts_dir):
    _write(contracts_dir, "a.yml", metric_id="subject.old", status="deprecated",
           deprecation={"replaced_by": "subject.nowhere"})
    with pytest.raises(config.ContractError, match="不存在的指标"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_cycle(contracts_dir):
    _write(contracts_dir, "a.yml", metric_id="subject.a", formula_sql="sum(metric('subject.b'))",
           lineage={"sources": [], "upstream_metrics": ["subject.b"]})
    _write(contracts_dir, "b.yml", metric_id="subject.b", formula_sql="sum(metric('subject.a'))",
           lineage={"sources": [], "upstream_metrics": ["subject.a"]})
    with pytest.raises(config.ContractError, match="环"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_reserved_key(contracts_dir):
    _write(contracts_dir, "a.yml", contract_hash="sha256:forged")
    with pytest.raises(config.ContractError, match="保留键"):
        load_contracts(contracts_dir)


def test_load_contracts_rejects_extra_key(contracts_dir):
    _write(contracts_dir, "a.yml", notes="§2 之外的键必须显式失败")
    with pytest.raises(config.ContractError, match="字段校验失败"):
        load_contracts(contracts_dir)


def test_load_contracts_bad_directory(contracts_dir):
    with pytest.raises(config.ContractError):
        load_contracts(contracts_dir / "nope")


# ================================================================== 注入面
@pytest.mark.parametrize(
    "dimension",
    [
        "date.weekday; DROP TABLE fact_metric_value",
        "date.weekday -- comment",
        "date.weekday /* x */",
        "SELECT * FROM fact_metric_value",
        "date.weekday UNION SELECT 1",
    ],
)
def test_from_json_rejects_injection_in_string_values(dimension):
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.sleep_daily"], "dimensions": [dimension]})


def test_from_json_rejects_injection_in_filter_values():
    with pytest.raises(config.QueryError):
        QueryRequest.from_json(
            {"metrics": ["subject.sleep_daily"],
             "filters": {"date.between": ["2026-01-01", "2026-01-03'; DROP TABLE t --"]}}
        )


def test_redteam_injection_drop_table_rejected():
    """红队 4：字段值含 `; DROP TABLE` → QueryError（接口不接受任何 SQL 片段）。"""
    with pytest.raises(config.QueryError) as exc:
        QueryRequest.from_json(
            {"metrics": ["subject.sleep_daily"],
             "dimensions": ["date.weekday; DROP TABLE fact_metric_value"]}
        )
    assert "DROP" in str(exc.value) or ";" in str(exc.value)
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.sleep_daily"], "order_by": [{"field": "date.day; DROP TABLE t"}]})


def test_from_json_rejects_sql_payload_key():
    """接口不接受任何 SQL 字符串：多带一个 sql 键必须被拒。"""
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.sleep_daily"], "sql": "SELECT 1"})


def test_from_json_rejects_bad_json_and_shapes():
    with pytest.raises(config.QueryError):
        QueryRequest.from_json("{not json")
    with pytest.raises(config.QueryError):
        QueryRequest.from_json(["subject.sleep_daily"])
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": []})
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.x"], "limit": config.DEFAULT_LIMIT + 1})
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.x"], "limit": 0})
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.x"], "filters": {"date.month": {"nested": 1}}})
    with pytest.raises(config.QueryError):
        QueryRequest.from_json({"metrics": ["subject.x"], "order_by": [{"field": "date.day", "dir": "up"}]})


def test_from_json_accepts_json_string_with_defaults():
    req = QueryRequest.from_json(json.dumps({"metrics": ["subject.sleep_daily"]}))
    assert req.limit == config.DEFAULT_LIMIT
    assert req.grain is None
    assert req.dimensions == []
    assert req.filters == {}


# ================================================================== 五条校验
def test_redteam_unknown_metric_rejected(contracts):
    """红队 1：未注册指标 → unknown_metric:"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.not_registered"]}, contracts)
    assert exc.value.rule == "registered"
    assert exc.value.detail.startswith("unknown_metric:")


def test_deprecated_metric_rejected(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.deprecated_demo"]}, contracts)
    assert exc.value.rule == "registered"
    assert exc.value.detail.startswith("deprecated_metric:")
    assert "subject.sleep_daily" in exc.value.detail


def test_redteam_dimension_out_of_whitelist(contracts):
    """红队 2a：越界维度 → dimension_not_allowed:"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"], "dimensions": ["date.is_holiday"]}, contracts)
    assert exc.value.rule == "dimensions"
    assert exc.value.detail.startswith("dimension_not_allowed:")


def test_redteam_filter_out_of_whitelist(contracts):
    """红队 2b：越界过滤器 → filter_not_allowed:"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"], "filters": {"dim_context.is_travel": True}}, contracts)
    assert exc.value.rule == "dimensions"
    assert exc.value.detail.startswith("filter_not_allowed:")


def test_filter_not_allowed_prefix(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.is_travel_demo"], "filters": {"date.last_n_days": 5}}, contracts)
    assert exc.value.rule == "dimensions"
    assert exc.value.detail.startswith("filter_not_allowed:")


def test_dimension_not_materializable_fails_closed(contracts):
    """白名单里有、但编译器物化不了的维度 → fail-closed（不允许静默忽略）。"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.bad_dim"], "dimensions": ["widget.foo"]}, contracts)
    assert exc.value.detail.startswith("dimension_not_allowed:")


def test_unknown_column_fails_closed_at_compile_time(contracts):
    """列名写错（date.not_a_col）在编译期就拒绝，而不是等到执行时 binder 报错。"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.bad_column"], "dimensions": ["date.not_a_col"]}, contracts)
    assert exc.value.detail.startswith("dimension_not_allowed:")
    assert "没有列" in exc.value.detail


def test_fact_table_filter_fails_closed(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.bad_filter"], "filters": {"metric.value": 1}}, contracts)
    assert exc.value.detail.startswith("filter_not_allowed:")


def test_order_by_unknown_field_rejected(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"], "order_by": [{"field": "subject.other"}]}, contracts)
    assert exc.value.detail.startswith("dimension_not_allowed:")


def test_redteam_grain_too_fine_rejected(contracts):
    """红队 3：日指标求分钟 → grain_not_compatible:"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"], "grain": "minute"}, contracts)
    assert exc.value.rule == "grain"
    assert exc.value.detail.startswith("grain_not_compatible:")


def test_grain_finer_than_declared_rejected(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.spending_monthly"], "grain": "day"}, contracts)
    assert exc.value.rule == "grain"
    assert exc.value.detail.startswith("grain_not_compatible:")


def test_enforced_checks_records_actually_executed_checks(contracts, monkeypatch):
    """审计头的 `enforced_checks` 必须记录**真实执行过**的校验，而不是回显配置常量。

    为什么需要这条测试：只断言 `enforced_checks == config.ENFORCED_CHECKS` 是**同义反复**——
    即使 `compile_query` 把常量原样抄进去（删掉某条校验也不改这个字段），断言依然通过。

    做法：给五个校验函数装 spy，记录真实调用，再断言：
    ① 五个都真的被调用了；② 输出仍按契约的规范顺序排列。
    """
    executed: list[str] = []
    for name in config.ENFORCED_CHECKS:
        original = getattr(enforcement, f"check_{name}")

        def spy(*args, _name=name, _original=original, **kwargs):
            executed.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(enforcement, f"check_{name}", spy)

    compiled = _compile({"metrics": ["subject.sleep_daily"]}, contracts)

    # ① 五个校验都真的执行过（删掉任何一条，这里就会缺一项而变红）
    assert sorted(executed) == sorted(config.ENFORCED_CHECKS), (
        f"实际执行的校验 {sorted(executed)} != 契约要求的 {sorted(config.ENFORCED_CHECKS)}"
    )
    # ② 对外输出仍按契约的规范顺序（ast_join_path 在 rls 之前），与执行顺序无关
    assert compiled.enforced_checks == list(config.ENFORCED_CHECKS)

    # ③ 执行顺序：rlS 必须先于 ast_join_path（要先把改写计划算出来，才能校验最终 SQL）
    assert executed.index("rls") < executed.index("ast_join_path"), (
        f"rls 必须先于 ast_join_path 执行，实际顺序 {executed}"
    )
    # ④ 前三条按声明顺序先跑（registered → dimensions → grain）
    assert executed[:3] == ["registered", "dimensions", "grain"], executed


def test_compile_builds_sql_once_and_reuses_ast(contracts, monkeypatch):
    """SQL 只构造一次，且 AST 校验吃的是表达式树而不是渲染后的字符串。

    若先构造一份"未注入 RLS"的 SQL 校验后丢弃，再把字符串传给
    `check_ast_join_path`，会触发 sqlglot 二次解析。
    """
    from veriself.semantic import compiler as compiler_mod

    builds: list[bool] = []
    original_build = compiler_mod._Builder.build

    def counting_build(self):
        builds.append(self.rls is not None)
        return original_build(self)

    monkeypatch.setattr(compiler_mod._Builder, "build", counting_build)

    passed_tree: list[bool] = []
    original_check = enforcement.check_ast_join_path

    def spy_check(sql_or_tree, **kwargs):
        passed_tree.append(not isinstance(sql_or_tree, str))
        return original_check(sql_or_tree, **kwargs)

    monkeypatch.setattr(enforcement, "check_ast_join_path", spy_check)

    _compile({"metrics": ["subject.sleep_daily"]}, contracts)

    assert len(builds) == 1, f"SQL 应只构造一次，实际 {len(builds)} 次（rls 注入情况 {builds}）"
    assert builds == [True], "唯一一次构造应已注入 RLS"
    assert passed_tree == [True], "AST 校验应收到表达式树，而不是字符串（否则会二次解析）"


# ================================================================== 4. AST 校验
@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT a FROM secret_table", "未声明表"),
        ("SELECT a FROM fact_metric_value WHERE a IN (SELECT b FROM dim_date)", "子查询"),
        ("SELECT a FROM (SELECT a FROM fact_metric_value) AS x", "子查询"),
        ("SELECT a FROM fact_metric_value UNION ALL SELECT a FROM dim_date", "UNION"),
        ("WITH c AS (SELECT 1 AS a) SELECT a FROM c", "CTE"),
        ("SELECT * FROM fact_metric_value", "SELECT *"),
        ("SELECT f.* FROM fact_metric_value AS f", "SELECT *"),
        ("SELECT 1; DROP TABLE fact_metric_value", "单条 SELECT"),
        ("SELECT a FROM read_csv('/etc/passwd')", "表值函数"),
        ("SELECT a FROM 'x.csv'", "未声明表"),
        ("SELECT system('id')", "未声明的函数"),
        ("ATTACH 'evil.db' AS e", "必须是 SELECT"),
        ("SELECT a FROM fact_metric_value CROSS JOIN dim_date", "CROSS JOIN"),
        ("SELECT a FROM fact_metric_value JOIN dim_context ON TRUE", "JOIN"),
        ("SELECT a FROM fact_metric_value JOIN dim_subject ON dim_date.date_key = dim_subject.subject_sk",
         "JOIN"),
    ],
)
def test_ast_violations_detected(sql, fragment):
    with pytest.raises(config.EnforcementError) as exc:
        validate_ast(sql)
    assert exc.value.rule == "ast_join_path"
    assert exc.value.detail.startswith("ast_violation:")
    assert fragment in exc.value.detail


def test_redteam_ast_detects_escalation_sql():
    """红队 6：手工构造的越权 SQL 必须被 sqlglot AST 校验识破。"""
    with pytest.raises(config.EnforcementError) as exc:
        validate_ast("SELECT subject_id FROM fact_metric_value WHERE value > (SELECT avg(value) FROM fact_metric_value)")
    assert exc.value.rule == "ast_join_path"
    assert exc.value.detail.startswith("ast_violation:")
    assert "子查询" in exc.value.detail

    with pytest.raises(config.EnforcementError) as exc:
        validate_ast("SELECT a FROM secret_table")
    assert exc.value.detail.startswith("ast_violation:")
    assert "未声明表" in exc.value.detail

    with pytest.raises(config.EnforcementError) as exc:
        validate_ast("SELECT * FROM fact_metric_value")
    assert exc.value.detail.startswith("ast_violation:")
    assert "SELECT *" in exc.value.detail


def test_ast_allows_compiled_sql(contracts):
    compiled = _compile({"metrics": ["subject.sleep_daily"], "dimensions": ["date.month"]}, contracts)
    assert isinstance(check_ast_join_path(compiled.sql), object)


def test_star_check_allows_count_star_but_not_select_star():
    validate_ast("SELECT COUNT(*) FROM fact_metric_value")
    with pytest.raises(config.EnforcementError, match="SELECT \\*"):
        validate_ast("SELECT count(*), * FROM fact_metric_value")


def test_execute_refuses_tampered_compiled_query(conn, contracts):
    """防御性复核：手工构造的 CompiledQuery 也过不了 AST 校验。"""
    req = QueryRequest.from_json({"metrics": ["subject.sleep_daily"]})
    tampered = CompiledQuery(
        request=req,
        sql="SELECT * FROM fact_metric_value",
        params=[],
        metric_versions={"subject.sleep_daily": 1},
        contract_hashes={"subject.sleep_daily": HASH},
        rls_applied=[],
        enforced_checks=list(config.ENFORCED_CHECKS),
    )
    with pytest.raises(config.EnforcementError) as exc:
        execute_query(tampered, conn=conn, audit=False)
    assert exc.value.detail.startswith("ast_violation:")


# ================================================================== 5. RLS
def test_owner_is_subject_scoped(contracts, conn):
    compiled = _compile({"metrics": ["subject.sleep_daily"]}, contracts, role=config.Role.OWNER)
    assert '"subject_id" = ?' in compiled.sql
    assert config.SUBJECT_ID in compiled.params
    assert compiled.rls_applied == ["owner_only"]
    result = execute_query(compiled, role=config.Role.OWNER, conn=conn, audit=False)
    # S002 的 100.0 必须被行级过滤掉
    assert [row["subject.sleep_daily"] for row in result.data] == [6.0, 7.0, 8.0, 6.0, 7.0]


def test_redteam_partner_owner_only_denied(contracts):
    """红队 5：partner 查 owner_only → rule == rls"""
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"]}, contracts, role=config.Role.PARTNER)
    assert exc.value.rule == "rls"
    assert exc.value.detail.startswith("rls_denied:")
    assert "owner_only" in exc.value.detail


def test_aggregate_min5_partner_gets_having(contracts):
    compiled = _compile({"metrics": ["subject.steps_daily"]}, contracts, role=config.Role.PARTNER)
    assert f'COUNT(DISTINCT "f"."subject_id") >= {config.MIN_GROUP_SIZE}' in compiled.sql
    assert f"COUNT(*) >= {config.MIN_GROUP_SIZE}" in compiled.sql
    assert '"subject_id" = ?' not in compiled.sql
    assert compiled.rls_applied == ["aggregate_min5"]


def test_aggregate_min5_hides_small_groups(contracts, conn):
    """5 个主体可见；删到 3 个主体后必须返回空（而不是泄露个体值）。"""
    payload = {"metrics": ["subject.steps_daily"]}
    rows = _run(payload, contracts, conn, role=config.Role.PARTNER).data
    assert rows == [{"date.day": "2026-01-01", "subject.steps_daily": 30.0}]
    conn.execute("DELETE FROM fact_metric_value WHERE subject_id IN ('S002','S003')")
    assert _run(payload, contracts, conn, role=config.Role.PARTNER).data == []


def test_owner_on_aggregate_min5_still_sees_own_detail(contracts, conn):
    """owner 查 aggregate_min5 指标：不加闸门，拿到的必须**等于自己的明细值**（不是空集）。

    这是数值断言而不是"SQL 里有没有 HAVING"：若以后有人把 MIN_GROUP_SIZE 误改成对所有角色生效，
    owner 的数据会被 `HAVING COUNT(DISTINCT subject_id) >= 5` 清空，本用例立刻变红。
    """
    compiled = _compile({"metrics": ["subject.steps_daily"]}, contracts, role=config.Role.OWNER)
    assert "HAVING" not in compiled.sql
    assert "COUNT(DISTINCT" not in compiled.sql
    assert config.SUBJECT_ID in compiled.params

    data = execute_query(compiled, role=config.Role.OWNER, conn=conn, audit=False).data
    assert data, "owner 不应被 MIN_GROUP_SIZE 清空"
    # 与直接查明细表的结果独立比对
    expected = conn.execute(
        "SELECT value FROM fact_metric_value WHERE metric_id = ? AND subject_id = ? AND valid_to IS NULL",
        ["subject.steps_daily", config.SUBJECT_ID],
    ).fetchall()
    assert [row["subject.steps_daily"] for row in data] == [row[0] for row in expected] == [10.0]

    # 上卷到月：owner 拿到的仍是自己的值，不被跨主体聚合污染
    assert _run({"metrics": ["subject.steps_daily"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.steps_daily": 10.0}
    ]

    # 闸门对比：删到 3 个主体后 partner 被清空，owner 依旧看得到自己的明细
    conn.execute("DELETE FROM fact_metric_value WHERE subject_id IN ('S002','S003')")
    assert _run({"metrics": ["subject.steps_daily"]}, contracts, conn, role=config.Role.PARTNER).data == []
    assert [
        row["subject.steps_daily"]
        for row in _run({"metrics": ["subject.steps_daily"]}, contracts, conn, role=config.Role.OWNER).data
    ] == [10.0]


def test_no_pii_blocks_pii_column(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.pii_demo"], "dimensions": ["subject.timezone"]},
                 contracts, role=config.Role.RESEARCHER)
    assert exc.value.rule == "rls"
    assert exc.value.detail.startswith("rls_denied:")
    assert "timezone" in exc.value.detail


def test_researcher_cannot_read_owner_only(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"]}, contracts, role=config.Role.RESEARCHER)
    assert exc.value.rule == "rls"


def test_researcher_can_read_no_pii_metric(contracts, conn):
    compiled = _compile({"metrics": ["subject.pii_demo"]}, contracts, role=config.Role.RESEARCHER)
    assert '"subject_id" = ?' not in compiled.sql
    assert _run({"metrics": ["subject.pii_demo"]}, contracts, conn, role=config.Role.RESEARCHER).data == [
        {"date.day": "2026-01-01", "subject.pii_demo": 42.0}
    ]


def test_role_accepts_plain_string(contracts):
    compiled = compile_query(QueryRequest.from_json({"metrics": ["subject.steps_daily"]}), contracts, role="partner")
    assert compiled.rls_applied == ["aggregate_min5"]


# ================================================================== 跨粒度上卷
def test_same_grain_does_not_aggregate(contracts, conn):
    """同粒度禁止聚合：必须是 5 行原始日值，而不是 1 行均值。"""
    result = _run({"metrics": ["subject.sleep_daily"]}, contracts, conn)
    assert [row["subject.sleep_daily"] for row in result.data] == [6.0, 7.0, 8.0, 6.0, 7.0]
    assert "AVG(" not in _compile({"metrics": ["subject.sleep_daily"]}, contracts).sql


def test_sum_rollup_day_to_month(contracts, conn):
    compiled = _compile({"metrics": ["subject.spending_daily"], "grain": "month"}, contracts)
    assert "SUM(" in compiled.sql
    assert _run({"metrics": ["subject.spending_daily"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.spending_daily": 60.0}
    ]


def test_mean_rollup_day_to_month(contracts, conn):
    compiled = _compile({"metrics": ["subject.sleep_daily"], "grain": "month"}, contracts)
    assert "AVG(" in compiled.sql
    assert _run({"metrics": ["subject.sleep_daily"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.sleep_daily": 6.8}
    ]


def test_month_grain_rolls_up_to_quarter_with_agg(contracts, conn):
    """月粒度指标滚到季度：按 agg 在该值之上再聚合（Lead 强调的语义）。"""
    result = _run({"metrics": ["subject.spending_monthly"], "grain": "quarter"}, contracts, conn)
    assert result.data == [{"date.quarter": "2026-01-01", "subject.spending_monthly": 300.0}]


def test_week_grain_rolls_up_to_month_with_mean(contracts, conn):
    result = _run({"metrics": ["subject.focus_weekly"], "grain": "month"}, contracts, conn)
    assert result.data == [{"date.month": "2026-01-01", "subject.focus_weekly": 60.0}]


def test_last_is_arg_max_by_date_not_max_value(contracts, conn):
    """last = arg_max(value, date)：数据 5,1,3 时 last=3 而 max=5。"""
    assert _run({"metrics": ["subject.last_demo"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.last_demo": 3.0}
    ]
    assert _run({"metrics": ["subject.max_demo"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.max_demo": 5.0}
    ]
    assert _run({"metrics": ["subject.min_demo"], "grain": "month"}, contracts, conn).data == [
        {"date.month": "2026-01-01", "subject.min_demo": 1.0}
    ]
    assert "ARG_MAX(" in _compile({"metrics": ["subject.last_demo"], "grain": "month"}, contracts).sql


def test_same_grain_last_is_raw_value_not_arg_max(contracts, conn):
    result = _run({"metrics": ["subject.last_demo"]}, contracts, conn)
    assert [row["subject.last_demo"] for row in result.data] == [5.0, 1.0, 3.0]


def test_current_version_only(contracts, conn):
    """"当前有效"= valid_to IS NULL 且 metric_version = 契约 version。"""
    compiled = _compile({"metrics": ["subject.versioned"]}, contracts)
    assert '"metric_version" = ?' in compiled.sql
    assert 2 in compiled.params
    assert _run({"metrics": ["subject.versioned"]}, contracts, conn).data == [
        {"date.day": "2026-01-01", "subject.versioned": 5.0}
    ]


def test_dropped_old_contract_rows_are_ignored(contracts, conn):
    """contract_hash 归属旧口径的行不应被算进来（版本双保险）。"""
    conn.execute("UPDATE fact_metric_value SET metric_version = 1 WHERE metric_id = 'subject.versioned'")
    assert _run({"metrics": ["subject.versioned"]}, contracts, conn).data == []


def test_dim_date_inner_join_drops_orphan_dates(contracts, conn):
    assert _run({"metrics": ["subject.orphan"]}, contracts, conn).data == []


def test_multi_metric_query_in_one_scan(contracts, conn):
    payload = {"metrics": ["subject.spending_daily", "subject.sleep_daily"], "grain": "month"}
    compiled = _compile(payload, contracts)
    assert "UNION" not in compiled.sql
    result = execute_query(compiled, conn=conn, audit=False)
    assert result.data == [
        {"date.month": "2026-01-01", "subject.spending_daily": 60.0, "subject.sleep_daily": 6.8}
    ]


def test_default_order_is_date_ascending(contracts, conn):
    rows = _run({"metrics": ["subject.sleep_daily"]}, contracts, conn).data
    assert [row["date.day"] for row in rows] == sorted(row["date.day"] for row in rows)
    assert 'ORDER BY "date.day" ASC' in _compile({"metrics": ["subject.sleep_daily"]}, contracts).sql


def test_explicit_order_by_and_limit(contracts, conn):
    payload = {"metrics": ["subject.sleep_daily"], "order_by": [{"field": "date.day", "dir": "desc"}], "limit": 2}
    compiled = _compile(payload, contracts)
    assert 'ORDER BY "date.day" DESC' in compiled.sql
    assert compiled.params[-1] == 2
    assert [row["date.day"] for row in execute_query(compiled, conn=conn, audit=False).data] == [
        "2026-01-05", "2026-01-04"
    ]


# ================================================================== 过滤器
def test_date_between_filter(contracts, conn):
    payload = {"metrics": ["subject.sleep_daily"], "filters": {"date.between": ["2026-01-02", "2026-01-04"]}}
    rows = _run(payload, contracts, conn).data
    assert [row["date.day"] for row in rows] == ["2026-01-02", "2026-01-03", "2026-01-04"]


def test_date_between_bad_values_rejected(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        _compile({"metrics": ["subject.sleep_daily"], "filters": {"date.between": ["2026-01-02"]}}, contracts)
    assert exc.value.detail.startswith("filter_not_allowed:")
    with pytest.raises(config.EnforcementError):
        _compile({"metrics": ["subject.sleep_daily"],
                  "filters": {"date.between": ["2026-13-99", "2026-01-04"]}}, contracts)


def test_date_last_n_days_uses_injected_anchor(contracts, conn, monkeypatch):
    monkeypatch.setattr(compiler_mod, "_today", lambda: dt.date(2026, 1, 5))
    payload = {"metrics": ["subject.sleep_daily"], "filters": {"date.last_n_days": 3}}
    compiled = _compile(payload, contracts)
    assert "2026-01-03" in compiled.params and "2026-01-05" in compiled.params
    rows = execute_query(compiled, conn=conn, audit=False).data
    assert [row["date.day"] for row in rows] == ["2026-01-03", "2026-01-04", "2026-01-05"]


def test_date_month_equality_filter(contracts, conn):
    payload = {"metrics": ["subject.spending_monthly"], "filters": {"date.month": [2]}}
    assert _run(payload, contracts, conn).data == [
        {"date.month": "2026-02-01", "subject.spending_monthly": 200.0}
    ]


def test_dim_context_is_travel_filter(contracts, conn):
    payload = {"metrics": ["subject.is_travel_demo"], "filters": {"dim_context.is_travel": True}}
    compiled = _compile(payload, contracts)
    assert "INNER JOIN" in compiled.sql and "dim_context" in compiled.sql
    assert _run(payload, contracts, conn).data == [
        {"date.day": "2026-01-02", "subject.is_travel_demo": 2.0}
    ]


def test_dim_context_without_join_key_fails_closed(contracts):
    """冻结 DDL 的 dim_context 没有 date_key：必须明确拒绝，而不是给出错数。"""
    broken = _make_conn(with_context_key=False)
    try:
        compiled = _compile(
            {"metrics": ["subject.is_travel_demo"], "filters": {"dim_context.is_travel": True}}, contracts
        )
        with pytest.raises(config.EnforcementError) as exc:
            execute_query(compiled, conn=broken, audit=False)
        assert exc.value.detail.startswith("ast_violation:")
        assert "date_key" in exc.value.detail
    finally:
        broken.close()


def test_dim_subject_scd2_uses_current_row(contracts, conn):
    payload = {"metrics": ["subject.need_demo"], "dimensions": ["subject.sleep_need_h"]}
    compiled = _compile(payload, contracts)
    assert '"is_current" IS TRUE' in compiled.sql
    assert execute_query(compiled, conn=conn, audit=False).data == [
        {"date.day": "2026-01-01", "subject.sleep_need_h": 7.75, "subject.need_demo": 6.0}
    ]


# ================================================================== 参数化
def test_params_are_bound_and_ordered(contracts, conn):
    payload = {
        "metrics": ["subject.sleep_daily"],
        "filters": {"date.between": ["2026-01-01", "2026-01-03"]},
        "limit": 7,
    }
    compiled = _compile(payload, contracts)
    assert compiled.sql.count("?") == len(compiled.params)
    assert compiled.params == [
        "subject.sleep_daily",   # select 里的 CASE WHEN metric_id = ?
        "subject.sleep_daily",   # WHERE 的指标谓词
        1,                       # WHERE 的 metric_version
        config.SUBJECT_ID,       # RLS 行级过滤
        "2026-01-01",            # date.between 起点
        "2026-01-03",            # date.between 终点
        7,                       # LIMIT
    ]
    # 任何客户端取值都不出现在 SQL 文本里
    for value in ("2026-01-01", "2026-01-03", config.SUBJECT_ID):
        assert value not in compiled.sql
    rows = execute_query(compiled, conn=conn, audit=False).data
    assert [row["date.day"] for row in rows] == ["2026-01-01", "2026-01-02", "2026-01-03"]


# ================================================================== 执行/审计
def test_audit_header_structure(contracts, conn):
    result = _run({"metrics": ["subject.sleep_daily"]}, contracts, conn)
    audit = result.audit
    for key in ("metric_versions", "contract_hashes", "compiled_sql", "rls_applied",
                "enforced_checks", "as_of_definition", "queried_at"):
        assert key in audit
    assert audit["as_of_definition"] == AS_OF_DEFINITION == "2026-10-01"
    assert audit["enforced_checks"] == list(config.ENFORCED_CHECKS)
    assert audit["metric_versions"] == {"subject.sleep_daily": 1}
    assert audit["contract_hashes"] == {"subject.sleep_daily": contracts["subject.sleep_daily"].contract_hash}
    assert audit["rls_applied"] == ["owner_only"]
    assert audit["queried_at"].endswith("+08:00")
    assert audit["row_versioning"].startswith("valid_to IS NULL")
    assert audit["compiled_sql"] == _compile({"metrics": ["subject.sleep_daily"]}, contracts).sql


def test_result_rows_are_json_serializable(contracts, conn):
    result = _run({"metrics": ["subject.spending_monthly"], "grain": "quarter"}, contracts, conn)
    assert json.loads(json.dumps(result.model_dump()))["data"][0]["subject.spending_monthly"] == 300.0


def _install_fake_loader(monkeypatch, store):
    module = types.ModuleType("veriself.warehouse.loader")

    def write_audit(conn, record):
        store.append(record)
        conn.execute(
            "INSERT INTO fact_audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                len(store), record["queried_at"], record["actor_role"], record["request_json"],
                record["compiled_sql"], record["metric_versions"], record["contract_hashes"],
                record["rls_applied"], record["checks_passed"], record["outcome"],
            ],
        )

    module.write_audit = write_audit
    monkeypatch.setitem(sys.modules, "veriself.warehouse.loader", module)


def test_audit_row_written_to_fact_audit_log(contracts, conn, monkeypatch):
    store = []
    _install_fake_loader(monkeypatch, store)
    compiled = _compile({"metrics": ["subject.sleep_daily"]}, contracts)
    execute_query(compiled, role=config.Role.OWNER, conn=conn, audit=True)
    assert len(store) == 1
    row = conn.execute(
        "SELECT actor_role, outcome, metric_versions, rls_applied, checks_passed FROM fact_audit_log"
    ).fetchall()
    assert row == [("owner", "ok", '{"subject.sleep_daily": 1}', '["owner_only"]',
                    json.dumps(list(config.ENFORCED_CHECKS)))]
    assert config.SUBJECT_ID in store[0]["request_json"] or "subject.sleep_daily" in store[0]["request_json"]


def test_audit_false_does_not_write(contracts, conn, monkeypatch):
    store = []
    _install_fake_loader(monkeypatch, store)
    execute_query(_compile({"metrics": ["subject.sleep_daily"]}, contracts), conn=conn, audit=False)
    assert store == []
    assert conn.execute("SELECT count(*) FROM fact_audit_log").fetchone() == (0,)


def test_audit_degrades_when_loader_missing(contracts, conn, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "veriself.warehouse.loader", None)
    with caplog.at_level("WARNING"):
        result = execute_query(_compile({"metrics": ["subject.sleep_daily"]}, contracts), conn=conn, audit=True)
    assert result.data
    assert any("审计日志降级" in record.message for record in caplog.records)


def test_scan_limit_precheck_rejects(contracts, conn, monkeypatch):
    monkeypatch.setattr(config, "SCAN_LIMIT_HINT", 1)
    store = []
    _install_fake_loader(monkeypatch, store)
    compiled = _compile({"metrics": ["subject.sleep_daily"]}, contracts)
    with pytest.raises(config.EnforcementError) as exc:
        execute_query(compiled, conn=conn, audit=True)
    assert exc.value.rule == "scan"
    assert exc.value.detail.startswith("scan_too_large:")
    assert [record["outcome"] for record in store] == ["rejected:scan"]


def test_execute_reports_missing_warehouse(contracts, monkeypatch, contracts_dir):
    monkeypatch.setattr(config, "WAREHOUSE_PATH", contracts_dir / "nope.duckdb")
    with pytest.raises(config.QueryError, match="数仓文件不存在"):
        execute_query(_compile({"metrics": ["subject.sleep_daily"]}, contracts), audit=False)


# ================================================================== 目录 API
def test_list_metrics_summary(contracts):
    listed = list_metrics(contracts)
    assert [item["metric_id"] for item in listed] == sorted(contracts)
    assert set(listed[0]) == {"metric_id", "display_name", "unit", "direction", "grain", "status", "version"}
    assert all(item["status"] in ("draft", "active", "deprecated") for item in listed)


def test_describe_metric_detail(contracts):
    described = describe_metric(contracts, "subject.sleep_daily")
    assert described["metric_id"] == "subject.sleep_daily"
    assert described["contract_hash"] == contracts["subject.sleep_daily"].contract_hash
    assert described["lineage"]["sources"] == ["fact_observation.sleep_hours"]
    assert described["available_dimensions"] == ["date.weekday", "date.month"]
    assert described["as_of_definition"] == AS_OF_DEFINITION


def test_describe_metric_unknown(contracts):
    with pytest.raises(config.EnforcementError) as exc:
        describe_metric(contracts, "subject.nope")
    assert exc.value.rule == "registered"
    assert exc.value.detail.startswith("unknown_metric:")


def test_public_api_signatures_are_frozen():
    """契约 §7 冻结签名不得漂移（interfaces 按此调用）。"""
    import inspect

    from veriself.semantic import compile_query as cq
    from veriself.semantic import execute_query as eq
    from veriself.semantic import load_contracts as lc

    assert list(inspect.signature(lc).parameters) == ["metrics_dir"]
    compile_params = inspect.signature(cq).parameters
    assert list(compile_params) == ["req", "contracts", "role"]
    assert compile_params["role"].default is config.Role.OWNER
    execute_params = inspect.signature(eq).parameters
    assert list(execute_params) == ["compiled", "role", "conn", "audit"]
    assert execute_params["role"].default is config.Role.OWNER
    assert execute_params["conn"].default is None
    assert execute_params["audit"].default is True
    fields = set(QueryRequest.model_fields)
    assert fields == {"metrics", "dimensions", "filters", "grain", "order_by", "limit"}
    assert set(CompiledQuery.model_fields) == {
        "request", "sql", "params", "metric_versions", "contract_hashes", "rls_applied", "enforced_checks"
    }


# ------------------------------------------------------------------ 固定哈希样例用 YAML
SAMPLE_YAML = """metric_id: subject.sleep_debt_7d
version: 1
status: active
owner: jiayezi
display_name: 近7日睡眠债
synonyms: [睡眠债, sleep debt]
definition: 过去7日(含当日)每日睡眠缺口之和
unit: hour
direction: lower_better
agg: sum
grain: day
entity: subject
allowed_dimensions: [date.weekday, date.month]
allowed_filters: [date.between, date.last_n_days]
rls_policy: owner_only
formula_sql: |
  sum(metric('subject.sleep_need_deviation_daily'))
lineage:
  sources: []
  upstream_metrics: [subject.sleep_need_deviation_daily]
deprecation:
  replaced_by: null
  sunset_at: null
"""

UPSTREAM_YAML = """metric_id: subject.sleep_need_deviation_daily
version: 1
status: active
owner: jiayezi
display_name: 睡眠需求偏差
unit: hour
direction: neutral
agg: mean
grain: day
entity: subject
allowed_dimensions: [date.weekday]
allowed_filters: [date.between]
rls_policy: no_pii
formula_sql: o.sleep_hours - s.sleep_need_h
lineage:
  sources: [fact_observation.sleep_hours, dim_subject.sleep_need_h]
  upstream_metrics: []
deprecation:
  replaced_by: null
  sunset_at: null
"""


# ================================================================
# `date.day_of_week`：周内趋势必须按星期序号排序，而不是星期名的字母序
# ================================================================


def test_day_of_week_alias_resolves_to_numeric_column():
    """`date.day_of_week` 必须解析到 `dim_date.day_of_week`（1=周一…7=周日）。"""
    from veriself.semantic import catalog

    ref = catalog.resolve_column("date.day_of_week")
    assert ref.table == "dim_date"
    assert ref.column == "day_of_week"
    assert not ref.requires_join  # dim_date 由编译器无条件内连接

    # 与 `date.weekday` 是不同的列：一个是名字，一个是数字
    assert catalog.resolve_column("date.weekday").column == "weekday_name"


def test_order_by_day_of_week_uses_numeric_order(conn, contracts):
    """按 `date.day_of_week` 排序必须得到星期序（周一→周日），而不是字母序。

    这正是引入该维度的原因：`date.weekday` 是字符串（"Friday"…），
    按它排序会把 Friday 排在 Monday 前面——任何"周内趋势"图都会画错。
    本用例同时断言两个维度的排序结果**确实不同**，防止将来有人把两者合并。
    """
    metric_id = "subject.dow_probe"
    contract = _contract(
        metric_id=metric_id,
        allowed_dimensions=["date.weekday", "date.day_of_week"],
    )
    pool = {**contracts, metric_id: contract}

    # 造一整周的数据（2026-01-05 是周一）
    monday = dt.date(2026, 1, 5)
    for offset in range(7):
        _insert(conn, metric_id, "S001", monday + dt.timedelta(days=offset), float(offset + 1))

    numeric = _run(
        {
            "metrics": [metric_id],
            "dimensions": ["date.day_of_week"],
            "order_by": [{"field": "date.day_of_week", "dir": "asc"}],
        },
        pool,
        conn,
    )
    keys = [row["date.day_of_week"] for row in numeric.data]
    assert keys == sorted(keys), f"按 day_of_week 排序不是星期序: {keys}"
    assert keys == list(range(1, 8)), f"应覆盖周一..周日各一天，实际 {keys}"

    # 用字符串维度排序 → 字母序，且顺序与数字序不同（证明两个维度语义不同）
    alphabetical = _run(
        {
            "metrics": [metric_id],
            "dimensions": ["date.weekday"],
            "order_by": [{"field": "date.weekday", "dir": "asc"}],
        },
        pool,
        conn,
    )
    names = [row["date.weekday"] for row in alphabetical.data]
    assert names == sorted(names), f"按 weekday 排序应为字母序: {names}"
    assert names[0] == "Friday", f"字母序首项应为 Friday，实际 {names[0]}"

    # 数字序的第一天是 Monday，而字母序的第一天是 Friday —— 两种排序结果不同
    first_numeric_day = numeric.data[0]["date.day"]
    first_alpha_day = alphabetical.data[0]["date.day"]
    assert first_numeric_day != first_alpha_day

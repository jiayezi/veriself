"""T2 交付物回归测试：18 个指标契约 YAML + DuckDB DDL + 装载器。

覆盖点（对应 `docs/00-接口契约.md` 第 1 / 2 / 4 / 8 节与 `docs/01-指标清单.md`）：

1. `metrics/*.yml`：18 个文件、字段完整、枚举合法、`metric_id` 唯一且与文件名一致、
   `formula_sql` 里的每个 `表.列` 都在 `lineage.sources` 中、每个 `metric('x')` 都在
   `lineage.upstream_metrics` 中、白名单取值合法、`rls_policy` 分布与清单一致、无环。
2. `metrics/history/*.yml`：2 个历史版本（`version=1`），与当前版本口径真实不同，
   不被 `load_contracts()` 扫进查询目录，但会和当前版一起写入 `dim_metric`。
3. `schema.sql` 是唯一 DDL 来源，9 张表的列名/类型/可空性/主键与契约逐字一致，
   `ensure_schema` 幂等。
4. `loader`：`upsert_dim_metric` 幂等且哈希来自 `veriself.contract_hash`；
   `materialize_metric` 双时间轴正确（先关旧行再插新行）；`write_audit` 自增写审计。

严格校验（Pydantic 模型、五条强制校验）由 `semantic` 模块的测试负责，本文件不重复实现。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
import yaml


def _naive(*parts: int) -> dt.datetime:
    """构造 naïve 墙钟时间。DuckDB TIMESTAMP 不带时区，读回来比较时必须仍是 naïve。"""
    return dt.datetime(*parts, tzinfo=dt.UTC).replace(tzinfo=None)

from veriself import config
from veriself.contract_hash import (
    GOLDEN_CONTRACT,
    GOLDEN_CONTRACT_HASH,
    contract_hash,
)
from veriself.warehouse import (
    ensure_schema,
    materialize_metric,
    read_schema_sql,
    upsert_dim_metric,
    write_audit,
)

METRICS_DIR = config.METRICS_DIR
HISTORY_DIR = METRICS_DIR / "history"
WAREHOUSE_DIR = config.SCHEMA_SQL_PATH.parent

METRIC_FILES = sorted(METRICS_DIR.glob("*.yml"))
HISTORY_FILES = sorted(HISTORY_DIR.glob("*.yml"))
ALL_FILES = METRIC_FILES + HISTORY_FILES

# ---------------------------------------------------------------- 契约常量
REQUIRED_FIELDS = (
    "metric_id",
    "version",
    "status",
    "owner",
    "display_name",
    "synonyms",
    "definition",
    "unit",
    "direction",
    "agg",
    "grain",
    "entity",
    "allowed_dimensions",
    "allowed_filters",
    "rls_policy",
    "formula_sql",
    "lineage",
)

# docs/01-指标清单.md 的冻结取值：metric_id -> (grain, agg, unit, direction)
MANIFEST: dict[str, tuple[str, str, str, str]] = {
    "subject.sleep_duration_daily": ("day", "mean", "hour", "higher_better"),
    "subject.sleep_need_deviation_daily": ("day", "mean", "hour", "neutral"),
    "subject.sleep_debt_7d": ("day", "mean", "hour", "lower_better"),
    "subject.sleep_regularity_7d": ("day", "mean", "score", "higher_better"),
    "subject.recovery_score_daily": ("day", "mean", "score", "higher_better"),
    "subject.deep_sleep_ratio_daily": ("day", "mean", "ratio", "higher_better"),
    "subject.avg_resting_hr_7d": ("day", "mean", "count", "lower_better"),
    "subject.daily_steps": ("day", "mean", "count", "higher_better"),
    "subject.exercise_minutes_daily": ("day", "mean", "minute", "higher_better"),
    "subject.focus_score_daily": ("day", "mean", "score", "higher_better"),
    "subject.focus_score_weekly": ("week", "mean", "score", "higher_better"),
    "subject.screen_minutes_daily": ("day", "mean", "minute", "lower_better"),
    "subject.spending_daily": ("day", "sum", "currency", "neutral"),
    "subject.spending_monthly": ("month", "sum", "currency", "neutral"),
    "subject.discretionary_spending_ratio": ("month", "mean", "ratio", "lower_better"),
    "subject.mood_score_daily": ("day", "mean", "score", "higher_better"),
    "subject.mood_volatility_7d": ("day", "mean", "score", "lower_better"),
    "subject.note_count_weekly": ("week", "sum", "count", "neutral"),
}

# docs/01-指标清单.md 末尾的 RLS 分布（红队测试依赖它）
RLS_EXPECTED: dict[str, set[str]] = {
    "owner_only": {
        "subject.sleep_duration_daily",
        "subject.sleep_debt_7d",
        "subject.recovery_score_daily",
        "subject.focus_score_daily",
        "subject.mood_score_daily",
        "subject.spending_daily",
        "subject.spending_monthly",
        "subject.note_count_weekly",
    },
    "aggregate_min5": {
        "subject.avg_resting_hr_7d",
        "subject.daily_steps",
        "subject.exercise_minutes_daily",
        "subject.screen_minutes_daily",
        "subject.focus_score_weekly",
        "subject.sleep_regularity_7d",
    },
    "no_pii": {
        "subject.sleep_need_deviation_daily",
        "subject.deep_sleep_ratio_daily",
        "subject.discretionary_spending_ratio",
        "subject.mood_volatility_7d",
    },
}

DIMENSION_VOCABULARY = {
    "date.weekday",       # 星期名（"Monday"…）——按它排序是字母序
    "date.day_of_week",   # 星期序号（1=周一…7=周日）——周内趋势必须用这个
    "date.month",
    "date.quarter",
    "context.is_travel",
    "context.is_illness",
    "context.location_type",
}
FILTER_VOCABULARY = {
    "date.between",
    "date.last_n_days",
    "date.month",
    "context.is_travel",
    "context.is_illness",
    "context.location_type",
}

# 契约 §2 的必填字段全集：semantic 的 MetricContract 是 extra="forbid"，多一个键就拒载。
# 可选键：`deprecation`（空块可省略，缺省等价 null）与 `bucket`。
CONTRACT_FIELDS = frozenset(REQUIRED_FIELDS)
OPTIONAL_FIELDS = frozenset({"deprecation", "bucket"})
LINEAGE_FIELDS = frozenset({"sources", "upstream_metrics"})
DEPRECATION_FIELDS = frozenset({"replaced_by", "sunset_at"})
NAMESPACED = re.compile(r"^[a-z_]+\.[a-z_]+$")

# formula_sql 允许引用的物理表（别名 o./e./s. 由 materializer 负责改写与去前缀）
KNOWN_SOURCE_TABLES = {
    "fact_observation",
    "fact_event",
    "dim_subject",
    "dim_date",
    "dim_metric",
    "fact_metric_value",
}

# ---------------------------------------------------------------- DDL 期望（契约第 1 节，逐字）
EXPECTED_SCHEMA: dict[str, tuple[tuple[str, str, str], ...]] = {
    "dim_date": (
        ("date_key", "INTEGER", "NO"),
        ("date", "DATE", "NO"),
        ("year", "INTEGER", "YES"),
        ("quarter", "INTEGER", "YES"),
        ("month", "INTEGER", "YES"),
        ("week", "INTEGER", "YES"),
        ("day_of_week", "INTEGER", "YES"),
        ("weekday_name", "VARCHAR", "YES"),
        ("is_weekend", "BOOLEAN", "YES"),
        ("is_holiday", "BOOLEAN", "YES"),
    ),
    "dim_subject": (
        ("subject_sk", "BIGINT", "NO"),
        ("subject_id", "VARCHAR", "NO"),
        ("name", "VARCHAR", "YES"),
        ("birth_date", "DATE", "YES"),
        ("sleep_need_h", "DOUBLE", "YES"),
        ("base_weight_kg", "DOUBLE", "YES"),
        ("timezone", "VARCHAR", "YES"),
        ("valid_from", "TIMESTAMP", "NO"),
        ("valid_to", "TIMESTAMP", "YES"),
        ("is_current", "BOOLEAN", "NO"),
        ("version", "INTEGER", "NO"),
        ("recorded_at", "TIMESTAMP", "NO"),
    ),
    "dim_source": (
        ("source_id", "VARCHAR", "NO"),
        ("display_name", "VARCHAR", "YES"),
        ("reliability_tier", "VARCHAR", "YES"),
    ),
    "fact_subject_day": (
        ("subject_id", "VARCHAR", "NO"),
        ("date_key", "INTEGER", "NO"),
        ("is_travel", "BOOLEAN", "NO"),
        ("is_illness", "BOOLEAN", "NO"),
        ("location_type", "VARCHAR", "NO"),
    ),
    "dim_metric": (
        ("metric_id", "VARCHAR", "NO"),
        ("display_name", "VARCHAR", "YES"),
        ("unit", "VARCHAR", "YES"),
        ("direction", "VARCHAR", "YES"),
        ("grain", "VARCHAR", "YES"),
        ("version", "INTEGER", "NO"),
        ("contract_hash", "VARCHAR", "YES"),
        ("status", "VARCHAR", "YES"),
    ),
    "fact_observation": (
        ("observation_id", "BIGINT", "NO"),
        ("subject_id", "VARCHAR", "NO"),
        ("observed_at", "TIMESTAMP", "NO"),
        ("date_key", "INTEGER", "NO"),
        ("channel", "VARCHAR", "NO"),
        ("value", "DOUBLE", "NO"),
        ("source_id", "VARCHAR", "NO"),
        ("recorded_at", "TIMESTAMP", "NO"),
    ),
    "fact_event": (
        ("event_id", "BIGINT", "NO"),
        ("subject_id", "VARCHAR", "NO"),
        ("occurred_at", "TIMESTAMP", "NO"),
        ("date_key", "INTEGER", "NO"),
        ("event_type", "VARCHAR", "NO"),
        ("amount", "DOUBLE", "YES"),
        ("category", "VARCHAR", "YES"),
        ("text", "VARCHAR", "YES"),
        ("source_id", "VARCHAR", "NO"),
        ("recorded_at", "TIMESTAMP", "NO"),
    ),
    "fact_metric_value": (
        ("metric_id", "VARCHAR", "NO"),
        ("subject_id", "VARCHAR", "NO"),
        ("date_key", "INTEGER", "NO"),
        ("value", "DOUBLE", "NO"),
        ("metric_version", "INTEGER", "NO"),
        ("contract_hash", "VARCHAR", "NO"),
        ("computed_at", "TIMESTAMP", "NO"),
        ("valid_from", "TIMESTAMP", "NO"),
        ("valid_to", "TIMESTAMP", "YES"),
    ),
    "fact_audit_log": (
        ("audit_id", "BIGINT", "NO"),
        ("queried_at", "TIMESTAMP", "NO"),
        ("actor_role", "VARCHAR", "NO"),
        ("request_json", "VARCHAR", "NO"),
        ("compiled_sql", "VARCHAR", "YES"),
        ("metric_versions", "VARCHAR", "YES"),
        ("contract_hashes", "VARCHAR", "YES"),
        ("rls_applied", "VARCHAR", "YES"),
        ("checks_passed", "VARCHAR", "YES"),
        ("outcome", "VARCHAR", "NO"),
    ),
}

EXPECTED_PRIMARY_KEYS: dict[str, set[str]] = {
    "dim_date": {"date_key"},
    "dim_subject": {"subject_sk"},
    "dim_source": {"source_id"},
    "fact_subject_day": {"subject_id", "date_key"},
    "dim_metric": {"metric_id", "version"},
    # 观测用**业务自然键**：同一主体同一通道同一时刻只应有一条读数
    "fact_observation": {"subject_id", "observed_at", "channel"},
    # 事件刻意不加自然键：同一分钟的多笔消费是合法数据，事件真身份须由上游提供
    "fact_event": {"event_id"},
    "fact_metric_value": {"metric_id", "subject_id", "date_key", "valid_from"},
    "fact_audit_log": {"audit_id"},
}


# ---------------------------------------------------------------- 工具
def load_contract(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_contracts(paths) -> dict[str, dict]:
    return {data["metric_id"]: data for data in (load_contract(p) for p in paths)}


def fresh_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    return conn


def describe(conn, table: str) -> list[tuple[str, str, str, str]]:
    """返回 [(列名, 类型, 是否可空, key)]，顺序即建表顺序。"""
    return [(row[0], row[1], row[2], row[3]) for row in conn.execute(f"DESCRIBE {table}").fetchall()]


def is_acyclic(graph: dict[str, set[str]]) -> bool:
    remaining = {node: set(deps) for node, deps in graph.items()}
    resolved: set[str] = set()
    while remaining:
        ready = [node for node, deps in remaining.items() if deps <= resolved]
        if not ready:
            return False
        resolved.update(ready)
        for node in ready:
            remaining.pop(node)
    return True


def split_formula_references(formula: str) -> tuple[set[str], set[str]]:
    """返回 (形如 `表.列` 的引用集合, `metric('x')` 引用的 metric_id 集合)。"""
    metric_refs = set(re.findall(r"metric\(\s*'([^']+)'\s*\)", formula))
    without_metrics = re.sub(r"metric\(\s*'[^']+'\s*\)", " ", formula)
    dotted = {
        f"{table}.{column}"
        for table, column in re.findall(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", without_metrics)
    }
    return dotted, metric_refs


@pytest.fixture(scope="module")
def contracts() -> dict[str, dict]:
    return load_contracts(METRIC_FILES)


@pytest.fixture(scope="module")
def history() -> dict[str, dict]:
    return load_contracts(HISTORY_FILES)


@pytest.fixture()
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = fresh_conn()
    yield connection
    connection.close()


# ================================================================ 1. 契约 YAML
def test_metrics_dir_has_exactly_18_current_contracts(contracts: dict) -> None:
    assert len(METRIC_FILES) == 18, [p.name for p in METRIC_FILES]
    assert len(contracts) == 18
    assert set(contracts) == set(MANIFEST)


def test_history_dir_holds_two_older_versions_and_is_not_scanned(
    contracts: dict, history: dict
) -> None:
    assert len(HISTORY_FILES) == 2, [p.name for p in HISTORY_FILES]
    assert set(history) == {"subject.sleep_debt_7d", "subject.focus_score_daily"}
    # 非递归 glob（semantic 的 load_contracts 就是按 metrics/*.yml 扫的）不会带出 history
    assert set(HISTORY_FILES).isdisjoint(set(METRIC_FILES))
    assert len(METRIC_FILES) == 18
    for metric_id, current in contracts.items():
        assert current["version"] >= 1
    for metric_id, old in history.items():
        assert old["version"] == 1
        assert contracts[metric_id]["version"] == 2
        assert old["formula_sql"] != contracts[metric_id]["formula_sql"]
        assert old["definition"] != contracts[metric_id]["definition"]


def test_semantic_models_yaml_matches_frozen_column_lists():
    """semantic_models/*.yml 的列清单必须与契约 §2 冻结的可用列一致。"""
    from veriself.semantic_model import load_semantic_models

    models = load_semantic_models()
    assert set(models) == {"fact_observation", "fact_event", "dim_subject"}
    assert models["fact_observation"].column_names == frozenset({
        "sleep_hours", "sleep_start_hour", "resting_hr", "hrv", "deep_sleep_hours",
        "steps", "exercise_minutes", "focus_score", "screen_minutes", "mood_score",
    })
    assert models["fact_event"].column_names == frozenset({
        "spending", "note_count", "llm_turn_count", "workout_count", "discretionary_spending",
    })
    assert models["dim_subject"].column_names == frozenset({
        "name", "birth_date", "sleep_need_h", "base_weight_kg", "timezone",
    })


def test_every_contract_source_exists_in_semantic_models(contracts: dict, history: dict):
    """每个契约 lineage.sources 的每项都真实存在于语义模型（加载期已强制，双保险）。"""
    from veriself.semantic_model import load_semantic_models, resolve_model

    models = load_semantic_models()
    for data in [*contracts.values(), *history.values()]:
        for entry in data["lineage"]["sources"]:
            qualifier, _, column = entry.partition(".")
            model = resolve_model(models, qualifier)
            assert model is not None, f"{entry}: '{qualifier}' 不是任何语义模型的表名/别名"
            assert column in model.column_names, f"{entry}: '{column}' 不在语义模型里"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_contract_file_is_complete_and_valid(path: Path) -> None:
    data = load_contract(path)
    assert isinstance(data, dict)

    missing = [field for field in REQUIRED_FIELDS if field not in data]
    assert not missing, f"{path.name} 缺字段: {missing}"

    assert data["metric_id"] == path.stem, "metric_id 必须与文件名一致"
    assert isinstance(data["version"], int) and data["version"] >= 1
    assert data["status"] in {"draft", "active", "deprecated"}
    assert data["unit"] in config.UNITS
    assert data["direction"] in config.DIRECTIONS
    assert data["agg"] in {"sum", "mean", "min", "max", "last"}
    assert data["grain"] in config.GRAINS
    assert data["entity"] == "subject"
    assert data["owner"] and data["definition"] and data["display_name"]
    assert isinstance(data["synonyms"], list) and data["synonyms"]
    assert isinstance(data["formula_sql"], str) and data["formula_sql"].strip()
    assert config.rls_visible_roles(data["rls_policy"])  # 非法 policy 会抛 ValueError

    lineage = data["lineage"]
    assert isinstance(lineage["sources"], list)
    assert isinstance(lineage["upstream_metrics"], list)

    for dimension in data["allowed_dimensions"]:
        assert dimension in DIMENSION_VOCABULARY, dimension
        assert NAMESPACED.match(dimension), dimension
    for flt in data["allowed_filters"]:
        assert flt in FILTER_VOCABULARY, flt
        assert NAMESPACED.match(flt), flt

    if data["status"] == "deprecated":
        assert data["deprecation"]["replaced_by"], "deprecated 契约必须填 replaced_by"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_contract_uses_only_frozen_schema_fields(path: Path) -> None:
    """semantic 的 MetricContract 是 extra="forbid"：多一个键就会被拒载。

    这里把字段集锁死在契约 §2（必填全集 + 可选 `deprecation` / `bucket`），
    多键/少键都会立刻失败；`lineage` / `deprecation` 的子键同样锁死。
    `bucket` 只允许出现在非日粒度契约上；空 `deprecation` 块可省略。
    """
    data = load_contract(path)
    allowed = CONTRACT_FIELDS | OPTIONAL_FIELDS
    assert set(data) <= allowed, f"{path.name} 多出 {sorted(set(data) - allowed)}"
    assert CONTRACT_FIELDS <= set(data), (
        f"{path.name} 缺少 {sorted(CONTRACT_FIELDS - set(data))}"
    )
    if "bucket" in data:
        assert data["grain"] != "day", f"{path.name}: bucket 只允许用于非日粒度指标"
        assert set(data["bucket"]) == {"agg"}, f"{path.name}: bucket 子键只能有 agg"
    assert set(data["lineage"]) == LINEAGE_FIELDS
    if "deprecation" in data:
        assert set(data["deprecation"]) == DEPRECATION_FIELDS
        assert data["status"] == "deprecated", f"{path.name}: 空 deprecation 块应省略"
    elif data["status"] == "deprecated":
        raise AssertionError(f"{path.name}: deprecated 契约必须提供 deprecation 块")
    if data["status"] == "deprecated":
        assert data["deprecation"]["replaced_by"] in MANIFEST


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_formula_references_are_declared_in_lineage(path: Path) -> None:
    data = load_contract(path)
    sources = set(data["lineage"]["sources"])
    upstreams = set(data["lineage"]["upstream_metrics"])
    dotted, metric_refs = split_formula_references(data["formula_sql"])

    for reference in sorted(dotted):
        table, _column = reference.split(".", 1)
        assert table in KNOWN_SOURCE_TABLES, f"{path.name}: 未知来源表 {reference}"
        assert reference in sources, f"{path.name}: {reference} 未在 lineage.sources 声明"

    for metric_id in sorted(metric_refs):
        assert metric_id in upstreams, f"{path.name}: {metric_id} 未在 lineage.upstream_metrics 声明"
    for metric_id in sorted(upstreams):
        assert f"metric('{metric_id}')" in data["formula_sql"], (
            f"{path.name}: upstream_metrics 里的 {metric_id} 未在 formula_sql 里用 metric('...') 引用"
        )
    if upstreams:  # 派生指标必须用 metric('...') 形式，不能旁路引用上游的原始来源
        assert metric_refs == upstreams


def test_manifest_grain_agg_unit_direction_match(contracts: dict) -> None:
    for metric_id, expected in MANIFEST.items():
        data = contracts[metric_id]
        actual = (data["grain"], data["agg"], data["unit"], data["direction"])
        assert actual == expected, f"{metric_id}: {actual} != {expected}"


def test_rls_policy_distribution_matches_manifest(contracts: dict) -> None:
    actual: dict[str, set[str]] = {}
    for metric_id, data in contracts.items():
        actual.setdefault(data["rls_policy"], set()).add(metric_id)
    assert actual == RLS_EXPECTED
    assert set(actual) == {"owner_only", "aggregate_min5", "no_pii"}


def test_upstream_metrics_registered_and_acyclic(contracts: dict, history: dict) -> None:
    registered = set(contracts)
    graph: dict[str, set[str]] = {}
    for metric_id, data in {**contracts, **history}.items():
        upstreams = set(data["lineage"]["upstream_metrics"])
        assert upstreams <= registered, f"{metric_id} 引用了未注册的上游: {upstreams - registered}"
        graph[metric_id] = upstreams
    assert is_acyclic(graph), "lineage.upstream_metrics 存在环"


def test_derived_metrics_declare_upstream(contracts: dict) -> None:
    expected = {
        "subject.sleep_need_deviation_daily": ["subject.sleep_duration_daily"],
        "subject.sleep_debt_7d": ["subject.sleep_need_deviation_daily"],
        "subject.sleep_regularity_7d": ["subject.sleep_duration_daily"],
        "subject.focus_score_weekly": ["subject.focus_score_daily"],
        "subject.spending_monthly": ["subject.spending_daily"],
        "subject.mood_volatility_7d": ["subject.mood_score_daily"],
    }
    for metric_id, upstreams in expected.items():
        assert contracts[metric_id]["lineage"]["upstream_metrics"] == upstreams


def test_history_versions_have_real_dialect_difference(history: dict) -> None:
    sleep_debt = history["subject.sleep_debt_7d"]["formula_sql"]
    assert "ROWS BETWEEN 4 PRECEDING" in sleep_debt, "v1 应为 5 日窗口"
    focus = history["subject.focus_score_daily"]["formula_sql"]
    assert "focus_score - 40" in focus, "v1 应为 40-90 线性拉伸口径"


def test_context_dimensions_are_granted_on_every_metric(contracts: dict) -> None:
    """日情境已经能按 (subject_id, date_key) 连接，18 个当前指标都开放这三列。"""
    context = {"context.is_travel", "context.is_illness", "context.location_type"}
    for metric_id, data in contracts.items():
        assert context <= set(data["allowed_dimensions"]), metric_id
        assert context <= set(data["allowed_filters"]), metric_id


# ================================================================ 2. contract_hash
def test_contract_hash_golden_vector_matches_authoritative_module() -> None:
    assert GOLDEN_CONTRACT_HASH == "sha256:015c2525e5106928"
    assert contract_hash(GOLDEN_CONTRACT) == GOLDEN_CONTRACT_HASH
    reordered = dict(reversed(list(GOLDEN_CONTRACT.items())))
    assert contract_hash(reordered) == GOLDEN_CONTRACT_HASH, "键序不同不应改变哈希"
    assert contract_hash({"a": 1, "b": 2}) == contract_hash({"b": 2, "a": 1})

    contract = load_contract(METRICS_DIR / "subject.sleep_debt_7d.yml")
    fingerprint = contract_hash(contract)
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 16
    assert fingerprint == contract_hash(contract)


def test_loader_uses_shared_hash_module() -> None:
    source = (WAREHOUSE_DIR / "loader.py").read_text(encoding="utf-8")
    assert "from veriself.contract_hash import contract_hash" in source
    assert "import hashlib" not in source, "不得自行实现哈希算法"


# ================================================================ 3. schema.sql / DDL
def test_schema_sql_is_the_only_ddl_source() -> None:
    sql = read_schema_sql()
    statements = [
        line.strip() for line in sql.splitlines() if line.strip().upper().startswith("CREATE TABLE")
    ]
    assert len(statements) == 9, statements
    assert all("IF NOT EXISTS" in statement for statement in statements)
    for table in EXPECTED_SCHEMA:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    # Python 侧不得内联 DDL：用**行首语句**检测，而不是裸子串。
    # 裸子串会误伤 docstring 里对 `CREATE TABLE` 的引用（本仓库就有），
    # 且漏检 `CREATE INDEX` / `ALTER TABLE` / `DROP TABLE`。
    # 注：物化器里的 `CREATE OR REPLACE TEMP TABLE`（写临时表做合并）不匹配
    # `CREATE\s+TABLE`，属允许的运行时临时对象。
    inline_ddl = re.compile(r"^\s*(CREATE\s+TABLE|CREATE\s+INDEX|ALTER\s+TABLE|DROP\s+TABLE)\b", re.MULTILINE)
    for py in WAREHOUSE_DIR.glob("*.py"):
        match = inline_ddl.search(py.read_text(encoding="utf-8"))
        assert match is None, f"{py.name} 内联了 DDL: {match.group(0).strip() if match else ''}"


def test_ensure_schema_is_idempotent_and_matches_contract(conn) -> None:
    ensure_schema(conn)
    ensure_schema(conn)
    tables = {
        row[0] for row in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    }
    assert set(EXPECTED_SCHEMA) <= tables

    for table, expected in EXPECTED_SCHEMA.items():
        actual = [(name, dtype, nullable) for name, dtype, nullable, _key in describe(conn, table)]
        assert actual == list(expected), f"{table} 列定义与契约不一致"
        primary_key = {
            name for name, _dtype, _nullable, key in describe(conn, table) if key == "PRI"
        }
        assert primary_key == EXPECTED_PRIMARY_KEYS[table], f"{table} 主键不一致"


def test_ensure_schema_preserves_existing_rows(conn) -> None:
    conn.execute(
        "INSERT INTO dim_source VALUES ('wearable', '手环', 'high')"
    )
    ensure_schema(conn)
    assert conn.execute("SELECT count(*) FROM dim_source").fetchone()[0] == 1


def test_fact_metric_value_primary_key_is_enforced(conn) -> None:
    conn.execute(
        "INSERT INTO fact_metric_value VALUES "
        "('m', 'S001', 20260101, 1.0, 1, 'sha256:x', now(), TIMESTAMP '2026-01-01 00:00:00', NULL)"
    )
    with pytest.raises(duckdb.Error):
        conn.execute(
            "INSERT INTO fact_metric_value VALUES "
            "('m', 'S001', 20260101, 2.0, 1, 'sha256:x', now(), TIMESTAMP '2026-01-01 00:00:00', NULL)"
        )


# ---------------------------------------------------------------- §1.3 唯一约束
def _insert_observation(conn, *, obs_id: int = 1, channel: str = "sleep_hours", value: float = 7.0) -> None:
    conn.execute(
        "INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            obs_id,
            GOLDEN_SUBJECT,
            _naive(2026, 1, 1, 7, 30),
            20260101,
            channel,
            value,
            "wearable",
            _naive(2026, 1, 1, 7, 31),
        ],
    )


def test_duplicate_observation_is_rejected(conn) -> None:
    """同一 `(subject_id, observed_at, channel)` 只允许一条观测。

    为什么必须由数据库拦住：`obs_daily` 用 `sum(value) FILTER (WHERE channel = ...)`
    汇总当日观测——一条重复观测会让当日数值**翻倍且无任何报错**。
    这是"结构允许、静默出错"的典型，必须做成约束而不是靠约定。
    """
    _insert_observation(conn)
    with pytest.raises(duckdb.Error):
        # 同主体、同通道、同时间戳 → 必须被拒（即使 value 不同）
        _insert_observation(conn, obs_id=2, value=99.0)


def test_observation_distinguishes_by_channel_and_time(conn) -> None:
    """约束不能过严：换通道或换时间戳都应是不同的观测。"""
    _insert_observation(conn)
    _insert_observation(conn, obs_id=2, channel="focus_score")      # 换通道 → 允许
    conn.execute(
        "INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            3,
            GOLDEN_SUBJECT,
            _naive(2026, 1, 1, 8, 0),   # 换时间戳
            20260101,
            "sleep_hours",
            8.0,
            "wearable",
            _naive(2026, 1, 1, 8, 1),
        ],
    )
    assert conn.execute("SELECT count(*) FROM fact_observation").fetchone()[0] == 3


def test_observation_id_is_unique_but_not_the_primary_key(conn) -> None:
    """`observation_id` 是代理列（唯一索引），主键是业务自然键。"""
    _insert_observation(conn)
    # 换通道/时间戳 → 是另一条观测，但复用同一个 observation_id 必须被唯一索引拒绝
    with pytest.raises(duckdb.Error):
        _insert_observation(conn, channel="focus_score")

    keys = {
        name for name, _dtype, _nullable, key in describe(conn, "fact_observation") if key == "PRI"
    }
    assert keys == {"subject_id", "observed_at", "channel"}


def test_ensure_schema_has_no_migration_and_requires_rebuild() -> None:
    """**没有迁移机制**：老库不会被自动改造，升级方式是重建库。

    改 `schema.sql` 不会升级存量库：`CREATE TABLE IF NOT EXISTS` 会静默跳过旧表，
    因此老库的主键不会被改成自然键。

    ⚠️ 但**自然键的唯一性依然会被守住**——因为它同时用独立语句
    `CREATE UNIQUE INDEX IF NOT EXISTS` 声明，索引对任何库都会执行。
    这是"没有迁移函数"仍不丢掉核心保障的原因。
    """
    legacy = duckdb.connect(":memory:")
    try:
        # 模拟 v0.1 的老库：主键是代理键
        legacy.execute(
            "CREATE TABLE fact_observation ("
            " observation_id BIGINT PRIMARY KEY, subject_id VARCHAR NOT NULL,"
            " observed_at TIMESTAMP NOT NULL, date_key INTEGER NOT NULL,"
            " channel VARCHAR NOT NULL, value DOUBLE NOT NULL,"
            " source_id VARCHAR NOT NULL, recorded_at TIMESTAMP NOT NULL)"
        )
        _insert_observation(legacy)

        ensure_schema(legacy)

        # ① 主键**没有**被改造（无迁移）——这是有意为之，文档已写明升级需重建库
        keys = {
            name
            for name, _dtype, _nullable, key in describe(legacy, "fact_observation")
            if key == "PRI"
        }
        assert keys == {"observation_id"}, "不应存在迁移：老库主键保持原样"
        legacy_count = legacy.execute("SELECT count(*) FROM fact_observation").fetchone()
        assert legacy_count is not None
        assert legacy_count[0] == 1

        # ② 但自然键唯一索引已生效 → 重复观测仍被拒绝
        with pytest.raises(duckdb.Error):
            _insert_observation(legacy, obs_id=2, value=99.0)
    finally:
        legacy.close()


def test_ensure_schema_fails_loudly_when_legacy_data_violates_natural_key() -> None:
    """存量库若已有重复观测，`ensure_schema` 必须**报错**而不是静默通过。

    这是"删掉迁移"后仍然安全的关键：索引创建会校验现有数据，
    脏数据会让建索引失败并暴露出来，而不是被悄悄忽略。
    """
    dirty = duckdb.connect(":memory:")
    try:
        dirty.execute(
            "CREATE TABLE fact_observation ("
            " observation_id BIGINT PRIMARY KEY, subject_id VARCHAR NOT NULL,"
            " observed_at TIMESTAMP NOT NULL, date_key INTEGER NOT NULL,"
            " channel VARCHAR NOT NULL, value DOUBLE NOT NULL,"
            " source_id VARCHAR NOT NULL, recorded_at TIMESTAMP NOT NULL)"
        )
        # 构造一条重复观测（旧约束允许）
        _insert_observation(dirty)
        _insert_observation(dirty, obs_id=2, value=99.0)
        dirty_count = dirty.execute("SELECT count(*) FROM fact_observation").fetchone()
        assert dirty_count is not None
        assert dirty_count[0] == 2

        with pytest.raises(duckdb.Error):
            ensure_schema(dirty)
    finally:
        dirty.close()


# ================================================================ 4. loader
def test_upsert_dim_metric_writes_18_rows_and_is_idempotent(conn, contracts: dict) -> None:
    assert upsert_dim_metric(conn, contracts) == 18
    assert conn.execute("SELECT count(*) FROM dim_metric").fetchone()[0] == 18

    assert upsert_dim_metric(conn, contracts) == 18
    assert conn.execute("SELECT count(*) FROM dim_metric").fetchone()[0] == 18

    rows = dict(
        conn.execute("SELECT metric_id, contract_hash FROM dim_metric").fetchall()
    )
    for metric_id, contract in contracts.items():
        assert rows[metric_id] == contract_hash(contract), f"{metric_id} 的哈希与契约不一致"

    display_name, unit, direction, grain, version, status = conn.execute(
        "SELECT display_name, unit, direction, grain, version, status FROM dim_metric"
        " WHERE metric_id = 'subject.sleep_debt_7d'"
    ).fetchone()
    assert (display_name, unit, direction, grain, version, status) == (
        "近7日睡眠债",
        "hour",
        "lower_better",
        "day",
        2,
        "active",
    )

    updated = dict(contracts["subject.daily_steps"], display_name="日步数（改名）")
    upsert_dim_metric(conn, {"subject.daily_steps": updated})
    assert conn.execute("SELECT count(*) FROM dim_metric").fetchone()[0] == 18
    assert (
        conn.execute(
            "SELECT display_name FROM dim_metric WHERE metric_id = 'subject.daily_steps'"
        ).fetchone()[0]
        == "日步数（改名）"
    )


def test_history_versions_coexist_and_current_upsert_does_not_replace_them(
    conn, contracts: dict, history: dict
) -> None:
    """同一 metric_id 的历史版与当前版同时在表里；改当前版不会盖掉历史版。

    只按 metric_id 去 join 会把两个版本乘到事实行上。带上
    `dim_metric.version = fact_metric_value.metric_version` 才取到这一行的口径。
    """
    from veriself.semantic.contract import load_definition_versions

    definitions = load_definition_versions()
    assert len(definitions) == len(contracts) + len(history) == 20
    assert upsert_dim_metric(conn, definitions) == 20
    assert upsert_dim_metric(conn, definitions) == 20
    assert conn.execute("SELECT count(*) FROM dim_metric").fetchone()[0] == 20

    stored = {
        (metric_id, version): fingerprint
        for metric_id, version, fingerprint in conn.execute(
            "SELECT metric_id, version, contract_hash FROM dim_metric"
            " WHERE metric_id IN ('subject.sleep_debt_7d', 'subject.focus_score_daily')"
        ).fetchall()
    }
    assert stored[("subject.sleep_debt_7d", 1)] == contract_hash(history["subject.sleep_debt_7d"])
    assert stored[("subject.sleep_debt_7d", 2)] == contract_hash(contracts["subject.sleep_debt_7d"])
    assert stored[("subject.focus_score_daily", 1)] == contract_hash(history["subject.focus_score_daily"])
    assert stored[("subject.focus_score_daily", 1)] != stored[("subject.focus_score_daily", 2)]

    updated = dict(contracts["subject.sleep_debt_7d"], display_name="近7日睡眠债（改名）")
    upsert_dim_metric(conn, {"subject.sleep_debt_7d": updated})
    names = dict(
        conn.execute(
            "SELECT version, display_name FROM dim_metric WHERE metric_id = 'subject.sleep_debt_7d'"
        ).fetchall()
    )
    assert names[1] == history["subject.sleep_debt_7d"]["display_name"]
    assert names[2] == "近7日睡眠债（改名）"
    assert conn.execute("SELECT count(*) FROM dim_metric").fetchone()[0] == 20

    conn.execute(
        "INSERT INTO fact_metric_value VALUES "
        "('subject.sleep_debt_7d', 'S001', 20260101, 1.0, 2, 'sha256:test', "
        "TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-01', NULL)"
    )
    fanout = conn.execute(
        "SELECT count(*) FROM fact_metric_value AS f "
        "JOIN dim_metric AS d ON d.metric_id = f.metric_id "
        "WHERE f.metric_id = 'subject.sleep_debt_7d'"
    ).fetchone()[0]
    matched = conn.execute(
        "SELECT d.version, d.grain FROM fact_metric_value AS f "
        "JOIN dim_metric AS d ON d.metric_id = f.metric_id AND d.version = f.metric_version "
        "WHERE f.metric_id = 'subject.sleep_debt_7d'"
    ).fetchall()
    assert fanout == 2
    assert matched == [(2, "day")]


def _demo_contract(version: int) -> dict:
    return {"metric_id": "subject.demo", "version": version, "contract_hash": f"sha256:v{version}"}


def test_materialize_metric_keeps_double_timeline(conn) -> None:
    t1 = _naive(2026, 1, 1, 0, 0, 0)
    t2 = _naive(2026, 2, 1, 0, 0, 0)
    values = [
        {"subject_id": "S001", "date_key": 20260101, "value": 1.0, "computed_at": t1},
        {"subject_id": "S001", "date_key": 20260102, "value": 2.0, "computed_at": t1},
    ]
    assert materialize_metric(conn, "subject.demo", values, _demo_contract(1)) == 2

    # 只重算 1 月 1 日：该日旧行被关闭，1 月 2 日的行不受影响
    assert (
        materialize_metric(
            conn,
            "subject.demo",
            [{"subject_id": "S001", "date_key": 20260101, "value": 9.0, "computed_at": t2}],
            _demo_contract(2),
        )
        == 1
    )

    rows = conn.execute(
        "SELECT date_key, value, metric_version, contract_hash, valid_from, valid_to"
        " FROM fact_metric_value WHERE metric_id = 'subject.demo' ORDER BY date_key, valid_from"
    ).fetchall()
    assert rows == [
        (20260101, 1.0, 1, "sha256:v1", t1, t2),
        (20260101, 9.0, 2, "sha256:v2", t2, None),
        (20260102, 2.0, 1, "sha256:v1", t1, None),
    ]
    current = conn.execute(
        "SELECT count(*) FROM fact_metric_value WHERE metric_id = 'subject.demo' AND valid_to IS NULL"
    ).fetchone()[0]
    assert current == 2, "每个 (metric_id, subject_id, date_key) 只能有一行有效"


def test_materialize_metric_is_idempotent_for_same_computed_at(conn) -> None:
    computed_at = _naive(2026, 3, 1, 12, 0, 0)
    value = [{"subject_id": "S001", "date_key": 20260301, "value": 3.0, "computed_at": computed_at}]
    assert materialize_metric(conn, "subject.demo", value, _demo_contract(1)) == 1
    again = [{"subject_id": "S001", "date_key": 20260301, "value": 4.0, "computed_at": computed_at}]
    assert materialize_metric(conn, "subject.demo", again, _demo_contract(1)) == 1
    rows = conn.execute(
        "SELECT value, valid_to FROM fact_metric_value WHERE metric_id = 'subject.demo'"
    ).fetchall()
    assert rows == [(4.0, None)]


def test_materialize_metric_skips_null_nan_inf_and_deduplicates(conn) -> None:
    values = [
        {"subject_id": "S001", "date_key": 20260401, "value": None},
        {"subject_id": "S001", "date_key": 20260402, "value": float("nan")},
        {"subject_id": "S001", "date_key": 20260403, "value": float("inf")},
        {"subject_id": "S001", "date_key": 20260404, "value": "5.5"},
        {"subject_id": "S001", "date_key": "2026-04-05", "value": 6.5},
        {"subject_id": "S001", "date_key": 20260405, "value": 7.5},
    ]
    assert materialize_metric(conn, "subject.demo", values, _demo_contract(1)) == 2
    rows = conn.execute(
        "SELECT date_key, value FROM fact_metric_value WHERE metric_id = 'subject.demo'"
        " ORDER BY date_key"
    ).fetchall()
    assert rows == [(20260404, 5.5), (20260405, 7.5)]
    assert materialize_metric(conn, "subject.demo", [], _demo_contract(1)) == 0


def test_write_audit_appends_with_incrementing_id(conn) -> None:
    queried_at = _naive(2026, 5, 1, 8, 30, 0)
    assert (
        write_audit(
            conn,
            {
                "queried_at": queried_at,
                "actor_role": "owner",
                "request_json": {"metrics": ["subject.sleep_debt_7d"]},
                "compiled_sql": "SELECT 1",
                "metric_versions": {"subject.sleep_debt_7d": 2},
                "contract_hashes": {"subject.sleep_debt_7d": "sha256:abc"},
                "rls_applied": ["owner_only"],
                "enforced_checks": list(config.ENFORCED_CHECKS),
                "outcome": "ok",
            },
        )
        is None
    )
    write_audit(
        conn,
        {
            "queried_at": queried_at,
            "actor_role": "partner",
            "request_json": {"metrics": ["subject.daily_steps"]},
            "checks_passed": ["registered"],
            "outcome": "rejected:grain_not_compatible",
        },
    )

    rows = conn.execute(
        "SELECT audit_id, queried_at, actor_role, request_json, rls_applied, checks_passed, outcome"
        " FROM fact_audit_log ORDER BY audit_id"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == queried_at
    assert json.loads(rows[0][3]) == {"metrics": ["subject.sleep_debt_7d"]}
    assert json.loads(rows[0][4]) == ["owner_only"]
    assert json.loads(rows[0][5]) == list(config.ENFORCED_CHECKS)
    assert rows[1][6] == "rejected:grain_not_compatible"
    assert rows[1][4] == "[]"

    with pytest.raises(ValueError):
        write_audit(conn, {"actor_role": "owner"})
    with pytest.raises(ValueError):
        write_audit(conn, {"outcome": "ok"})
    assert conn.execute("SELECT count(*) FROM fact_audit_log").fetchone()[0] == 2


# ================================================================ 5. 与物化器的集成
def test_formula_sql_binds_in_materializer_scope(conn, contracts: dict, history: dict) -> None:
    """每个 formula_sql 都要能绑到 materializer 生成的求值作用域上（不会 Binder Error）。"""
    materializer = pytest.importorskip("veriself.materializer")
    build_insert_sql = getattr(materializer, "build_insert_sql", None)
    if build_insert_sql is None:  # 物化器 API 变更时跳过，避免误报
        pytest.skip("materializer.build_insert_sql 不存在")

    materializer.ensure_views(conn)
    pool = {**contracts, **history}
    for path in ALL_FILES:
        contract = load_contract(path)
        sql = build_insert_sql(contract, pool)
        conn.execute("EXPLAIN " + sql)


def test_materializer_obs_channels_are_channel_scoped() -> None:
    """obs_daily 的每个通道列必须只看自己 channel 的观测。

    通道聚合少了 `channel` 过滤时，同一天 `sleep_hours=7.0` 与 `focus_score=70.0`
    会被加成 77.0 / 平均成 38.5，9 个观测类指标全部算错。
    """
    materializer = pytest.importorskip("veriself.materializer")
    materializer.ensure_views(conn := fresh_conn())
    conn.execute(
        "INSERT INTO fact_observation VALUES "
        "(1, 'S001', TIMESTAMP '2026-01-01 08:00:00', 20260101, 'sleep_hours', 7.0, 'w',"
        " TIMESTAMP '2026-01-01 09:00:00')"
    )
    conn.execute(
        "INSERT INTO fact_observation VALUES "
        "(2, 'S001', TIMESTAMP '2026-01-01 09:00:00', 20260101, 'focus_score', 70.0, 'p',"
        " TIMESTAMP '2026-01-01 10:00:00')"
    )
    row = conn.execute("SELECT sleep_hours, focus_score FROM obs_daily").fetchone()
    conn.close()
    assert row == (7.0, 70.0), f"通道串味: {row}"


# ================================================================ 6. 数值基准（golden）
# 固定合成数据集：20 天 × 9 通道 + 每日 2 笔交易 + 1 条笔记（完全确定性，无随机、无种子依赖）。
# 用途：把 18 个指标的数值固化成基准。任何人改 materializer / 公式 / DDL 导致数值漂移，
# 这些用例都会立刻变红。基准值由 2026-10-01 的 IFACE-v1 实现产出并人工核对过量级。
GOLDEN_START = dt.date(2026, 1, 1)
GOLDEN_DAYS = 20
GOLDEN_SUBJECT = "S001"
GOLDEN_SLEEP_NEED_H = 7.75
# 通道 → (base, step, modulo)，通道值 = base + step * (i % modulo)
GOLDEN_CHANNEL_SPEC: tuple[tuple[str, float, float, int], ...] = (
    ("sleep_hours", 7.0, 0.3, 5),
    ("deep_sleep_hours", 1.4, 0.1, 3),
    ("resting_hr", 58.0, 1.0, 7),
    ("hrv", 45.0, 1.0, 9),
    ("focus_score", 70.0, 1.0, 11),
    ("steps", 8000.0, 100.0, 20),
    ("mood_score", 70.0, 1.0, 8),
    ("screen_minutes", 180.0, 10.0, 5),
    ("exercise_minutes", 30.0, 5.0, 4),
)

# metric_id -> 当前有效行数
GOLDEN_ROW_COUNTS: dict[str, int] = {
    "subject.sleep_duration_daily": 20,
    "subject.sleep_need_deviation_daily": 20,
    "subject.sleep_debt_7d": 20,
    "subject.sleep_regularity_7d": 20,
    "subject.recovery_score_daily": 20,
    "subject.deep_sleep_ratio_daily": 20,
    "subject.avg_resting_hr_7d": 20,
    "subject.daily_steps": 20,
    "subject.exercise_minutes_daily": 20,
    "subject.focus_score_daily": 20,
    "subject.focus_score_weekly": 4,
    "subject.screen_minutes_daily": 20,
    "subject.spending_daily": 20,
    "subject.spending_monthly": 1,
    "subject.discretionary_spending_ratio": 1,
    "subject.mood_score_daily": 20,
    "subject.mood_volatility_7d": 20,
    "subject.note_count_weekly": 4,
}

# metric_id -> 前 5 天（2026-01-01..01-05）的值，保留 3 位小数
GOLDEN_HEAD_VALUES: dict[str, list[float]] = {
    "subject.sleep_duration_daily": [7.0, 7.3, 7.6, 7.9, 8.2],
    "subject.sleep_need_deviation_daily": [-0.75, -0.45, -0.15, 0.15, 0.45],
    # 7 日滚动缺口和：0.75 → 1.2 → 1.35 后持平（窗口内只剩 3 个欠觉日）
    "subject.sleep_debt_7d": [0.75, 1.2, 1.35, 1.35, 1.35],
    # 7 日滚动标准差 → 0-100 映射（系数 40 按合成数据 σ 分布标定，见 YAML definition）
    "subject.sleep_regularity_7d": [100.0, 94.0, 90.2, 86.58, 83.03],
    "subject.recovery_score_daily": [40.67, 39.33, 38.0, 36.67, 35.33],
    "subject.deep_sleep_ratio_daily": [0.2, 0.206, 0.211, 0.177, 0.183],
    "subject.avg_resting_hr_7d": [58.0, 58.5, 59.0, 59.5, 60.0],
    "subject.daily_steps": [8000.0, 8100.0, 8200.0, 8300.0, 8400.0],
    "subject.exercise_minutes_daily": [30.0, 35.0, 40.0, 45.0, 30.0],
    "subject.focus_score_daily": [70.0, 71.0, 72.0, 73.0, 74.0],
    "subject.mood_score_daily": [70.0, 71.0, 72.0, 73.0, 74.0],
    "subject.mood_volatility_7d": [0.0, 0.5, 0.816, 1.118, 1.414],
    "subject.screen_minutes_daily": [180.0, 190.0, 200.0, 210.0, 220.0],
    "subject.spending_daily": [620.0, 623.0, 626.0, 629.0, 632.0],
}

# metric_id -> [(date_key, value)]，date_key 必须是桶内最后一个有数据的真实日期
GOLDEN_BUCKET_VALUES: dict[str, list[tuple[int, float]]] = {
    "subject.focus_score_weekly": [(20260104, 71.5), (20260111, 77.0), (20260118, 73.0), (20260120, 77.5)],
    "subject.note_count_weekly": [(20260104, 4.0), (20260111, 7.0), (20260118, 7.0), (20260120, 2.0)],
    "subject.spending_monthly": [(20260120, 12970.0)],
    "subject.discretionary_spending_ratio": [(20260120, 1.0)],
}


def _golden_dataset() -> tuple[list[tuple], list[tuple]]:
    observations: list[tuple] = []
    events: list[tuple] = []
    observation_id = 0
    event_id = 0
    for i in range(GOLDEN_DAYS):
        day = GOLDEN_START + dt.timedelta(days=i)
        date_key = int(day.strftime("%Y%m%d"))
        for channel, base, step, modulo in GOLDEN_CHANNEL_SPEC:
            observation_id += 1
            observations.append(
                (
                    observation_id,
                    GOLDEN_SUBJECT,
                    dt.datetime.combine(day, dt.time(8, 0)),
                    date_key,
                    channel,
                    base + step * (i % modulo),
                    "wearable" if channel in {"sleep_hours", "deep_sleep_hours", "resting_hr", "hrv"} else "phone",
                    dt.datetime.combine(day, dt.time(9, 0)),
                )
            )
        for event_type, amount, category, text in (
            ("transaction", 120.0 + 3.0 * i, "dining", None),
            ("transaction", 500.0, "shopping", None),
            ("note", None, None, f"note-{i}"),
        ):
            event_id += 1
            events.append(
                (
                    event_id,
                    GOLDEN_SUBJECT,
                    dt.datetime.combine(day, dt.time(12, 0)),
                    date_key,
                    event_type,
                    amount,
                    category,
                    text,
                    "phone",
                    dt.datetime.combine(day, dt.time(13, 0)),
                )
            )
    return observations, events


@pytest.fixture(scope="module")
def golden_warehouse(contracts: dict):
    """建库 → 灌固定数据 → 用 materializer 物化 18 个指标。"""
    from veriself import materializer  # Lead 所有；此处作为集成基准使用

    conn = fresh_conn()
    materializer.ensure_views(conn)
    conn.execute(
        "INSERT INTO dim_subject VALUES (1, ?, 'demo-subject', DATE '1990-01-01', ?, 70.0,"
        " 'Asia/Shanghai', TIMESTAMP '2024-01-01 00:00:00', NULL, TRUE, 1,"
        " TIMESTAMP '2024-01-01 00:00:00')",
        [GOLDEN_SUBJECT, GOLDEN_SLEEP_NEED_H],
    )
    observations, events = _golden_dataset()
    conn.executemany("INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)", observations)
    conn.executemany("INSERT INTO fact_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
    # 填 dim_date：真实链路里由 `synth` 生成，基准库也要有，
    # 否则"date_key 必须能与 dim_date 内连接"这条契约约束无法被验证。
    date_rows = []
    for i in range(GOLDEN_DAYS):
        day = GOLDEN_START + dt.timedelta(days=i)
        date_rows.append(
            (
                int(day.strftime("%Y%m%d")),
                day,
                day.year,
                (day.month - 1) // 3 + 1,
                day.month,
                int(day.strftime("%W")),
                day.isoweekday(),
                day.strftime("%A"),
                day.isoweekday() >= 6,
                False,
            )
        )
    conn.executemany("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", date_rows)
    # 写 dim_metric：让基准库也能按契约 §1 的方式"JOIN dim_metric 取 grain"，
    # 这样 grain 相关的断言走的是真实链路，而不是测试自带的字典。
    from veriself.warehouse.loader import upsert_dim_metric

    upsert_dim_metric(conn, contracts)
    written = materializer.materialize_all(conn, contracts)
    yield conn, written
    conn.close()


def test_golden_all_18_metrics_have_expected_current_rows(golden_warehouse) -> None:
    conn, written = golden_warehouse
    assert set(written) == set(MANIFEST)
    counts = {
        metric_id: conn.execute(
            "SELECT count(*) FROM fact_metric_value WHERE metric_id = ? AND valid_to IS NULL",
            [metric_id],
        ).fetchone()[0]
        for metric_id in MANIFEST
    }
    assert counts == GOLDEN_ROW_COUNTS
    assert sum(counts.values()) == 290
    assert all(count > 0 for count in counts.values())
    # 双时间轴：每个 (metric, subject, date_key) 至多一行有效
    duplicates = conn.execute(
        "SELECT metric_id, subject_id, date_key, count(*) c FROM fact_metric_value"
        " WHERE valid_to IS NULL GROUP BY 1, 2, 3 HAVING c > 1"
    ).fetchall()
    assert duplicates == []


@pytest.mark.parametrize("metric_id", sorted(GOLDEN_HEAD_VALUES))
def test_golden_daily_metric_head_values(golden_warehouse, metric_id: str) -> None:
    conn, _written = golden_warehouse
    actual = [
        row[0]
        for row in conn.execute(
            "SELECT round(value, 3) FROM fact_metric_value"
            " WHERE metric_id = ? AND valid_to IS NULL ORDER BY date_key LIMIT 5",
            [metric_id],
        ).fetchall()
    ]
    assert actual == pytest.approx(GOLDEN_HEAD_VALUES[metric_id], abs=1e-6), metric_id


@pytest.mark.parametrize("metric_id", sorted(GOLDEN_BUCKET_VALUES))
def test_golden_week_month_metric_values(golden_warehouse, metric_id: str) -> None:
    """周/月粒度指标：每桶一行，date_key 是桶内最后一个有数据的真实日期。"""
    conn, _written = golden_warehouse
    actual = conn.execute(
        "SELECT date_key, round(value, 3) FROM fact_metric_value"
        " WHERE metric_id = ? AND valid_to IS NULL ORDER BY date_key",
        [metric_id],
    ).fetchall()
    expected = GOLDEN_BUCKET_VALUES[metric_id]
    assert len(actual) == len(expected), metric_id
    for (date_key, value), (expected_key, expected_value) in zip(actual, expected):
        assert date_key == expected_key, metric_id
        assert value == pytest.approx(expected_value, abs=1e-6), metric_id
        day = _parse_date_key(date_key)
        assert GOLDEN_START <= day < GOLDEN_START + dt.timedelta(days=GOLDEN_DAYS)


# ================================================================
# date_key 语义（契约 §1「`date_key` 的语义（冻结）」）
# ================================================================


def _parse_date_key(date_key: int) -> dt.date:
    text = f"{date_key:08d}"
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _bucket_interval(date_key: int, grain: str) -> tuple[dt.date, dt.date]:
    """按契约 §1 从 `date_key + grain` 派生桶区间（闭区间）。"""
    day = _parse_date_key(date_key)
    if grain == "day":
        return day, day
    if grain == "week":
        return day - dt.timedelta(days=day.isoweekday() - 1), day
    if grain == "month":
        return day.replace(day=1), day
    if grain == "quarter":
        first_month = 3 * ((day.month - 1) // 3) + 1
        return day.replace(month=first_month, day=1), day
    raise AssertionError(f"未知 grain: {grain}")


def test_date_key_lies_inside_its_derived_bucket(golden_warehouse) -> None:
    """每个事实行的 `date_key` 必须落在「由 `date_key + grain` 派生」的桶区间内。

    契约 §1 的冻结约定：桶区间**不存储**，由 `date_key + dim_metric.grain` 派生；
    `date_key` 是桶内最后一个有数据的真实日期。这条测试把"派生"这一半固定下来。
    """
    conn, _written = golden_warehouse
    rows = conn.execute(
        "SELECT f.metric_id, f.date_key, d.grain"
        " FROM fact_metric_value AS f"
        " JOIN dim_metric AS d ON d.metric_id = f.metric_id AND d.version = f.metric_version"
        " WHERE f.valid_to IS NULL"
    ).fetchall()
    assert rows, "基准库应有事实行"

    grains_seen: set[str] = set()
    for metric_id, date_key, grain in rows:
        grains_seen.add(grain)
        start, end = _bucket_interval(date_key, grain)
        day = _parse_date_key(date_key)
        assert start <= day <= end, (
            f"{metric_id}: date_key={date_key}（{day}）不在派生桶 [{start}, {end}] 内"
        )
        # date_key 必须是真实存在的日期，才能与 dim_date 内连接
        assert conn.execute(
            "SELECT count(*) FROM dim_date WHERE date_key = ?", [date_key]
        ).fetchone()[0] == 1, f"{metric_id}: date_key={date_key} 不是 dim_date 中的日期"

    # 基准数据同时覆盖 day 与非日粒度，确保这条测试不是只验证了日粒度
    assert "day" in grains_seen
    assert grains_seen & {"week", "month"}, f"未覆盖非日粒度，实际 {grains_seen}"


def test_date_key_is_last_day_with_data_in_bucket(golden_warehouse) -> None:
    """`date_key` 必须是桶内**最后一个有数据的日期**（契约 §1 的否定式定义）。

    用"桶内不存在更晚的数据"来断言——这是"最后一个"的精确定义，
    且对"上游缺行"的桶同样成立（例如只有 2 个月数据的 `discretionary_spending_ratio`）。
    """
    conn, _written = golden_warehouse
    buckets = conn.execute(
        "SELECT f.metric_id, f.date_key, d.grain"
        " FROM fact_metric_value AS f"
        " JOIN dim_metric AS d ON d.metric_id = f.metric_id AND d.version = f.metric_version"
        " WHERE f.valid_to IS NULL"
    ).fetchall()

    for metric_id, date_key, grain in buckets:
        _start, end = _bucket_interval(date_key, grain)
        later = conn.execute(
            "SELECT count(*) FROM fact_observation"
            " WHERE date_key > ? AND date_key <= ?",
            [date_key, int(end.strftime("%Y%m%d"))],
        ).fetchone()[0]
        assert later == 0, (
            f"{metric_id}: 桶 [{_start}, {end}] 内 date_key={date_key} 之后仍有 {later} 条观测，"
            "说明 date_key 不是'桶内最后一个有数据的日期'"
        )

    # 数据在桶内提前截止时，date_key 应等于数据末日（而不是桶的名义末日）
    last_data = conn.execute("SELECT max(date_key) FROM fact_observation").fetchone()[0]
    for metric_id, grain in (("subject.focus_score_weekly", "week"),
                             ("subject.spending_monthly", "month")):
        row = conn.execute(
            "SELECT date_key FROM fact_metric_value WHERE metric_id = ? AND valid_to IS NULL"
            " ORDER BY date_key DESC LIMIT 1",
            [metric_id],
        ).fetchone()
        if row is None:
            continue
        start, end = _bucket_interval(row[0], grain)
        assert row[0] <= last_data, f"{metric_id}: date_key 晚于数据末日"
        if start <= _parse_date_key(last_data) <= end:
            assert row[0] == last_data, (
                f"{metric_id}: 数据末日 {last_data} 落在末桶 {start}..{end} 内，"
                f"date_key 应为 {last_data}，实际 {row[0]}"
            )

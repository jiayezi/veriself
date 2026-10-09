"""查询期领域描述符：加载校验，以及声明的列必须真的在 DDL 里。

加载器运行时不读 `schema.sql`。列存在性只在这里对账，这样领域文件多写一列会红，
而不是拖到查询执行时才变成 binder 错误。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from veriself import config
from veriself.domain import default_domain, parse_domain
from veriself.semantic import catalog, lineage

_COLUMN_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:BIGINT|INTEGER|VARCHAR|DOUBLE|BOOLEAN|DATE|TIMESTAMP)\b",
    re.IGNORECASE,
)


def _schema_columns(sql: str) -> dict[str, set[str]]:
    """从 DDL 抽出 `表 → 列名`。跳过约束行，只认带类型的列定义。"""
    tables: dict[str, set[str]] = {}
    current: str | None = None
    for raw in sql.splitlines():
        line = raw.split("--", 1)[0]
        header = re.match(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", line, re.IGNORECASE)
        if header:
            current = header.group(1)
            tables[current] = set()
            line = line.split("(", 1)[1]
        if current is None:
            continue
        matched = _COLUMN_RE.match(line)
        if matched:
            tables[current].add(matched.group(1))
        if re.search(r"\)\s*;\s*$", line):
            current = None
    return tables


def _minimal() -> dict:
    return {
        "domain_id": "person",
        "entity_key": "subject_id",
        "time_key": "date_key",
        "metric_table": "fact_metric_value",
        "metric_alias": "f",
        "time_dimension": {
            "namespace": "date",
            "table": "dim_date",
            "alias": "d",
            "join": "always",
            "keys": ["date_key"],
            "calendar_column": "date",
            "columns": [{"name": "date", "column": "date"}],
        },
        "dimensions": [
            {
                "namespace": "subject",
                "table": "dim_subject",
                "alias": "s",
                "join": "when_referenced",
                "keys": ["subject_id"],
                "predicate": "is_current",
                "columns": [{"name": "is_current", "column": "is_current"}],
            }
        ],
    }


def test_person_domain_columns_exist_in_schema() -> None:
    """领域文件点名的表和列必须能在 schema.sql 里找到。

    可失败性：YAML 里写一个 DDL 没有的列或表，本测试即红。加载器自己不读 DDL。
    """
    schema = _schema_columns(Path(config.SCHEMA_SQL_PATH).read_text(encoding="utf-8"))
    declared = default_domain().declared_columns()
    assert declared, "领域文件应声明至少一张表"
    for table, columns in declared.items():
        assert table in schema, f"领域文件引用了 DDL 里没有的表 {table}"
        missing = columns - schema[table]
        assert not missing, f"{table} 缺少领域文件声明的列 {sorted(missing)}"


def test_person_domain_preserves_query_names() -> None:
    """现有语义名、复合连接和读路径白名单不因抽文件而变。"""
    assert catalog.resolve_column("date.weekday").column == "weekday_name"
    assert catalog.resolve_column("dim_subject.sleep_need_h").column == "sleep_need_h"
    with pytest.raises(catalog.UnresolvedName):
        catalog.resolve_column("dim_date.weekday")
    assert catalog.FILTER_KINDS["date.between"] == "between"
    assert catalog.FILTER_KINDS["date.last_n_days"] == "last_n_days"
    assert set(lineage.declared_join_keys("fact_metric_value", "fact_subject_day")) == {
        "subject_id",
        "date_key",
    }
    assert lineage.declared_join_keys("fact_metric_value", "dim_metric") == ("metric_id",)
    assert "fact_observation" not in lineage.READ_PATH_TABLES
    assert lineage.declared_join_keys("fact_observation", "dim_source") == ("source_id",)


def test_domain_rejects_is_current_without_the_column() -> None:
    raw = _minimal()
    raw["dimensions"][0]["columns"] = [{"name": "name", "column": "name"}]
    with pytest.raises(config.ContractError, match="is_current"):
        parse_domain(raw, "bad.yml")


def test_domain_rejects_unknown_field() -> None:
    raw = _minimal()
    raw["sql"] = "select 1"
    with pytest.raises(config.ContractError, match="字段校验失败"):
        parse_domain(raw, "bad.yml")


def test_domain_rejects_split_join_keys() -> None:
    """同一对表的键拆成两条连接时拒绝，避免复合键被当成任选其一。"""
    raw = _minimal()
    raw["source_joins"] = [
        {"left": "fact_event", "right": "dim_source", "keys": ["source_id"]},
        {"left": "dim_source", "right": "fact_event", "keys": ["source_id"]},
    ]
    with pytest.raises(config.ContractError, match="同一对表"):
        parse_domain(raw, "bad.yml")

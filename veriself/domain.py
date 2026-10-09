"""查询期领域描述符（`domains/<id>.yml`）。

物化用的逻辑列仍在 `semantic_models/`。本模块只描述查询时谁能 JOIN 谁、
语义名对应哪一列。加载失败抛 `config.ContractError`。

谓词是枚举（`none` / `is_current`），不是一段 SQL。`is_current` 只允许出现在
已经声明了物理列 `is_current` 的表上。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from veriself import config

__all__ = [
    "DOMAIN_FILENAME",
    "Domain",
    "QueryRelation",
    "SourceJoin",
    "default_domain",
    "load_domain",
    "parse_domain",
]

#: 默认领域文件名。目录由 `config.DOMAINS_DIR` 决定，可用环境变量覆盖。
DOMAIN_FILENAME = "person.yml"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_JOIN_KINDS = ("always", "when_referenced")
_PREDICATES = ("none", "is_current")


class DomainColumn(BaseModel):
    """语义名 → 物理列。同名时 `name` 与 `column` 相同。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    column: str


class QueryRelation(BaseModel):
    """一张查询期可连接的表。"""

    model_config = ConfigDict(extra="forbid")

    namespace: str
    table: str
    alias: str
    also_namespaces: list[str] = Field(default_factory=list)
    join: str
    keys: list[str]
    predicate: str = "none"
    calendar_column: str | None = None
    columns: list[DomainColumn]


class SourceJoin(BaseModel):
    """来源表之间的等值连接。只进 JOIN 注册表，编译器不生成 JOIN。"""

    model_config = ConfigDict(extra="forbid")

    left: str
    right: str
    keys: list[str]


class Domain(BaseModel):
    """一份领域描述符。指标表名属于引擎的指标库，这里只登记它在本领域的键和别名。"""

    model_config = ConfigDict(extra="forbid")

    domain_id: str
    entity_key: str
    time_key: str
    metric_table: str
    metric_alias: str
    time_dimension: QueryRelation
    dimensions: list[QueryRelation] = Field(default_factory=list)
    source_joins: list[SourceJoin] = Field(default_factory=list)

    @property
    def query_relations(self) -> tuple[QueryRelation, ...]:
        """时间维在前，然后是按需连接的表（YAML 顺序）。"""
        return (self.time_dimension, *self.dimensions)

    def namespace_tables(self) -> dict[str, str]:
        """语义命名空间 → 物理表。不含引擎自己的 `metric` 命名空间。"""
        mapping: dict[str, str] = {}
        for relation in self.query_relations:
            mapping[relation.namespace] = relation.table
            for extra in relation.also_namespaces:
                mapping[extra] = relation.table
        return mapping

    def column_aliases(self) -> dict[str, str]:
        """`命名空间.语义名` → 物理列。

        物理表名作为额外命名空间时，只在语义名与物理列同名时登记。
        因此 `dim_subject.sleep_need_h` 能解析，`dim_date.weekday` 不能
        （物理列是 `weekday_name`，不是 `weekday`）。
        """
        aliases: dict[str, str] = {}
        for relation in self.query_relations:
            for column in relation.columns:
                aliases[f"{relation.namespace}.{column.name}"] = column.column
                if column.name == column.column:
                    for extra in relation.also_namespaces:
                        aliases[f"{extra}.{column.name}"] = column.column
        return aliases

    def table_columns(self) -> dict[str, frozenset[str]]:
        """查询期表 → 允许解析的物理列（语义列、连接键、谓词列）。"""
        tables: dict[str, set[str]] = {}
        for relation in self.query_relations:
            columns = {item.column for item in relation.columns}
            columns.update(relation.keys)
            if relation.predicate == "is_current":
                columns.add("is_current")
            tables.setdefault(relation.table, set()).update(columns)
        return {name: frozenset(columns) for name, columns in tables.items()}

    def declared_columns(self) -> dict[str, frozenset[str]]:
        """领域文件点名的全部物理列，含来源表连接键和指标表上的键。"""
        tables: dict[str, set[str]] = {
            name: set(columns) for name, columns in self.table_columns().items()
        }
        metric_cols = tables.setdefault(self.metric_table, set())
        metric_cols.add(self.entity_key)
        metric_cols.add(self.time_key)
        for relation in self.query_relations:
            metric_cols.update(relation.keys)
            tables.setdefault(relation.table, set()).update(relation.keys)
        for join in self.source_joins:
            tables.setdefault(join.left, set()).update(join.keys)
            tables.setdefault(join.right, set()).update(join.keys)
        return {name: frozenset(columns) for name, columns in tables.items()}

    def equijoin_edges(self) -> tuple[tuple[str, str, str], ...]:
        """等值连接 `(左表, 右表, 键列)`。不含引擎表 `dim_metric`。"""
        edges: list[tuple[str, str, str]] = []
        for relation in self.query_relations:
            for key in relation.keys:
                edges.append((self.metric_table, relation.table, key))
        for join in self.source_joins:
            for key in join.keys:
                edges.append((join.left, join.right, key))
        return tuple(edges)

    def read_path_tables(self) -> frozenset[str]:
        """编译产物允许出现的表。不含来源表，也不含 `dim_metric`。"""
        return frozenset(
            {self.metric_table, *(relation.table for relation in self.query_relations)}
        )

    def dimension_tables(self) -> frozenset[str]:
        """查询期可 JOIN、可出现在维度/过滤器里的表。"""
        return frozenset(relation.table for relation in self.query_relations)

    def alias_for(self, table: str) -> str:
        """物理表 → 查询别名。未知表抛 `KeyError`。"""
        if table == self.metric_table:
            return self.metric_alias
        for relation in self.query_relations:
            if relation.table == table:
                return relation.alias
        raise KeyError(table)


def _ident(value: str, label: str, source: str) -> None:
    if not _IDENT_RE.fullmatch(value):
        raise config.ContractError(f"领域 {source} 的 {label} '{value}' 不是合法 SQL 标识符")


def _validate(domain: Domain, source: str) -> None:
    """结构校验。失败一律 `ContractError`。"""
    if not _DOMAIN_ID_RE.fullmatch(domain.domain_id):
        raise config.ContractError(
            f"领域 {source} 的 domain_id '{domain.domain_id}' 必须是小写标识符"
        )
    _ident(domain.entity_key, "entity_key", source)
    _ident(domain.time_key, "time_key", source)
    _ident(domain.metric_table, "metric_table", source)
    _ident(domain.metric_alias, "metric_alias", source)

    namespaces: dict[str, str] = {}
    aliases: dict[str, str] = {domain.metric_alias: domain.metric_table}
    tables: dict[str, str] = {domain.metric_table: "metric_table"}

    if domain.time_dimension.join != "always":
        raise config.ContractError(f"领域 {source} 的时间维 join 必须是 always")
    if domain.time_key not in domain.time_dimension.keys:
        raise config.ContractError(
            f"领域 {source} 的 time_key '{domain.time_key}' 不在时间维的连接键里"
        )
    _validate_relation(domain.time_dimension, source, namespaces, aliases, tables, calendar=True)

    for relation in domain.dimensions:
        if relation.join != "when_referenced":
            raise config.ContractError(
                f"领域 {source} 的表 '{relation.table}' join 必须是 when_referenced"
            )
        _validate_relation(relation, source, namespaces, aliases, tables, calendar=False)

    seen_pairs: set[frozenset[str]] = set()
    for relation in domain.query_relations:
        pair = frozenset({domain.metric_table, relation.table})
        if pair in seen_pairs:
            raise config.ContractError(
                f"领域 {source} 的连接 {domain.metric_table} ↔ {relation.table} 重复；"
                "同一对表的键必须写在同一条连接里"
            )
        seen_pairs.add(pair)

    for join in domain.source_joins:
        _ident(join.left, "source_joins.left", source)
        _ident(join.right, "source_joins.right", source)
        if join.left == join.right:
            raise config.ContractError(f"领域 {source} 的来源连接不能是表自己：{join.left}")
        if not join.keys:
            raise config.ContractError(
                f"领域 {source} 的来源连接 {join.left} ↔ {join.right} 缺少连接键"
            )
        seen: set[str] = set()
        for key in join.keys:
            _ident(key, "source_joins.keys", source)
            if key in seen:
                raise config.ContractError(
                    f"领域 {source} 的来源连接 {join.left} ↔ {join.right} 键 '{key}' 重复"
                )
            seen.add(key)
        pair = frozenset({join.left, join.right})
        if pair in seen_pairs:
            raise config.ContractError(
                f"领域 {source} 的连接 {join.left} ↔ {join.right} 重复；"
                "同一对表的键必须写在同一条连接里"
            )
        seen_pairs.add(pair)


def _validate_relation(
    relation: QueryRelation,
    source: str,
    namespaces: dict[str, str],
    aliases: dict[str, str],
    tables: dict[str, str],
    *,
    calendar: bool,
) -> None:
    label = relation.table or relation.namespace or source
    _ident(relation.namespace, f"表 {label} 的 namespace", source)
    _ident(relation.table, f"表 {label} 的 table", source)
    _ident(relation.alias, f"表 {label} 的 alias", source)
    if relation.join not in _JOIN_KINDS:
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' join='{relation.join}'"
            f" 不在 {list(_JOIN_KINDS)} 中"
        )
    if relation.predicate not in _PREDICATES:
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' predicate='{relation.predicate}'"
            f" 不在 {list(_PREDICATES)} 中"
        )
    if not relation.keys:
        raise config.ContractError(f"领域 {source} 的表 '{relation.table}' 缺少连接键")
    if not relation.columns:
        raise config.ContractError(f"领域 {source} 的表 '{relation.table}' 的 columns 为空")

    _claim(namespaces, relation.namespace, relation.table, source, "命名空间")
    if len(relation.also_namespaces) != len(set(relation.also_namespaces)):
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' 的 also_namespaces 有重复项"
        )
    for extra in relation.also_namespaces:
        _ident(extra, f"表 {relation.table} 的 also_namespaces", source)
        if extra == relation.namespace:
            raise config.ContractError(
                f"领域 {source} 的表 '{relation.table}' 把命名空间 '{extra}' 重复登记了"
            )
        _claim(namespaces, extra, relation.table, source, "命名空间")
    if relation.alias in aliases:
        raise config.ContractError(
            f"领域 {source} 的别名 '{relation.alias}' 同时用于"
            f" {aliases[relation.alias]} 与 {relation.table}"
        )
    aliases[relation.alias] = relation.table
    if relation.table in tables:
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' 重复出现（已作为 {tables[relation.table]}）"
        )
    tables[relation.table] = relation.namespace

    seen_keys: set[str] = set()
    for key in relation.keys:
        _ident(key, f"表 {relation.table} 的连接键", source)
        if key in seen_keys:
            raise config.ContractError(f"领域 {source} 的表 '{relation.table}' 连接键 '{key}' 重复")
        seen_keys.add(key)

    physical = set()
    seen_names: set[str] = set()
    for column in relation.columns:
        _ident(column.name, f"表 {relation.table} 的语义名", source)
        _ident(column.column, f"表 {relation.table} 的物理列", source)
        if column.name in seen_names:
            raise config.ContractError(
                f"领域 {source} 的表 '{relation.table}' 语义名 '{column.name}' 重复"
            )
        seen_names.add(column.name)
        physical.add(column.column)

    if relation.predicate == "is_current" and "is_current" not in physical:
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' 使用谓词 is_current，但未声明该物理列"
        )
    if calendar:
        if not relation.calendar_column:
            raise config.ContractError(f"领域 {source} 的时间维缺少 calendar_column")
        _ident(relation.calendar_column, "calendar_column", source)
        if relation.calendar_column not in physical:
            raise config.ContractError(
                f"领域 {source} 的 calendar_column '{relation.calendar_column}'"
                " 不是时间维已声明的物理列"
            )
    elif relation.calendar_column is not None:
        raise config.ContractError(
            f"领域 {source} 的表 '{relation.table}' 不是时间维，不能声明 calendar_column"
        )


def _claim(taken: dict[str, str], name: str, owner: str, source: str, kind: str) -> None:
    previous = taken.get(name)
    if previous is not None and previous != owner:
        raise config.ContractError(
            f"领域 {source} 的{kind} '{name}' 同时指向 {previous} 与 {owner}"
        )
    taken[name] = owner


def parse_domain(raw: Mapping[str, object], source: str) -> Domain:
    """从已解析的映射构造领域。字段不合法抛 `ContractError`。"""
    try:
        domain = Domain.model_validate(dict(raw))
    except ValidationError as exc:
        raise config.ContractError(f"领域 {source} 字段校验失败：{exc}") from exc
    _validate(domain, source)
    return domain


def load_domain(path: Path | None = None) -> Domain:
    """加载一份领域文件。`path=None` 时取 `DOMAINS_DIR/person.yml`。"""
    file = Path(path) if path is not None else config.DOMAINS_DIR / DOMAIN_FILENAME
    if not file.is_file():
        raise config.ContractError(f"领域文件不存在：{file}")
    try:
        loaded = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise config.ContractError(f"领域文件 {file.name} 无法读取：{exc}") from exc
    if not isinstance(loaded, Mapping):
        raise config.ContractError(
            f"领域文件 {file.name} 顶层必须是映射，实际是 {type(loaded).__name__}"
        )
    return parse_domain(loaded, file.name)


@lru_cache(maxsize=1)
def default_domain() -> Domain:
    """默认领域（`veriself/domains/person.yml`）。catalog / lineage / compiler 走这里。"""
    return load_domain()

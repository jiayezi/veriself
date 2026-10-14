"""指标契约的 Pydantic v2 模型与加载期强校验（IFACE-v1 第 2 节）。

`load_contracts` 会强制这些规则（契约 §2）：
1. `metric_id` 唯一，且与文件名主干一致；
2. 同目录同名 `.sql` 是一条 SELECT，输出 `subject_id` / `date_key` / `value`；
   只读来源视图或 `fact_metric_value`，且 `depends_on` 与 SQL 里的上游一致；
3. `status=deprecated` 时 `deprecation.replaced_by` 必填（且必须指向已存在的指标）；
4. `grain` / `direction` / `unit` / `agg` / `status` / `rls_policy` 必须 ∈ 各自枚举；
5. `depends_on` 引用的指标必须存在，且禁止环；读到的 `metric_version` 必须是该上游的当前版本。

任何失败一律抛 `config.ContractError`（启动即失败，不允许半可用状态）。

`contract_hash` **不在这里实现**：唯一权威实现在 `veriself/contract_hash.py`。
哈希输入是原始 YAML dict 加上 `sql` 键（SQL 文件原文）。改 SQL 即改口径哈希。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from veriself import config, semantic_model
from veriself.contract_hash import contract_hash
from veriself.semantic import lineage as lineage_mod
from veriself.semantic.metric_sql import check_metric_sql

__all__ = [
    "AGG_VALUES",
    "AS_OF_DEFINITION",
    "RLS_POLICIES",
    "STATUS_VALUES",
    "Deprecation",
    "MetricContract",
    "load_contracts",
    "load_definition_versions",
    "metric_contract_from_mapping",
]

_log = logging.getLogger(__name__)

#: 契约冻结日期（IFACE-v1），审计头 `as_of_definition`。
AS_OF_DEFINITION = "2026-10-01"

#: 跨时间粒度上卷方式（契约 §2）。只用于查询期上卷，不参与物化。
AGG_VALUES: tuple[str, ...] = ("sum", "mean", "min", "max", "last")
STATUS_VALUES: tuple[str, ...] = ("draft", "active", "deprecated")
RLS_POLICIES: tuple[str, ...] = ("owner_only", "aggregate_min5", "no_pii")

#: YAML 里不允许出现的保留键。`sql` / `formula_sql` 由同名 `.sql` 文件承载，
#: 写进 YAML 会让口径有两份来源。
_RESERVED_KEYS = ("contract_hash", "source_file", "sql", "formula_sql")

#: metric_id 必须是"点分小写"（契约 §2）。它会被字符串插值进物化 SQL / 查询 SQL，
#: 因此格式约束既是注入面的第一道闸门，也是"`metric('...')` 引用可被可靠解析"的前提。
#: 注意：段内**允许下划线**（现有契约大量使用，如 `sleep_need_deviation_daily`），
#: 但段首必须是**小写字母**、段内只允许小写字母/数字/下划线、用点分隔。
_METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")


class Deprecation(BaseModel):
    """废弃信息（契约 §2 可选块）。"""

    model_config = ConfigDict(extra="forbid")

    replaced_by: str | None = None
    sunset_at: str | None = None


class MetricContract(BaseModel):
    """指标契约（字段与契约 §2 的 YAML schema 一一对应）。

    计算本身在同名 `.sql` 里。`contract_hash` / `source_file` / `sql` 是加载器附加的，
    哈希输入是 YAML 字段加上 `sql` 原文，不包含 `contract_hash` 与 `source_file`。
    """

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    version: int = Field(ge=1)
    status: str = "active"
    owner: str
    display_name: str = ""
    synonyms: list[str] = Field(default_factory=list)
    definition: str = ""
    unit: str
    direction: str
    agg: str
    grain: str
    entity: str = "subject"
    allowed_dimensions: list[str] = Field(default_factory=list)
    allowed_filters: list[str] = Field(default_factory=list)
    rls_policy: str
    depends_on: list[str] = Field(default_factory=list)
    deprecation: Deprecation | None = None

    # 加载器附加（contract_hash / source_file 不参与哈希；sql 参与）
    contract_hash: str = ""
    source_file: str = ""

    #: 哈希输入：YAML 字段 + `sql` 原文
    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)
    _sql: str = PrivateAttr(default="")
    #: SQL 里读到的 (上游 metric_id, metric_version)，加载期抽出，不改写 SQL
    _metric_reads: tuple[tuple[str, int], ...] = PrivateAttr(default_factory=tuple)

    @property
    def raw(self) -> dict[str, Any]:
        """哈希输入的只读副本（YAML 字段加上 `sql`）。"""
        return dict(self._raw)

    @property
    def sql(self) -> str:
        """指标 SQL 原文。"""
        return self._sql

    @property
    def metric_reads(self) -> tuple[tuple[str, int], ...]:
        """SQL 中每个 `fact_metric_value` 读取的 `(metric_id, metric_version)`。"""
        return tuple(self._metric_reads)

    def allow_dimension(self, name: str) -> bool:
        """该指标是否把 `name` 列入维度白名单。"""
        return name in self.allowed_dimensions

    def allow_filter(self, name: str) -> bool:
        """该指标是否把 `name` 列入过滤器白名单。"""
        return name in self.allowed_filters


# ---------------------------------------------------------------- 加载
def _read_yaml(path: Path) -> dict[str, Any]:
    """读取单个 YAML 文件，失败抛 `ContractError`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise config.ContractError(f"契约文件 {path.name} 无法读取：{exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise config.ContractError(f"契约文件 {path.name} YAML 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise config.ContractError(f"契约文件 {path.name} 顶层必须是映射（dict），实际是 {type(raw).__name__}")
    for reserved in _RESERVED_KEYS:
        if reserved in raw:
            raise config.ContractError(
                f"契约文件 {path.name} 不允许出现保留键 '{reserved}'（由加载器计算，防止伪造）"
            )
    return raw


def _check_metric_id(payload: Mapping[str, Any], source_file: str) -> None:
    """契约 §2：`metric_id` 必须是"点分小写"形式，否则拒绝。

    它会被字符串插值进物化 SQL / 查询 SQL，格式非法会导致解析歧义或注入面扩大；
    这是除注入面检查外的第一道格式闸门。缺失/空串/非字符串也在此拒绝（fail-closed）。
    """
    metric_id = payload.get("metric_id")
    if not isinstance(metric_id, str) or not metric_id:
        raise config.ContractError(f"契约 {source_file} 的 metric_id 缺失或为空：{metric_id!r}")
    if not _METRIC_ID_RE.fullmatch(metric_id):
        raise config.ContractError(
            f"契约 {source_file} 的 metric_id '{metric_id}' 不是 '点分小写' 形式"
            "（段首小写字母，段内只允许小写字母/数字/下划线，用点分隔，如 subject.sleep_debt_7d）"
        )


def _check_enums(raw: Mapping[str, Any], name: str) -> None:
    """枚举检查（契约 §2 规则 4）。"""
    checks = (
        ("grain", config.GRAINS),
        ("direction", config.DIRECTIONS),
        ("unit", config.UNITS),
        ("agg", AGG_VALUES),
        ("status", STATUS_VALUES),
    )
    for key, allowed in checks:
        if key not in raw:
            continue
        value = raw[key]
        if value not in allowed:
            raise config.ContractError(
                f"契约 {name} 的 {key}='{value}' 不在枚举 {list(allowed)} 中"
            )
    policy = raw.get("rls_policy")
    if policy is not None:
        try:
            config.rls_visible_roles(str(policy))
        except ValueError as exc:
            raise config.ContractError(f"契约 {name} 的 rls_policy 非法：{exc}") from exc


def _check_upstream_versions(contract: MetricContract, catalog: Mapping[str, MetricContract]) -> None:
    """SQL 里写下的 `metric_version` 必须等于该上游契约的当前版本。"""
    for upstream_id, version in contract.metric_reads:
        upstream = catalog.get(upstream_id)
        if upstream is None:
            continue
        if upstream.version != version:
            raise config.ContractError(
                f"契约 {contract.metric_id} 读取上游 '{upstream_id}' 的 metric_version={version}，"
                f"但该指标当前版本是 {upstream.version}"
            )


def _check_attribute_names(contract: MetricContract) -> None:
    """维度/过滤器白名单的写法检查（必须是 `命名空间.属性`）。"""
    for key, names in (
        ("allowed_dimensions", contract.allowed_dimensions),
        ("allowed_filters", contract.allowed_filters),
    ):
        for name in names:
            if not lineage_mod.is_attribute_name(str(name)):
                raise config.ContractError(
                    f"契约 {contract.metric_id} 的 {key} 项 '{name}' 不是 '命名空间.属性' 形式（如 date.weekday）"
                )


def metric_contract_from_mapping(
    raw: Mapping[str, Any],
    source_file: str = "<memory>",
    *,
    sql: str | None = None,
    models: Mapping[str, Any] | None = None,
) -> MetricContract:
    """从原始 dict 构造并校验单个契约（不做跨契约检查）。

    哈希输入是 YAML 字段加上 `sql` 原文。`sql` 可以放在 `raw["sql"]`（程序化重建，
    与 `contract.raw` 往返），也可以用参数传入（从 `.sql` 文件读入）。
    YAML 文件路径上的 `sql` / `formula_sql` 键由 `_read_yaml` 拒绝，不会走到这里。
    `models` 为语义模型集；None 时自动加载 `semantic_models/` 并做列存在性校验。
    显式传空 `{}` 可跳过列存在性校验（测试构造用）。
    """
    if not isinstance(raw, Mapping):
        raise config.ContractError(f"契约 {source_file} 必须是映射，实际是 {type(raw).__name__}")
    payload = dict(raw)
    if "formula_sql" in payload:
        raise config.ContractError(
            f"契约 {source_file} 不允许出现 formula_sql（计算写在同名 .sql 文件里）"
        )
    for reserved in ("contract_hash", "source_file"):
        if reserved in payload:
            raise config.ContractError(f"契约 {source_file} 不允许出现保留键 '{reserved}'")
    embedded = payload.pop("sql", None)
    if sql is None:
        sql_text = embedded if isinstance(embedded, str) else None
    else:
        if embedded is not None and embedded != sql:
            raise config.ContractError(f"契约 {source_file} 的 sql 参数与映射中的 sql 不一致")
        sql_text = sql
    _check_metric_id(payload, source_file)
    _check_enums(payload, source_file)
    try:
        contract = MetricContract(**payload)
    except ValidationError as exc:
        raise config.ContractError(f"契约 {source_file} 字段校验失败：{exc}") from exc
    if not isinstance(sql_text, str) or not sql_text.strip():
        raise config.ContractError(f"契约 {contract.metric_id} 缺少 SQL")
    _check_attribute_names(contract)
    resolved_models = models if models is not None else semantic_model.default_models()
    reads = check_metric_sql(
        sql_text,
        metric_id=contract.metric_id,
        depends_on=list(contract.depends_on),
        models=resolved_models or None,
    )
    if contract.status == "deprecated":
        replaced_by = contract.deprecation.replaced_by if contract.deprecation else None
        if not replaced_by:
            raise config.ContractError(
                f"契约 {contract.metric_id} status=deprecated，必须提供 deprecation.replaced_by"
            )
    hash_payload = dict(payload)
    hash_payload["sql"] = sql_text
    contract.contract_hash = contract_hash(hash_payload)
    contract.source_file = source_file
    contract._raw = hash_payload
    contract._sql = sql_text
    contract._metric_reads = reads
    return contract


def load_contracts(metrics_dir: Path | None = None) -> dict[str, MetricContract]:
    """加载并校验 `metrics/*.yml`，返回 `{metric_id: MetricContract}`。

    失败（重复 metric_id / 环 / 非法枚举 / SQL 不合法 / deprecated 缺 replaced_by）抛
    `config.ContractError`。
    """
    directory = Path(metrics_dir) if metrics_dir is not None else config.METRICS_DIR
    if not directory.is_dir():
        raise config.ContractError(f"指标契约目录不存在：{directory}")
    paths = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    if not paths:
        raise config.ContractError(f"指标契约目录为空：{directory}")

    contracts: dict[str, MetricContract] = {}
    for path in paths:
        raw = _read_yaml(path)
        if "metric_id" not in raw:
            raise config.ContractError(f"契约文件 {path.name} 缺少必填字段 metric_id")
        contract = metric_contract_from_mapping(raw, source_file=path.name, sql=_read_sql(path))
        if path.stem != contract.metric_id:
            raise config.ContractError(
                f"契约文件 {path.name} 的文件名必须与 metric_id '{contract.metric_id}' 一致"
            )
        if contract.metric_id in contracts:
            other = contracts[contract.metric_id].source_file
            raise config.ContractError(
                f"metric_id 重复：'{contract.metric_id}' 同时出现在 {other} 与 {path.name}"
            )
        contracts[contract.metric_id] = contract

    _check_cross_contracts(contracts)
    _log.info("已加载 %d 个指标契约：%s", len(contracts), directory)
    return contracts


def load_definition_versions(metrics_dir: Path | None = None) -> list[MetricContract]:
    """当前口径加上 `metrics/history/`，供写入 `dim_metric`。

    查询与物化仍走 `load_contracts()`：那个结果按 `metric_id` 唯一，不含历史版本。
    这里返回列表，因为同一个 `metric_id` 会有多行。

    `history/` 里的每一份必须是当前目录中已有指标的更早版本（`version` 严格更小）。
    同一 `(metric_id, version)` 出现两次即拒绝。历史契约的 `depends_on`
    必须能在当前目录里找到，但不把历史版本自己放进查询用的契约表。
    历史 SQL 不参与物化，只进入版本登记和哈希。
    """
    directory = Path(metrics_dir) if metrics_dir is not None else config.METRICS_DIR
    current = load_contracts(directory)
    versions: list[MetricContract] = list(current.values())
    seen = {(contract.metric_id, contract.version) for contract in versions}
    history_dir = directory / "history"
    if not history_dir.is_dir():
        return versions
    paths = sorted({*history_dir.glob("*.yml"), *history_dir.glob("*.yaml")})
    for path in paths:
        raw = _read_yaml(path)
        if "metric_id" not in raw:
            raise config.ContractError(f"契约文件 history/{path.name} 缺少必填字段 metric_id")
        contract = metric_contract_from_mapping(
            raw, source_file=f"history/{path.name}", sql=_read_sql(path)
        )
        key = (contract.metric_id, contract.version)
        if key in seen:
            raise config.ContractError(
                f"口径版本重复：'{contract.metric_id}' version={contract.version} "
                f"已存在，又出现在 history/{path.name}"
            )
        current_contract = current.get(contract.metric_id)
        if current_contract is None:
            raise config.ContractError(
                f"history/{path.name} 的 metric_id '{contract.metric_id}' 不在当前指标目录中"
            )
        if contract.version >= current_contract.version:
            raise config.ContractError(
                f"history/{path.name} 的 version={contract.version} 必须小于当前版本 "
                f"{current_contract.version}"
            )
        missing = [metric_id for metric_id in contract.depends_on if metric_id not in current]
        if missing:
            raise config.ContractError(
                f"history/{path.name} 的 depends_on 引用了当前目录中不存在的指标 {missing}"
            )
        _check_upstream_versions(contract, current)
        versions.append(contract)
        seen.add(key)
    _log.info("口径版本 %d 个（含 history %d 个）：%s", len(versions), len(paths), directory)
    return versions


def _read_sql(yaml_path: Path) -> str:
    """读取与 YAML 同目录、同主干的 `.sql`。原文参与哈希，不做改写。"""
    sql_path = yaml_path.with_suffix(".sql")
    if not sql_path.is_file():
        raise config.ContractError(f"契约 {yaml_path.name} 缺少同名 SQL 文件 {sql_path.name}")
    try:
        return sql_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise config.ContractError(f"SQL 文件 {sql_path.name} 无法读取：{exc}") from exc


def _check_cross_contracts(contracts: Mapping[str, MetricContract]) -> None:
    """`depends_on` 存在且无环，SQL 中的上游版本与当前契约一致，`replaced_by` 可追溯。"""
    upstream = {metric_id: list(c.depends_on) for metric_id, c in contracts.items()}
    try:
        lineage_mod.topological_layers(upstream)
    except lineage_mod.LineageError as exc:
        raise config.ContractError(f"depends_on 校验失败：{exc}") from exc
    for contract in contracts.values():
        _check_upstream_versions(contract, contracts)
    for metric_id, contract in contracts.items():
        replaced_by = contract.deprecation.replaced_by if contract.deprecation else None
        if not replaced_by:
            continue
        if replaced_by == metric_id:
            raise config.ContractError(f"契约 {metric_id} 的 deprecation.replaced_by 指向自己")
        if replaced_by not in contracts:
            raise config.ContractError(
                f"契约 {metric_id} 的 deprecation.replaced_by='{replaced_by}' 指向不存在的指标"
            )

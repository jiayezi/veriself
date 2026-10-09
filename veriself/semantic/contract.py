"""指标契约的 Pydantic v2 模型与加载期强校验（IFACE-v1 第 2 节）。

`load_contracts` 会强制五条规则（契约 §2）：
1. `metric_id` 唯一；
2. `formula_sql` 里每个 `表.列` 必须在 `lineage.sources` 里，每个 `metric('x')` 必须在
   `lineage.upstream_metrics` 里（先剥离 `metric(...)` 再扫，避免 metric_id 被误判成表名）；
3. `status=deprecated` 时 `deprecation.replaced_by` 必填（且必须指向已存在的指标）；
4. `grain` / `direction` / `unit` / `agg` / `status` / `rls_policy` 必须 ∈ 各自枚举；
5. `upstream_metrics` 引用的指标必须存在，且禁止环。

任何失败一律抛 `config.ContractError`（启动即失败，不允许半可用状态）。

`contract_hash` **不在这里实现**：唯一权威实现在 `veriself/contract_hash.py`（Lead 所有），
哈希输入是**原始 YAML 解析出的 dict**（不含任何附加键），保证与 `warehouse` 写入
`dim_metric.contract_hash` / `fact_metric_value.contract_hash` 的值完全一致。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from veriself import config, semantic_model, sqlrefs
from veriself.contract_hash import contract_hash
from veriself.semantic import lineage as lineage_mod

__all__ = [
    "AGG_VALUES",
    "AS_OF_DEFINITION",
    "BUCKET_AGGS",
    "RLS_POLICIES",
    "STATUS_VALUES",
    "BucketSpec",
    "Deprecation",
    "MetricContract",
    "MetricLineage",
    "load_contracts",
    "load_definition_versions",
    "metric_contract_from_mapping",
]

_log = logging.getLogger(__name__)

#: 契约冻结日期（IFACE-v1），审计头 `as_of_definition`。
AS_OF_DEFINITION = "2026-10-01"

#: 跨时间粒度上卷方式（契约 §2）
AGG_VALUES: tuple[str, ...] = ("sum", "mean", "min", "max", "last")
STATUS_VALUES: tuple[str, ...] = ("draft", "active", "deprecated")
RLS_POLICIES: tuple[str, ...] = ("owner_only", "aggregate_min5", "no_pii")

#: 桶内聚合方式（契约 §2 `bucket.agg`）。与跨粒度上卷 AGG_VALUES 同枚举，
#: 但语义是"grain 桶内把日粒度值聚成一个桶值"。
BUCKET_AGGS: tuple[str, ...] = AGG_VALUES

#: YAML 里不允许出现的保留键（由加载器自己计算，避免"自证"式伪造）
_RESERVED_KEYS = ("contract_hash", "source_file")

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


class MetricLineage(BaseModel):
    """指标的来源声明（契约 §2 `lineage` 块）。"""

    model_config = ConfigDict(extra="forbid")

    sources: list[str] = Field(default_factory=list)
    upstream_metrics: list[str] = Field(default_factory=list)


class BucketSpec(BaseModel):
    """非日粒度指标的**桶内聚合**声明（契约 §2 可选块）。

    `agg`（契约顶层的同名字段）是**跨粒度上卷**方式；这里的 `agg` 是
    "grain 桶内如何把日粒度值聚成一个桶值"——两者语义分离。
    `last` 表示取桶内最后一个有数据日的值（`arg_max(value, date_key)`）。
    """

    model_config = ConfigDict(extra="forbid")

    agg: str


class MetricContract(BaseModel):
    """指标契约（字段与契约 §2 的 YAML schema 一一对应）。

    `contract_hash` / `source_file` 是加载器算出来的附加字段，**不参与哈希输入**。
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
    formula_sql: str
    bucket: BucketSpec | None = None
    lineage: MetricLineage = Field(default_factory=MetricLineage)
    deprecation: Deprecation | None = None

    # 加载器附加（不参与 contract_hash）
    contract_hash: str = ""
    source_file: str = ""

    #: 原始 YAML dict（哈希输入，也是 `describe_metric` 追溯口径的凭证）
    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        """原始 YAML dict（只读副本），审计/追溯用。"""
        return dict(self._raw)

    @property
    def upstream_metrics(self) -> list[str]:
        """便捷访问 `lineage.upstream_metrics`。"""
        return list(self.lineage.upstream_metrics)

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


def _check_column_exists(
    models: Mapping[str, Any],
    qualifier: str,
    column: str,
    metric_id: str,
) -> None:
    """严格列存在性校验：`限定名.列` 必须落在某个语义模型的逻辑列里。

    限定名可以是物理表名（`fact_observation`）或查询别名（`o`）；两者都由
    `semantic_models/*.yml` 声明。typo（如 `fact_observation.sleep_hour`）
    在这里被拒——把"物化期 Binder Error"变成"加载期 ContractError"。
    """
    model = semantic_model.resolve_model(models, qualifier)
    if model is None:
        raise config.ContractError(
            f"契约 {metric_id} 引用了 '{qualifier}.{column}'，"
            f"但 '{qualifier}' 不是任何语义模型的表名或别名"
        )
    if column not in model.column_names:
        raise config.ContractError(
            f"契约 {metric_id} 引用了 '{qualifier}.{column}'，"
            f"但语义模型 '{model.model_id}' 没有逻辑列 '{column}'"
        )


def _check_formula_refs(
    contract: MetricContract,
    models: Mapping[str, Any] | None = None,
) -> None:
    """契约 §2 规则 2：`表.列` 与 `metric('x')` 都必须有声明。

    `models` 提供语义模型集时，在声明检查之后追加**真实列存在性校验**。
    """
    formula = contract.formula_sql
    if not formula or not formula.strip():
        raise config.ContractError(f"契约 {contract.metric_id} 的 formula_sql 为空")
    upstream = set(contract.lineage.upstream_metrics)
    for metric_id in lineage_mod.metric_calls(formula):
        if metric_id not in upstream:
            raise config.ContractError(
                f"契约 {contract.metric_id} 的 formula_sql 引用了 metric('{metric_id}')，"
                f"但它不在 lineage.upstream_metrics {sorted(upstream)} 中"
            )
    for qualifier, column in lineage_mod.column_refs(formula):
        if not lineage_mod.source_covers(contract.lineage.sources, qualifier, column):
            raise config.ContractError(
                f"契约 {contract.metric_id} 的 formula_sql 引用了 '{qualifier}.{column}'，"
                f"但它不在 lineage.sources {contract.lineage.sources} 中"
            )
        if models is not None:
            _check_column_exists(models, qualifier, column, contract.metric_id)

    # lineage.sources 里的每一项也必须真实存在于语义模型（声明本身不许 typo）
    if models is not None:
        for entry in contract.lineage.sources:
            qualifier, sep, column = str(entry).partition(".")
            if not sep:
                raise config.ContractError(
                    f"契约 {contract.metric_id} 的 lineage.sources 项 '{entry}' 不是 '表.列' 形式"
                )
            _check_column_exists(models, qualifier, column, contract.metric_id)


def _check_bucket(contract: MetricContract) -> None:
    """`bucket` 可选块的校验。

    1. `bucket.agg` 必须 ∈ 枚举；
    2. 只允许用于非日粒度指标（grain=day 时语义含糊，拒绝）；
    3. 与手写窗口公式互斥——声明了 bucket 就由物化器自动做桶聚合，
       formula_sql 必须是**日粒度标量表达式**，再写 `OVER(...)` 会产生两套机制。
    """
    bucket = contract.bucket
    if bucket is None:
        return
    if bucket.agg not in BUCKET_AGGS:
        raise config.ContractError(
            f"契约 {contract.metric_id} 的 bucket.agg='{bucket.agg}' 不在枚举 {list(BUCKET_AGGS)} 中"
        )
    if contract.grain == "day":
        raise config.ContractError(
            f"契约 {contract.metric_id} 的 grain=day 不允许声明 bucket（桶聚合只用于非日粒度指标）"
        )
    if sqlrefs.uses_window(contract.formula_sql):
        raise config.ContractError(
            f"契约 {contract.metric_id} 声明了 bucket 后 formula_sql 不得再写窗口函数"
            "（桶聚合由物化器自动生成，手写 OVER 会产生两套机制）"
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
    models: Mapping[str, Any] | None = None,
) -> MetricContract:
    """从原始 dict 构造并校验单个契约（不做跨契约检查）。

    哈希输入就是传入的 `raw`；测试与程序化构造都走这里，保证与 YAML 加载同一条路径。
    `models` 为语义模型集；None 时自动加载 `semantic_models/` 并做严格列存在性校验。
    显式传空 `{}` 可跳过模型校验（测试构造用）。
    """
    if not isinstance(raw, Mapping):
        raise config.ContractError(f"契约 {source_file} 必须是映射，实际是 {type(raw).__name__}")
    payload = dict(raw)
    for reserved in _RESERVED_KEYS:
        if reserved in payload:
            raise config.ContractError(f"契约 {source_file} 不允许出现保留键 '{reserved}'")
    _check_metric_id(payload, source_file)
    _check_enums(payload, source_file)
    try:
        contract = MetricContract(**payload)
    except ValidationError as exc:
        raise config.ContractError(f"契约 {source_file} 字段校验失败：{exc}") from exc
    _check_attribute_names(contract)
    _check_formula_refs(contract, models if models is not None else semantic_model.default_models())
    _check_bucket(contract)
    # 规则 3：deprecated 必须给 replaced_by
    if contract.status == "deprecated":
        replaced_by = contract.deprecation.replaced_by if contract.deprecation else None
        if not replaced_by:
            raise config.ContractError(
                f"契约 {contract.metric_id} status=deprecated，必须提供 deprecation.replaced_by"
            )
    contract.contract_hash = contract_hash(payload)
    contract.source_file = source_file
    contract._raw = payload
    return contract


def load_contracts(metrics_dir: Path | None = None) -> dict[str, MetricContract]:
    """加载并校验 `metrics/*.yml`，返回 `{metric_id: MetricContract}`。

    失败（重复 metric_id / 环 / 非法枚举 / 未声明引用 / deprecated 缺 replaced_by）抛
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
        contract = metric_contract_from_mapping(raw, source_file=path.name)
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
    同一 `(metric_id, version)` 出现两次即拒绝。历史契约的 `upstream_metrics`
    必须能在当前目录里找到，但不把历史版本自己放进查询用的契约表。
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
        contract = metric_contract_from_mapping(raw, source_file=f"history/{path.name}")
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
        missing = [
            metric_id for metric_id in contract.lineage.upstream_metrics if metric_id not in current
        ]
        if missing:
            raise config.ContractError(
                f"history/{path.name} 的 upstream_metrics 引用了当前目录中不存在的指标 {missing}"
            )
        versions.append(contract)
        seen.add(key)
    _log.info("口径版本 %d 个（含 history %d 个）：%s", len(versions), len(paths), directory)
    return versions


def _check_cross_contracts(contracts: Mapping[str, MetricContract]) -> None:
    """契约 §2 规则 5（上游存在 + 无环）与 replaced_by 的可追溯性。"""
    upstream = {metric_id: list(c.lineage.upstream_metrics) for metric_id, c in contracts.items()}
    try:
        lineage_mod.topological_layers(upstream)
    except lineage_mod.LineageError as exc:
        raise config.ContractError(f"upstream_metrics 校验失败：{exc}") from exc
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

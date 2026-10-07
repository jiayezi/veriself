"""来源语义模型（`semantic_models/*.yml`）的加载与结构校验。

契约 `formula_sql` 引用的**逻辑列**在这里声明（业界对应物：dbt semantic_models.yml /
Cube cubes.yml）。三类模型：

- `observation`（table=fact_observation）：通道窄表 → 日粒度列，每列声明 `channel + agg`
  （当日多条观测压成一个日值的聚合方式）；
- `event`（table=fact_event）：事件表 → 日粒度逻辑列，每列是自由 `expr`；
- `subject`（table=dim_subject，type=dimension）：SCD2 当前版本（is_current）的维度列。

消费方（依赖方向合法，均只 import 本模块）：

- `materializer`：生成 `obs_daily` / `evt_daily` / `subject_current` 视图与求值列清单；
- `semantic/contract.py`：加载期做**真实列存在性校验**（typo 如
  `fact_observation.sleep_hour` 在加载期即拒绝）。

所有结构校验在此完成（fail-closed，一律抛 `config.ContractError`）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from veriself import config

__all__ = [
    "AGG_FUNCS",
    "MODEL_TYPES",
    "SEMANTIC_MODELS_DIR",
    "SemanticColumn",
    "SemanticModel",
    "load_semantic_models",
    "resolve_model",
]

#: 语义模型目录（不在 metrics/ 下，避免被 load_contracts 扫描到）
SEMANTIC_MODELS_DIR: Path = config.PROJECT_ROOT / "semantic_models"

#: 通道列的日聚合方式枚举（与契约 §2 的 agg 是两回事：这是"当日内多条观测→一个日值"）
AGG_FUNCS: tuple[str, ...] = ("sum", "avg", "min", "max")
MODEL_TYPES: tuple[str, ...] = ("fact", "dimension")

#: SQL 标识符形状（列名 / channel / alias 用）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SemanticColumn(BaseModel):
    """一条逻辑列：窄表列用 `channel + agg`，事件列用自由 `expr`，维度列只有 `name`。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    channel: str | None = None
    agg: str | None = None
    expr: str | None = None
    description: str = ""


class SemanticModel(BaseModel):
    """一个来源模型：某张物理表在物化求值作用域里的逻辑列集合。"""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    table: str
    type: str = "fact"                     # fact | dimension
    entity: str = "subject_id"             # 骨架 JOIN 用的主体键
    grain: str = "day"                     # 骨架日期粒度（当前全部为 day）
    date_column: str | None = None         # 日粒度日期列（dimension 模型无）
    alias: str | None = None               # 查询别名（o / e / s）
    columns: list[SemanticColumn] = Field(default_factory=list)

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(column.name for column in self.columns)

    @property
    def is_dimension(self) -> bool:
        return self.type == "dimension"


def _validate(model: SemanticModel, source_file: str) -> None:
    """结构校验：形状、枚举、唯一性。失败一律 `ContractError`（fail-closed）。"""
    name = model.model_id or model.table
    if not model.model_id or not model.table:
        raise config.ContractError(f"语义模型 {source_file} 缺少 model_id 或 table")
    if model.type not in MODEL_TYPES:
        raise config.ContractError(f"语义模型 {name} 的 type='{model.type}' 不在枚举 {list(MODEL_TYPES)} 中")
    if model.alias is not None and not _IDENT_RE.fullmatch(model.alias):
        raise config.ContractError(f"语义模型 {name} 的 alias '{model.alias}' 不是合法 SQL 标识符")
    if model.grain not in config.GRAINS:
        raise config.ContractError(
            f"语义模型 {name} 的 grain='{model.grain}' 不在枚举 {list(config.GRAINS)} 中"
        )
    if not model.columns:
        raise config.ContractError(f"语义模型 {name} 的 columns 为空")

    seen: set[str] = set()
    for column in model.columns:
        if not _IDENT_RE.fullmatch(column.name):
            raise config.ContractError(f"语义模型 {name} 的列名 '{column.name}' 不是合法 SQL 标识符")
        if column.name in seen:
            raise config.ContractError(f"语义模型 {name} 的列名 '{column.name}' 重复")
        seen.add(column.name)

        has_channel = column.channel is not None
        has_agg = column.agg is not None
        has_expr = column.expr is not None
        if model.is_dimension:
            if has_channel or has_agg or has_expr:
                raise config.ContractError(
                    f"语义模型 {name} 是 dimension，列 '{column.name}' 不允许 channel/agg/expr"
                )
            continue
        # fact 模型：channel+agg 与 expr 二者恰好其一
        if has_channel != has_agg:
            raise config.ContractError(
                f"语义模型 {name} 的列 '{column.name}' 的 channel 与 agg 必须成对出现"
            )
        if has_channel and has_expr:
            raise config.ContractError(
                f"语义模型 {name} 的列 '{column.name}' 同时声明了 channel/agg 与 expr"
            )
        if has_channel:
            if not _IDENT_RE.fullmatch(column.channel):
                raise config.ContractError(
                    f"语义模型 {name} 的列 '{column.name}' 的 channel '{column.channel}' 不是合法标识符"
                )
            if column.agg not in AGG_FUNCS:
                raise config.ContractError(
                    f"语义模型 {name} 的列 '{column.name}' 的 agg='{column.agg}' 不在枚举 {list(AGG_FUNCS)} 中"
                )
        elif not has_expr:
            raise config.ContractError(
                f"语义模型 {name} 的列 '{column.name}' 既没有 channel/agg 也没有 expr"
            )

    if model.is_dimension:
        if model.date_column is not None:
            raise config.ContractError(f"语义模型 {name} 是 dimension，不允许声明 date_column")
    elif not model.date_column:
        raise config.ContractError(f"语义模型 {name} 缺少 date_column")


def load_semantic_models(directory: Path | None = None) -> dict[str, SemanticModel]:
    """加载 `semantic_models/*.yml`，返回 `{table: SemanticModel}`。

    `directory=None` 时取 `SEMANTIC_MODELS_DIR`。校验 model_id / table / alias 唯一，
    失败一律抛 `config.ContractError`。
    """
    directory = Path(directory) if directory is not None else SEMANTIC_MODELS_DIR
    if not directory.is_dir():
        raise config.ContractError(f"语义模型目录不存在：{directory}")
    paths = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    if not paths:
        raise config.ContractError(f"语义模型目录为空：{directory}")

    models: dict[str, SemanticModel] = {}
    model_ids: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise config.ContractError(f"语义模型文件 {path.name} 无法读取：{exc}") from exc
        if not isinstance(raw, Mapping):
            raise config.ContractError(
                f"语义模型文件 {path.name} 顶层必须是映射，实际是 {type(raw).__name__}"
            )
        try:
            model = SemanticModel(**dict(raw))
        except Exception as exc:  # pydantic ValidationError → ContractError
            raise config.ContractError(f"语义模型 {path.name} 字段校验失败：{exc}") from exc
        _validate(model, path.name)

        if model.table in models:
            raise config.ContractError(f"语义模型 table 重复：'{model.table}' 出现在多个文件")
        if model.model_id in model_ids:
            raise config.ContractError(
                f"语义模型 model_id 重复：'{model.model_id}' 出现在 {model_ids[model.model_id]} 与 {path.name}"
            )
        if model.alias and model.alias in aliases:
            raise config.ContractError(
                f"语义模型 alias 重复：'{model.alias}' 出现在 {aliases[model.alias]} 与 {path.name}"
            )
        models[model.table] = model
        model_ids[model.model_id] = path.name
        if model.alias:
            aliases[model.alias] = path.name
    return models


@lru_cache(maxsize=1)
def default_models() -> dict[str, SemanticModel]:
    """语义模型的默认集（`SEMANTIC_MODELS_DIR`）。materializer / contract 的 `models=None` 走这里。"""
    return load_semantic_models()


def resolve_model(models: Mapping[str, SemanticModel], qualifier: str) -> SemanticModel | None:
    """按表名或查询别名解析契约 `表.列` 里的限定名。"""
    model = models.get(qualifier)
    if model is not None:
        return model
    for candidate in models.values():
        if candidate.alias == qualifier:
            return candidate
    return None

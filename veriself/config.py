"""项目级常量与路径（其他模块只读引用，不得修改）。"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------- 路径
_PKG_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = _PKG_DIR.parent
#: 是否处于"源码/editable 布局"（仓库根有 pyproject.toml）。
#: 安装到 site-packages 后为 False —— 那是**只读**的，数据不能往里写。
IN_SOURCE_CHECKOUT: bool = (PROJECT_ROOT / "pyproject.toml").is_file()


def _resolve_dir(env_var: str, source_dir: Path, packaged_dir: Path) -> Path:
    """目录解析三分：**环境变量 > 源码布局 > 安装后的包内默认值**。

    为什么要三分：`metrics/` 与 `semantic_models/` 在仓库根、**不在包内**。
    wheel 通过 `pyproject.toml` 的 `force-include` 把它们带到 `veriself/_defaults/`，
    但安装后 `PROJECT_ROOT` 变成 `site-packages`，`site-packages/metrics` 并不存在——
    所以必须能回退到包内那份默认值，否则 `pip install` 的用户跑 CLI 会拿到 exit 5。
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    return source_dir if source_dir.is_dir() else packaged_dir


METRICS_DIR: Path = _resolve_dir(
    "VERISELF_METRICS_DIR", PROJECT_ROOT / "metrics", _PKG_DIR / "_defaults" / "metrics"
)
#: 语义模型目录（不在 metrics/ 下，避免被 load_contracts 扫描到）。
#: 放在 config 里作为**唯一裁决点**，语义层只引用它、不再各自拼路径。
SEMANTIC_MODELS_DIR: Path = _resolve_dir(
    "VERISELF_SEMANTIC_MODELS_DIR",
    PROJECT_ROOT / "semantic_models",
    _PKG_DIR / "_defaults" / "semantic_models",
)
#: 数仓文件所在目录。源码布局下仍是 `仓库根/data`（**行为与改动前逐字一致**）；
#: 安装后写到当前工作目录下的 `data/`（可写、可预期），可用 `VERISELF_DATA_DIR` 覆盖。
DATA_DIR: Path = Path(
    os.environ.get("VERISELF_DATA_DIR")
    or (PROJECT_ROOT / "data" if IN_SOURCE_CHECKOUT else Path.cwd() / "data")
)
WAREHOUSE_PATH: Path = DATA_DIR / "warehouse.duckdb"
# 就地取包内文件：不要从 PROJECT_ROOT 拼回包目录（那样包改名/移动就会断）
SCHEMA_SQL_PATH: Path = _PKG_DIR / "warehouse" / "schema.sql"

# ---------------------------------------------------------------- 合成数据
# 3 年日粒度数据，固定种子保证可复现
SYNTH_SEED: int = 20261001
SYNTH_START_DATE: str = "2024-01-01"
SYNTH_END_DATE: str = "2026-09-30"
SUBJECT_ID: str = "S001"
SUBJECT_NAME: str = "demo-subject"
SUBJECT_SLEEP_NEED_H: float = 7.75

# ---------------------------------------------------------------- 角色与权限
class Role(str, Enum):
    """查询角色。rls_policy 与角色的映射见 audit.rls_applied。"""

    OWNER = "owner"            # 只能看自己的明细
    PARTNER = "partner"        # 只能看聚合，且分组人数 >= MIN_GROUP_SIZE
    RESEARCHER = "researcher"  # 聚合 + 禁止 PII 列

MIN_GROUP_SIZE: int = 5
PII_COLUMNS: frozenset[str] = frozenset({"name", "birth_date", "timezone"})


def rls_visible_roles(policy: str) -> frozenset[Role]:
    """rls_policy -> 允许访问的角色集合。"""
    mapping = {
        "owner_only": frozenset({Role.OWNER}),
        "aggregate_min5": frozenset({Role.OWNER, Role.PARTNER, Role.RESEARCHER}),
        "no_pii": frozenset({Role.OWNER, Role.RESEARCHER}),
    }
    if policy not in mapping:
        raise ValueError(f"unknown rls_policy: {policy}")
    return mapping[policy]


# ---------------------------------------------------------------- 强制校验
# 顺序即执行顺序，拒绝原因前缀固定，见 docs/00-接口契约.md 第 5 节
ENFORCED_CHECKS: tuple[str, ...] = (
    "registered",
    "dimensions",
    "grain",
    "ast_join_path",
    "rls",
)

REASON_PREFIXES: dict[str, str] = {
    "registered": "unknown_metric:",
    "deprecated": "deprecated_metric:",
    "dimension_not_allowed": "dimension_not_allowed:",
    "filter_not_allowed": "filter_not_allowed:",
    "grain": "grain_not_compatible:",
    "ast": "ast_violation:",
    "scan": "scan_too_large:",
}

# 编译后 EXPLAIN 预检：估算行数上限
SCAN_LIMIT_HINT: float = 1e7
DEFAULT_LIMIT: int = 1000

# ---------------------------------------------------------------- grain
GRAINS: tuple[str, ...] = ("day", "week", "month", "quarter")
GRAIN_ORDER: dict[str, int] = {g: i for i, g in enumerate(GRAINS)}
DIRECTIONS: tuple[str, ...] = ("higher_better", "lower_better", "neutral")
UNITS: tuple[str, ...] = ("hour", "count", "score", "currency", "ratio", "minute")

# ---------------------------------------------------------------- 异常
class VeriselfError(Exception):
    """项目异常基类。"""


class ContractError(VeriselfError):
    """指标契约加载/校验失败。"""


class EnforcementError(VeriselfError):
    """强制校验拒绝。reason 必须以 config.REASON_PREFIXES 中的前缀开头。"""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail}")


class QueryError(VeriselfError):
    """查询对象本身非法（缺字段、类型错误、含疑似 SQL 注入片段）。"""

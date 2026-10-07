"""项目级常量与路径（其他模块只读引用，不得修改）。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------- 路径
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = PROJECT_ROOT / "data"
METRICS_DIR: Path = PROJECT_ROOT / "metrics"
WAREHOUSE_PATH: Path = DATA_DIR / "warehouse.duckdb"
# 就地取包内文件：不要从 PROJECT_ROOT 拼回包目录（那样包改名/移动就会断）
SCHEMA_SQL_PATH: Path = Path(__file__).resolve().parent / "warehouse" / "schema.sql"

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

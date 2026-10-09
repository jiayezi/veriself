"""`interfaces` 层测试：CLI（Typer CliRunner）· MCP schema · 源码静态扫描。

三个原则：

1. **不依赖队友产物**：`semantic` / `warehouse` / `synth` / `materializer` 由其他
   teammate 并行开发，这里一律用 `monkeypatch` 注入假实现（只覆盖 interfaces 依赖的
   公开 API），并刻意使用"未冻结"的签名（如 `generate_all()` 无参）来验证适配层。
   异常类直接用冻结的 `veriself.config`，保证退出码映射与真实环境一致。
2. **MCP 没有原生语句通道**：断言 4 个工具名、`query_metric` 的入参 schema 只允许
   metrics/dimensions/filters/grain/order_by/limit，且 `additionalProperties = false`；
   再真调一次工具，证明多传的字段进不了请求对象。
3. **本层不拼业务查询**：对整个 `veriself/interfaces/` 做源码扫描
   （不得出现查询关键字、不得引用业务表、只有 gateway/auditlog 能出现 duckdb）。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 让测试在只设 PYTHONPATH=vendor 时也能 import 项目包
    sys.path.insert(0, str(ROOT))

from veriself import config
from veriself.interfaces import auditlog, cli, gateway, mcp_server, render

# 目录从已导入模块就地取，不在测试里重拼包路径（包改名/移动都不会断）。
INTERFACES_DIR = Path(render.__file__).resolve().parent
INTERFACE_FILES = sorted(INTERFACES_DIR.glob("*.py"))
BUSINESS_TABLES = (
    "fact_observation",
    "fact_event",
    "fact_metric_value",
    "fact_subject_day",
    "dim_date",
    "dim_subject",
    "dim_source",
    "dim_metric",
)
runner = CliRunner()

#: 假 semantic 返回的查询结果（interfaces 只负责搬运与展示）。
QUERY_ROWS = [
    {"date.weekday": "Monday", "metric_id": "subject.sleep_debt_7d", "value": 1.25},
    {"date.weekday": "Tuesday", "metric_id": "subject.sleep_debt_7d", "value": 0.5},
]

#: 用仓库内 gitignore 的专属目录，而不是 pytest 的 `tmp_path`：
#: 产物留在 `data/` 下便于排查，且不依赖系统临时目录是否可写（docs/02 允许中间产物放 data/）。
#: 目录名带 interfaces 前缀，避免并行跑测试时与其他测试的 `data/` 临时区互相踩到。
_TMP_ROOT = ROOT / "data" / "_interfaces_test_tmp"


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """覆盖内置 `tmp_path`：给每个用例在 `data/_interfaces_test_tmp/` 下开一个隔离目录。"""
    safe_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", request.node.name)[:60] or "case"
    path = _TMP_ROOT / safe_name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _source_text(path: Path) -> str:
    """读源码并小写化；剔除覆盖率注释（其 `pragma` 字样会与查询关键字黑名单撞名）。"""
    text = re.sub(r"#\s*pragma:[^\n]*", "", path.read_text(encoding="utf-8"))
    return text.lower()


# =========================================================================== 假实现


class FakeContract:
    """最小契约替身：只带 interfaces 展示层会用到的字段。"""

    def __init__(self, metric_id: str, **kwargs: Any) -> None:
        self.metric_id = metric_id
        self.display_name = kwargs.get("display_name", metric_id)
        self.unit = kwargs.get("unit", "score")
        self.direction = kwargs.get("direction", "higher_better")
        self.grain = kwargs.get("grain", "day")
        self.agg = kwargs.get("agg", "mean")
        self.status = kwargs.get("status", "active")
        self.version = kwargs.get("version", 1)
        self.contract_hash = kwargs.get("contract_hash", "sha256:0123456789abcdef")
        self.rls_policy = kwargs.get("rls_policy", "owner_only")
        self.owner = kwargs.get("owner", "jiayezi")
        self.definition = kwargs.get("definition", f"{metric_id} 的口径说明（测试替身）")
        self.formula_sql = kwargs.get("formula_sql", "mean(coalesce(o.sleep_hours, 0))")
        self.lineage = kwargs.get(
            "lineage",
            {"sources": ["fact_observation.sleep_hours"], "upstream_metrics": []},
        )
        self.allowed_dimensions = list(kwargs.get("allowed_dimensions", ["date.weekday", "date.month"]))
        self.allowed_filters = list(kwargs.get("allowed_filters", ["date.between", "date.last_n_days"]))


CONTRACTS: dict[str, FakeContract] = {
    "subject.sleep_debt_7d": FakeContract(
        "subject.sleep_debt_7d",
        display_name="近7日睡眠债",
        unit="hour",
        direction="lower_better",
        agg="sum",
        rls_policy="owner_only",
        allowed_dimensions=["date.weekday"],
        allowed_filters=["date.between", "date.last_n_days"],
        lineage={
            "sources": ["fact_observation.sleep_hours", "dim_subject.sleep_need_h"],
            "upstream_metrics": ["subject.sleep_need_deviation_daily"],
        },
    ),
    "subject.daily_steps": FakeContract(
        "subject.daily_steps",
        display_name="日步数",
        unit="count",
        direction="higher_better",
        rls_policy="aggregate_min5",
    ),
    "subject.focus_score_daily": FakeContract(
        "subject.focus_score_daily",
        display_name="专注度",
        unit="score",
        rls_policy="no_pii",
        allowed_dimensions=[],
        allowed_filters=[],
    ),
}


def _fake_semantic(
    contracts: dict[str, Any] | None = None,
    calls: dict[str, Any] | None = None,
    *,
    enforce: bool = True,
) -> SimpleNamespace:
    """构造 semantic 模块的假实现（契约第 7 节公开 API 的测试替身）。

    Args:
        contracts: 契约表，默认 `CONTRACTS`。
        calls: 调用记录（测试用它断言"接口层确实走了 semantic"）。
        enforce: False 时故意不复核校验，用来验证 `veriself reject` 的红队探针。
    """
    table: dict[str, Any] = dict(CONTRACTS if contracts is None else contracts)
    log: dict[str, Any] = {} if calls is None else calls

    def load_contracts(metrics_dir: Any = None) -> dict[str, Any]:
        log["load_contracts"] = True
        log["metrics_dir"] = None if metrics_dir is None else str(metrics_dir)
        return dict(table)

    def list_metrics(loaded: dict[str, Any]) -> list[dict[str, Any]]:
        log["list_metrics"] = True
        keys = ("metric_id", "display_name", "unit", "direction", "grain", "status", "version")
        return [{key: getattr(contract, key) for key in keys} for contract in loaded.values()]

    def describe_metric(loaded: dict[str, Any], metric_id: str) -> dict[str, Any]:
        log["describe_metric"] = metric_id
        if metric_id not in loaded:
            raise config.EnforcementError("registered", f"unknown_metric: {metric_id}")
        return dict(vars(loaded[metric_id]))

    class Request:
        """`QueryRequest` 替身：解析 JSON + 注入面检查 + 未知字段检查。"""

        ALLOWED: ClassVar[set[str]] = set(mcp_server.QUERY_FIELDS)

        def __init__(self, **payload: Any) -> None:
            self.metrics = [str(item) for item in payload.get("metrics") or []]
            self.dimensions = [str(item) for item in payload.get("dimensions") or []]
            self.filters = dict(payload.get("filters") or {})
            self.grain = payload.get("grain")
            self.order_by = [dict(item) for item in payload.get("order_by") or []]
            self.limit = int(payload.get("limit") or config.DEFAULT_LIMIT)

        @classmethod
        def from_json(cls, raw: Any) -> Request:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            log["raw_json"] = text
            if re.search(r"(?i)\b(select|drop|delete|insert|update|create|alter)\b|--|;", text):
                raise config.QueryError(f"illegal_query_object: 查询对象含疑似注入片段：{text[:80]}")
            unknown = sorted(set(payload) - cls.ALLOWED)
            if unknown:
                raise config.QueryError(f"illegal_query_object: 未知字段 {unknown}")
            return cls(**payload)

    def compile_query(request: Request, loaded: dict[str, Any], role: Any = config.Role.OWNER) -> SimpleNamespace:
        log["role"] = role
        log["request"] = request
        if enforce:
            for metric_id in request.metrics:
                if metric_id not in loaded:
                    raise config.EnforcementError("registered", f"unknown_metric: {metric_id}")
            for metric_id in request.metrics:
                contract = loaded[metric_id]
                if getattr(contract, "status", "active") == "deprecated":
                    raise config.EnforcementError("registered", f"deprecated_metric: {metric_id}")
                for dimension in request.dimensions:
                    if dimension not in contract.allowed_dimensions:
                        raise config.EnforcementError(
                            "dimensions",
                            f"dimension_not_allowed: {dimension}（metric={metric_id}，"
                            f"允许 {contract.allowed_dimensions}）",
                        )
                for key in request.filters:
                    if key not in contract.allowed_filters:
                        raise config.EnforcementError(
                            "dimensions",
                            f"filter_not_allowed: {key}（metric={metric_id}，"
                            f"允许 {contract.allowed_filters}）",
                        )
        log["compiled"] = True
        return SimpleNamespace(
            request=request,
            sql="compiled-by-semantic（由队友的 semantic 生成，interfaces 只搬运）",
            params=[],
            metric_versions={mid: getattr(loaded.get(mid), "version", 1) for mid in request.metrics},
            contract_hashes={
                mid: getattr(loaded.get(mid), "contract_hash", "sha256:unknown") for mid in request.metrics
            },
            rls_applied=sorted({getattr(loaded.get(mid), "rls_policy", "owner_only") for mid in request.metrics}),
            enforced_checks=list(config.ENFORCED_CHECKS),
        )

    def _visible(metric_id: str, role: Any) -> bool:
        policy = getattr(table.get(metric_id), "rls_policy", "owner_only")
        try:
            return role in config.rls_visible_roles(policy)
        except ValueError:
            return False

    def execute_query(
        compiled: SimpleNamespace,
        role: Any = config.Role.OWNER,
        conn: Any = None,
        audit: bool = True,
    ) -> dict[str, Any]:
        log["executed"] = True
        log["execute_role"] = role
        log["audit_flag"] = audit
        log["conn"] = conn
        visible = all(_visible(mid, role) for mid in compiled.request.metrics)
        return {
            "data": [dict(row) for row in QUERY_ROWS] if visible else [],
            "audit": {
                "metric_versions": dict(compiled.metric_versions),
                "contract_hashes": dict(compiled.contract_hashes),
                "compiled_sql": compiled.sql,
                "rls_applied": list(compiled.rls_applied),
                "enforced_checks": list(config.ENFORCED_CHECKS),
                "as_of_definition": "2026-10-01",
                "queried_at": "2026-10-01T16:40:00+08:00",
            },
        }

    return SimpleNamespace(
        load_contracts=load_contracts,
        list_metrics=list_metrics,
        describe_metric=describe_metric,
        QueryRequest=Request,
        compile_query=compile_query,
        execute_query=execute_query,
    )


@pytest.fixture
def semantic_fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """注入假 semantic，返回调用记录。"""
    calls: dict[str, Any] = {}
    module = _fake_semantic(calls=calls)
    monkeypatch.setattr(gateway, "semantic", lambda: module)
    return calls


@pytest.fixture
def warehouse_fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """假 warehouse：签名故意用未冻结的 `ensure_schema(conn)` / `upsert_dim_metric(conn, contract)`。"""
    events: dict[str, Any] = {}

    def ensure_schema(conn: Any) -> None:
        events["ensure_schema"] = conn

    def upsert_dim_metric(conn: Any, contract: Any) -> None:
        events.setdefault("contracts", []).append(contract.metric_id)

    module = SimpleNamespace(
        loader=SimpleNamespace(ensure_schema=ensure_schema, upsert_dim_metric=upsert_dim_metric)
    )
    monkeypatch.setattr(gateway, "warehouse", lambda: module)
    return events


@pytest.fixture
def materializer_fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """假 materializer（契约第 8 节签名）。"""
    events: dict[str, Any] = {}

    def materialize_all(conn: Any, contracts: dict[str, Any]) -> dict[str, dict[str, int]]:
        """契约第 8 节签名：无 `replace` 形参，返回逐指标统计字典。"""
        events["count"] = len(contracts)
        return {
            metric_id: {
                "live": 100 + index,
                "inserted": 0,
                "closed_changed": 0,
                "closed_vanished": 0,
                "unchanged": 100 + index,
            }
            for index, metric_id in enumerate(contracts)
        }

    monkeypatch.setattr(
        gateway, "materializer", lambda: SimpleNamespace(materialize_all=materialize_all)
    )
    return events


@pytest.fixture
def synth_fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """假 synth：`generate_all()` 故意不带参数，验证适配层能按签名调用。"""
    events: dict[str, Any] = {}

    def generate_all() -> dict[str, int]:
        events["called"] = True
        return {"fact_observation": 25920, "fact_event": 3000}

    monkeypatch.setattr(gateway, "synth", lambda: SimpleNamespace(generate_all=generate_all))
    return events


@pytest.fixture
def audit_db(tmp_path: Path) -> Path:
    """建一个只含审计表的临时数仓（测试脚手架；interfaces 层自己不允许写这种语句）。"""
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "create table fact_audit_log("
        "audit_id bigint, queried_at timestamp, actor_role varchar, request_json varchar, "
        "compiled_sql varchar, metric_versions varchar, contract_hashes varchar, "
        "rls_applied varchar, checks_passed varchar, outcome varchar)"
    )
    connection.execute(
        "insert into fact_audit_log values "
        "(1, '2026-10-01 16:40:00', 'owner', '{\"metrics\":[\"subject.sleep_debt_7d\"]}', "
        "'compiled', '{\"subject.sleep_debt_7d\":1}', '{\"subject.sleep_debt_7d\":\"sha256:x\"}', "
        "'owner_only', 'registered', 'ok')"
    )
    connection.execute(
        "insert into fact_audit_log values "
        "(2, '2026-10-01 16:41:00', 'owner', '{\"metrics\":[\"subject.nope\"]}', "
        "null, null, null, null, 'registered', 'rejected:unknown_metric')"
    )
    connection.close()
    return path


def _tool_json(result: Any) -> dict[str, Any]:
    """从 MCP `CallToolResult` 里取出工具返回的 JSON 文本。"""
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    raise AssertionError(f"工具返回里没有可解析的 JSON：{result!r}")


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """同步调用 MCP 工具并解析 JSON。"""
    result = asyncio.run(mcp_server.server.call_tool(name, arguments))
    return _tool_json(result)


def _tools() -> list[Any]:
    """列出 MCP server 注册的工具。"""
    return asyncio.run(mcp_server.server.list_tools())


def _tool(name: str) -> Any:
    for item in _tools():
        if item.name == name:
            return item
    raise AssertionError(f"没有注册工具 {name}")


# =========================================================================== CLI：帮助与用法


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["init", "--help"],
        ["synth", "--help"],
        ["query", "--help"],
        ["audit", "--help"],
        ["reject", "--help"],
        ["demo", "--help"],
        ["mcp", "--help"],
        ["metrics", "--help"],
        ["metrics", "list", "--help"],
        ["metrics", "show", "--help"],
    ],
)
def test_help_routes(args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_root_help_lists_all_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "synth", "query", "audit", "reject", "demo", "mcp", "metrics"):
        assert command in result.output, result.output


def test_query_command_has_no_native_query_option() -> None:
    from typer.main import get_command

    command = cast(Any, get_command(cli.app))
    options = {opt for param in command.commands["query"].params for opt in getattr(param, "opts", [])}
    assert {"--metrics", "--dimensions", "--filters", "--grain", "--order-by", "--limit", "--role"} <= options
    assert not [name for name in options if "sql" in name.lower() or "native" in name.lower()]


# =========================================================================== CLI：metrics


def test_metrics_list_renders_every_contract(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["metrics", "list"])
    assert result.exit_code == 0, result.output
    for metric_id in CONTRACTS:
        assert metric_id in result.output
    assert "近7日睡眠债" in result.output
    assert "共 3 个指标" in result.output
    assert semantic_fake["load_contracts"] is True


def test_metrics_list_on_empty_contracts_does_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic(contracts={}))
    result = runner.invoke(cli.app, ["metrics", "list", "--metrics-dir", str(tmp_path / "empty")])
    assert result.exit_code == 0, result.output
    assert "契约目录为空" in result.output


def test_metrics_list_json(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["metrics", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == len(CONTRACTS)
    assert {row["metric_id"] for row in payload["metrics"]} == set(CONTRACTS)


def test_metrics_show_prints_lineage_and_contract_hash(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["metrics", "show", "subject.sleep_debt_7d"])
    assert result.exit_code == 0, result.output
    assert "contract_hash" in result.output
    assert "sha256:0123456789abcdef" in result.output
    assert "血缘" in result.output
    assert "dim_subject.sleep_need_h" in result.output
    assert "subject.sleep_need_deviation_daily" in result.output
    assert "date.weekday" in result.output


def test_metrics_show_unknown_metric_suggests_and_fails(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["metrics", "show", "subject.sleep_debt_7day"])
    assert result.exit_code != 0
    assert "unknown_metric: subject.sleep_debt_7day" in result.output
    assert "subject.sleep_debt_7d" in result.output  # difflib 建议
    assert "reason:" in result.output


# =========================================================================== CLI：query


def test_query_json_matches_contract_structure(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app,
        [
            "query",
            "--metrics", "subject.sleep_debt_7d",
            "--dimensions", "date.weekday",
            "--filters", '{"date.between": ["2026-01-01", "2026-03-31"]}',
            "--role", "owner",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"data", "audit"}  # 契约第 4 节
    assert payload["data"][0]["value"] == 1.25
    audit = payload["audit"]
    assert audit["enforced_checks"] == list(config.ENFORCED_CHECKS)
    assert audit["contract_hashes"]["subject.sleep_debt_7d"].startswith("sha256:")
    assert audit["rls_applied"] == ["owner_only"]
    assert audit["as_of_definition"] == "2026-10-01"
    # 接口层把请求原样交给 semantic
    assert semantic_fake["request"].metrics == ["subject.sleep_debt_7d"]
    assert semantic_fake["request"].dimensions == ["date.weekday"]
    assert semantic_fake["compiled"] is True and semantic_fake["executed"] is True


def test_query_rich_output_has_audit_panel(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d"])
    assert result.exit_code == 0, result.output
    assert "审计头" in result.output
    assert "强制校验" in result.output
    assert "date.weekday" in result.output


def test_query_unknown_metric_exits_nonzero(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query", "--metrics", "subject.nope", "--json"])
    assert result.exit_code != 0
    assert "unknown_metric: subject.nope" in result.output
    assert '"ok": false' in result.output  # --json 模式额外给机器可读信封


def test_query_dimension_not_allowed_exits_nonzero(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--dimensions", "date.quarter"])
    assert result.exit_code != 0
    assert "dimension_not_allowed: date.quarter" in result.output
    assert semantic_fake.get("executed") is None  # 被拒绝的请求绝不执行


def test_query_filter_not_allowed_exits_nonzero(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app,
        ["query", "--metrics", "subject.sleep_debt_7d", "--filters", '{"date.last_90_days": 90}'],
    )
    assert result.exit_code != 0
    assert "filter_not_allowed" in result.output


def test_query_rejects_injection_in_filter_values(semantic_fake: dict[str, Any]) -> None:
    """防注入面由 semantic 的 `QueryRequest.from_json` 负责，接口层只是原样递 JSON。"""
    result = runner.invoke(
        cli.app,
        [
            "query",
            "--metrics", "subject.sleep_debt_7d",
            "--filters", '{"date.between": ["2026-01-01", "2026-03-31; drop table fact_observation"]}',
        ],
    )
    assert result.exit_code != 0
    assert "illegal_query_object" in result.output
    assert semantic_fake.get("compiled") is None


def test_query_bad_json_argument_is_query_error(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--filters", "{not json}"])
    assert result.exit_code == 4
    assert "invalid_query_object" in result.output


def test_query_requires_metrics(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query"])
    assert result.exit_code != 0
    assert "metrics 不能为空" in result.output


def test_query_limit_is_validated_and_clamped(semantic_fake: dict[str, Any]) -> None:
    ok = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--limit", "99999"])
    assert ok.exit_code == 0, ok.output
    assert "截断" in ok.output
    assert semantic_fake["request"].limit == config.DEFAULT_LIMIT

    bad = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--limit", "0"])
    assert bad.exit_code == 4
    assert "limit 必须 >= 1" in bad.output


def test_query_order_by_shorthand(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app,
        ["query", "--metrics", "subject.sleep_debt_7d", "--order-by", "date.day:desc", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    assert semantic_fake["request"].order_by == [{"field": "date.day", "dir": "desc"}]


def test_query_order_by_rejects_bad_direction(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--order-by", "date.day:sideways"])
    assert result.exit_code == 4
    assert "asc/desc" in result.output


def test_query_rls_rewrites_instead_of_rejecting(semantic_fake: dict[str, Any]) -> None:
    """契约第 5 节第 5 条：rls 不拒绝，而是改写（这里体现为 partner 看不到 owner_only 明细）。"""
    result = runner.invoke(
        cli.app, ["query", "--metrics", "subject.sleep_debt_7d", "--role", "partner", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"] == []
    assert payload["audit"]["rls_applied"] == ["owner_only"]


def test_query_dimension_may_repeat_and_comma_split(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app,
        ["query", "--metrics", "subject.daily_steps", "--dimensions", "date.weekday,date.month", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert semantic_fake["request"].dimensions == ["date.weekday", "date.month"]


# =========================================================================== CLI：reject


def test_reject_unknown_metric_prints_reason_and_suggestion(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["reject", "subject.sleep_debt_7day"])
    assert result.exit_code == 3  # 与真实拒绝一致的非 0 退出码
    assert "unknown_metric: subject.sleep_debt_7day" in result.output
    assert "subject.sleep_debt_7d" in result.output  # difflib 建议
    assert "契约允许的" in result.output  # 对错比照表
    assert "正确写法" in result.output
    assert "reason:" in result.output


def test_reject_known_metric_shows_dimension_comparison(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["reject", "subject.sleep_debt_7d"])
    assert result.exit_code == 3
    assert "dimension_not_allowed" in result.output
    assert "date.hour" in result.output  # 故意提交的越界维度
    assert "date.weekday" in result.output  # 契约允许的维度
    assert "正确写法" in result.output


def test_reject_exit_zero_flag(semantic_fake: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["reject", "subject.sleep_debt_7day", "--exit-zero"])
    assert result.exit_code == 0, result.output
    assert "unknown_metric" in result.output


def test_reject_canary_when_contract_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """若 semantic 放行了非法请求，reject 必须响亮失败（红队探针）。"""
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic(enforce=False))
    result = runner.invoke(cli.app, ["reject", "subject.sleep_debt_7day"])
    assert result.exit_code != 0
    assert "enforcement_bypass" in result.output


def test_reject_without_contracts_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic(contracts={}))
    result = runner.invoke(cli.app, ["reject", "subject.sleep_debt_7d"])
    assert result.exit_code == 5
    assert "contracts_empty" in result.output
    assert "Traceback" not in result.output


# =========================================================================== CLI：audit


def test_audit_shows_recent_rows(audit_db: Path) -> None:
    result = runner.invoke(cli.app, ["audit", "--limit", "5", "--db-path", str(audit_db)])
    assert result.exit_code == 0, result.output
    assert "audit_id" in result.output
    assert "rejected:unknown_metric" in result.output
    assert "actor_role" in result.output


def test_audit_json(audit_db: Path) -> None:
    result = runner.invoke(cli.app, ["audit", "--db-path", str(audit_db), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["rows"][0]["audit_id"] == 2  # 倒序：最近的在前


def test_audit_missing_warehouse_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["audit", "--db-path", str(tmp_path / "missing.duckdb")])
    assert result.exit_code == 5
    assert "audit_log_unavailable" in result.output
    assert "Traceback" not in result.output


def test_audit_empty_log_is_ok(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "empty.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("create table fact_audit_log(audit_id bigint, outcome varchar)")
    connection.close()
    result = runner.invoke(cli.app, ["audit", "--db-path", str(path)])
    assert result.exit_code == 0, result.output
    assert "审计日志为空" in result.output


def test_auditlog_fetch_one(audit_db: Path) -> None:
    found = auditlog.fetch_one(1, audit_db)
    assert found is not None
    assert found["outcome"] == "ok"
    assert auditlog.fetch_one(999, audit_db) is None
    with pytest.raises(auditlog.AuditLogUnavailable):
        auditlog.fetch_one("not-an-id", audit_db)  # type: ignore[arg-type]


# =========================================================================== CLI：init / synth / demo


def test_init_creates_schema_contracts_and_materialization(
    semantic_fake: dict[str, Any],
    warehouse_fake: dict[str, Any],
    materializer_fake: dict[str, Any],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    result = runner.invoke(
        cli.app, ["init", "--db-path", str(db_path), "--metrics-dir", str(tmp_path / "metrics")]
    )
    assert result.exit_code == 0, result.output
    assert db_path.exists()
    assert "loader.ensure_schema" in result.output
    assert sorted(warehouse_fake["contracts"]) == sorted(CONTRACTS)
    assert materializer_fake["count"] == len(CONTRACTS)
    assert "物化行数" in result.output


def test_init_without_materializer_keeps_exit_code_zero(
    semantic_fake: dict[str, Any], warehouse_fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lead 明确要求：materializer 未就绪时只提示，不改变退出码。"""

    def _missing() -> Any:
        raise gateway.GatewayUnavailable("veriself.materializer", "ModuleNotFoundError")

    monkeypatch.setattr(gateway, "materializer", _missing)
    result = runner.invoke(
        cli.app, ["init", "--db-path", str(tmp_path / "w.duckdb"), "--metrics-dir", str(tmp_path / "metrics")]
    )
    assert result.exit_code == 0, result.output
    assert "物化未执行" in result.output
    assert "materializer" in result.output


def test_init_without_contracts_warns_but_succeeds(
    monkeypatch: pytest.MonkeyPatch, warehouse_fake: dict[str, Any], materializer_fake: dict[str, Any], tmp_path: Path
) -> None:
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic(contracts={}))
    result = runner.invoke(
        cli.app, ["init", "--db-path", str(tmp_path / "w.duckdb"), "--metrics-dir", str(tmp_path / "empty")]
    )
    assert result.exit_code == 0, result.output
    assert "没有加载到任何契约" in result.output


def test_synth_reports_summary(
    semantic_fake: dict[str, Any], warehouse_fake: dict[str, Any], synth_fake: dict[str, Any], tmp_path: Path
) -> None:
    result = runner.invoke(cli.app, ["synth", "--db-path", str(tmp_path / "w.duckdb")])
    assert result.exit_code == 0, result.output
    assert synth_fake["called"] is True
    assert "fact_observation" in result.output
    assert "veriself synth 完成" in result.output


def test_synth_missing_module_fails_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _missing() -> Any:
        raise gateway.GatewayUnavailable("veriself.synth", "ModuleNotFoundError")

    monkeypatch.setattr(gateway, "synth", _missing)
    result = runner.invoke(cli.app, ["synth", "--db-path", str(tmp_path / "w.duckdb")])
    assert result.exit_code == 5
    assert "downstream_unavailable" in result.output
    assert "Traceback" not in result.output


def test_demo_end_to_end(
    semantic_fake: dict[str, Any],
    warehouse_fake: dict[str, Any],
    materializer_fake: dict[str, Any],
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli.app, ["demo", "--db-path", str(tmp_path / "w.duckdb"), "--metrics-dir", str(tmp_path / "metrics")]
    )
    assert result.exit_code == 0, result.output
    for step in ("步骤 1/4", "步骤 2/4", "步骤 3/4", "步骤 4/4"):
        assert step in result.output
    assert "演示摘要" in result.output
    assert "subject.sleep_debt_7d" in result.output
    assert "dimension_not_allowed" in result.output  # 拒绝演示的 reason
    assert semantic_fake["executed"] is True


def test_demo_without_materializer_prints_actionable_hint(
    semantic_fake: dict[str, Any], warehouse_fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _missing() -> Any:
        raise gateway.GatewayUnavailable("veriself.materializer", "ModuleNotFoundError")

    monkeypatch.setattr(gateway, "materializer", _missing)
    result = runner.invoke(
        cli.app, ["demo", "--db-path", str(tmp_path / "w.duckdb"), "--metrics-dir", str(tmp_path / "metrics")]
    )
    assert result.exit_code == 5
    assert "请先完成 materializer" in result.output
    assert "Traceback" not in result.output


def test_demo_without_contracts_fails_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic(contracts={}))
    result = runner.invoke(
        cli.app, ["demo", "--db-path", str(tmp_path / "w.duckdb"), "--metrics-dir", str(tmp_path / "metrics")]
    )
    assert result.exit_code == 5
    assert "contracts_empty" in result.output


# =========================================================================== MCP


def test_mcp_registers_exactly_four_tools() -> None:
    assert sorted(tool.name for tool in _tools()) == [
        "describe_metric",
        "explain_result",
        "list_metrics",
        "query_metric",
    ]


def test_mcp_query_metric_schema_has_only_structured_fields() -> None:
    schema = _tool("query_metric").input_schema
    assert set(schema["properties"]) == set(mcp_server.QUERY_FIELDS)
    assert set(mcp_server.QUERY_FIELDS) == {"metrics", "dimensions", "filters", "grain", "order_by", "limit"}
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["metrics"]
    text = json.dumps(schema, ensure_ascii=False).lower()
    assert '"additionalProperties": true' not in text
    for keyword in ("select", "insert", "update", "delete", "drop", "alter", "pragma", "exec"):
        assert keyword not in text, f"query_metric 的 schema 里出现了 {keyword!r}"
    forbidden_names = {"sql", "query", "raw", "statement", "native", "where", "clause", "expression", "script"}
    assert not (set(schema["properties"]) & forbidden_names)


def test_mcp_every_tool_is_a_closed_object() -> None:
    for tool in _tools():
        assert tool.input_schema.get("additionalProperties") is False, tool.name


def test_mcp_explain_result_schema_fields() -> None:
    schema = _tool("explain_result").input_schema
    assert set(schema["properties"]) == set(mcp_server.EXPLAIN_FIELDS)
    assert schema["additionalProperties"] is False


def test_mcp_describe_metric_schema_fields() -> None:
    schema = _tool("describe_metric").input_schema
    assert set(schema["properties"]) == {"metric_id"}
    assert schema["additionalProperties"] is False


def test_mcp_tool_descriptions_do_not_offer_a_native_channel() -> None:
    dump = json.dumps(
        [{"name": tool.name, "description": tool.description, "schema": tool.input_schema} for tool in _tools()],
        ensure_ascii=False,
    ).lower()
    for keyword in ("select", "insert", "update", "delete", "drop", "alter", "pragma"):
        assert keyword not in dump


def test_mcp_list_metrics(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool("list_metrics", {})
    assert payload["ok"] is True
    assert payload["count"] == len(CONTRACTS)
    assert {row["metric_id"] for row in payload["metrics"]} == set(CONTRACTS)


def test_mcp_describe_metric(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool("describe_metric", {"metric_id": "subject.sleep_debt_7d"})
    assert payload["ok"] is True
    assert payload["metric"]["contract_hash"].startswith("sha256:")
    assert payload["metric"]["lineage"]["sources"]
    assert payload["visible_roles"] == ["owner"]


def test_mcp_describe_unknown_metric_returns_reason(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool("describe_metric", {"metric_id": "subject.nope"})
    assert payload["ok"] is False
    assert payload["reason"].startswith("unknown_metric:")
    assert payload["rule"] == "unknown_metric"


def test_mcp_query_metric_success(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool(
        "query_metric",
        {
            "metrics": ["subject.sleep_debt_7d"],
            "dimensions": ["date.weekday"],
            "filters": {"date.between": ["2026-01-01", "2026-03-31"]},
            "limit": 10,
        },
    )
    assert payload["ok"] is True
    assert payload["data"][0]["value"] == 1.25
    assert payload["audit"]["enforced_checks"] == list(config.ENFORCED_CHECKS)
    assert payload["audit"]["rls_applied"] == ["owner_only"]
    assert semantic_fake["request"].limit == 10


def test_mcp_query_metric_rejection_returns_reason(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool("query_metric", {"metrics": ["subject.nope"]})
    assert payload["ok"] is False
    assert payload["reason"].startswith("unknown_metric:")


def test_mcp_query_metric_dimension_rejection(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool("query_metric", {"metrics": ["subject.sleep_debt_7d"], "dimensions": ["date.quarter"]})
    assert payload["ok"] is False
    assert payload["reason"].startswith("dimension_not_allowed:")
    assert semantic_fake.get("executed") is None


def test_mcp_query_metric_drops_or_rejects_extra_native_field(semantic_fake: dict[str, Any]) -> None:
    """多传一个想夹带原生语句的字段：要么被 MCP 入参校验拒绝，要么被丢弃 —— 都进不了请求对象。"""
    arguments = {"metrics": ["subject.sleep_debt_7d"], "sql": "select * from fact_observation", "raw": ";--"}
    try:
        payload = _call_tool("query_metric", arguments)
        assert payload.get("ok") in (True, False)
    except Exception:  # noqa: BLE001, S110 - SDK 在入参校验阶段直接拒绝（additionalProperties=false）
        pass
    raw = str(semantic_fake.get("raw_json", ""))
    assert "fact_observation" not in raw
    assert "select" not in raw.lower()
    assert "raw" not in raw


def test_mcp_query_metric_rejects_injection(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool(
        "query_metric",
        {"metrics": ["subject.sleep_debt_7d"], "filters": {"date.between": ["2026-01-01", "2026-02-01; drop"]}},
    )
    assert payload["ok"] is False
    assert "illegal_query_object" in payload["reason"]


def test_mcp_explain_result(semantic_fake: dict[str, Any]) -> None:
    payload = _call_tool(
        "explain_result", {"metrics": ["subject.sleep_debt_7d"], "role": "partner", "as_of": "2026-03-31"}
    )
    assert payload["ok"] is True
    assert payload["role"] == "partner"
    assert payload["as_of_definition"] == "2026-03-31"
    assert payload["enforced_checks"] == list(config.ENFORCED_CHECKS)
    entry = payload["metrics"][0]
    assert entry["rollup"].startswith("请求粒度更粗时按 SUM")
    assert entry["visible_roles"] == ["owner"]
    assert payload["contract_hashes"]["subject.sleep_debt_7d"].startswith("sha256:")


def test_mcp_explain_result_unknown_metric_and_role(semantic_fake: dict[str, Any]) -> None:
    unknown = _call_tool("explain_result", {"metrics": ["subject.nope"]})
    assert unknown["ok"] is False and unknown["reason"].startswith("unknown_metric:")
    bad_role = _call_tool("explain_result", {"metrics": ["subject.daily_steps"], "role": "hacker"})
    assert bad_role["ok"] is False and "role" in bad_role["reason"]


def test_mcp_explain_result_with_audit_id(audit_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "semantic", lambda: _fake_semantic())
    monkeypatch.setattr(config, "WAREHOUSE_PATH", audit_db)
    payload = _call_tool("explain_result", {"metrics": ["subject.sleep_debt_7d"], "audit_id": 2})
    assert payload["ok"] is True
    assert payload["audit"]["outcome"] == "rejected:unknown_metric"


def test_mcp_entrypoint_module() -> None:
    """`python -m veriself.interfaces.mcp_server` 必须有可调用的 main。"""
    assert callable(mcp_server.main)
    assert mcp_server.server.name == "veriself"


def test_mcp_server_exposes_synchronous_tools_view() -> None:
    """外部（红队测试、自检脚本）要能同步取到工具名与入参 schema，不必进事件循环。"""
    fresh = mcp_server.build_server()
    names = {tool.name for tool in fresh.tools}
    assert names == {"list_metrics", "describe_metric", "query_metric", "explain_result"}
    query = next(tool for tool in fresh.tools if tool.name == "query_metric")
    assert set(query.parameters["properties"]) == set(mcp_server.QUERY_FIELDS)
    assert query.parameters["additionalProperties"] is False
    assert isinstance(fresh, mcp_server.VeriselfServer)


# =========================================================================== 静态扫描


def test_interfaces_sources_have_no_native_query_keyword() -> None:
    """整个 interfaces 包不得出现原生查询关键字（含注释/字符串）。"""
    pattern = re.compile(r"\bselect\b", re.IGNORECASE)
    for path in INTERFACE_FILES:
        assert not pattern.search(_source_text(path)), f"{path.name} 出现了原生查询关键字"


def test_mcp_module_source_is_free_of_native_keywords() -> None:
    """MCP 是"没有原生语句通道"的最强承诺面：源码里连关键字都不出现。"""
    text = _source_text(INTERFACES_DIR / "mcp_server.py")
    for keyword in ("select", "insert", "update", "delete", "drop", "alter", "pragma"):
        assert keyword not in text, f"mcp_server.py 出现了 {keyword!r}"
    assert "sql" not in text


def test_interfaces_never_names_a_business_table() -> None:
    """只有 auditlog.py 允许提到审计表；任何业务表名都不该出现在本层。

    注意：`upsert_dim_metric` 是契约里的**函数名**，不算表引用，先剔除再判定。
    """
    for path in INTERFACE_FILES:
        if path.name == "auditlog.py":
            continue
        text = _source_text(path).replace("upsert_dim_metric", "").replace("upsert_dim_metrics", "")
        for table in BUSINESS_TABLES:
            assert table not in text, f"{path.name} 引用了业务表 {table}"
    audit_text = (INTERFACES_DIR / "auditlog.py").read_text(encoding="utf-8")
    assert "fact_audit_log" in audit_text  # 唯一例外必须明写审计表名


def test_only_gateway_and_auditlog_import_duckdb() -> None:
    """业务查询必须走 semantic；能碰到 duckdb 的只有 gateway（开连接）与 auditlog（审计表）。

    这里查的是**代码引用**（`import duckdb` / `duckdb.`），不误伤 `--db-path` 帮助文本里的产品名。
    """
    reference = re.compile(r"\bimport\s+duckdb\b|\bduckdb\.")
    allowed = {"gateway.py", "auditlog.py"}
    for path in INTERFACE_FILES:
        if path.name in allowed:
            continue
        assert not reference.search(path.read_text(encoding="utf-8")), path.name
    assert reference.search((INTERFACES_DIR / "gateway.py").read_text(encoding="utf-8"))
    assert reference.search((INTERFACES_DIR / "auditlog.py").read_text(encoding="utf-8"))


def test_gateway_uses_only_frozen_semantic_api() -> None:
    """adapter 只应通过契约第 7 节的 6 个名字访问 semantic。"""
    text = (INTERFACES_DIR / "gateway.py").read_text(encoding="utf-8")
    for name in ("load_contracts", "QueryRequest", "compile_query", "execute_query", "list_metrics", "describe_metric"):
        assert name in text
    assert "veriself.semantic" in text


# =========================================================================== gateway 单元测试


def test_normalize_payload_emits_exactly_six_keys() -> None:
    payload = gateway.normalize_payload(
        metrics="subject.sleep_debt_7d,subject.daily_steps",
        dimensions="date.weekday",
        filters={"date.between": ["2026-01-01", "2026-03-31"]},
        grain="week",
        order_by="date.day:desc",
        limit=10,
    )
    assert set(payload) == set(mcp_server.QUERY_FIELDS)
    assert payload["metrics"] == ["subject.sleep_debt_7d", "subject.daily_steps"]
    assert payload["order_by"] == [{"field": "date.day", "dir": "desc"}]
    assert payload["limit"] == 10


def test_normalize_payload_clamps_limit_and_rejects_bad_values() -> None:
    assert gateway.normalize_payload(metrics=["a"], limit=10**9)["limit"] == config.DEFAULT_LIMIT
    assert gateway.normalize_payload(metrics=["a"])["limit"] == config.DEFAULT_LIMIT
    with pytest.raises(config.QueryError):
        gateway.normalize_payload(metrics=[])
    with pytest.raises(config.QueryError):
        gateway.normalize_payload(metrics=["a"], limit=0)
    with pytest.raises(config.QueryError):
        gateway.normalize_payload(metrics=["a"], filters=["not", "a", "mapping"])
    with pytest.raises(config.QueryError):
        gateway.normalize_payload(metrics=["a"], order_by="date.day:sideways")


def test_rejection_reason_handles_both_enforcement_conventions() -> None:
    assert gateway.rejection_reason(config.EnforcementError("registered", "unknown_metric: x")) == "unknown_metric: x"
    assert gateway.rejection_reason(config.EnforcementError("unknown_metric", "x")) == "unknown_metric: x"
    assert gateway.rejection_reason(config.QueryError("illegal_query_object: y")) == "illegal_query_object: y"


def test_invoke_unfrozen_adapts_to_signature() -> None:
    seen: dict[str, Any] = {}

    def by_name(conn: Any) -> None:
        seen["conn"] = conn

    def by_position(connection: Any) -> None:
        seen["connection"] = connection

    def no_args() -> None:
        seen["none"] = True

    gateway.invoke_unfrozen(by_name, candidates={"conn": 1}, fallback=(9,))
    gateway.invoke_unfrozen(by_position, candidates={"conn": 2}, fallback=(2,))
    gateway.invoke_unfrozen(no_args, candidates={"conn": 3}, fallback=(3,))
    assert seen == {"conn": 1, "connection": 2, "none": True}


def test_invoke_unfrozen_reports_unadaptable_signature() -> None:
    def mystery(alpha: Any, beta: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("不应该被调用")

    with pytest.raises(gateway.GatewayUnavailable):
        gateway.invoke_unfrozen(mystery, candidates={}, fallback=(1,))


def test_upsert_dim_metrics_supports_batch_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def upsert_dim_metric(conn: Any, contracts: Any) -> None:
        calls.append((conn, contracts))

    monkeypatch.setattr(
        gateway, "warehouse", lambda: SimpleNamespace(loader=SimpleNamespace(upsert_dim_metric=upsert_dim_metric))
    )
    assert gateway.upsert_dim_metrics("conn", dict(CONTRACTS)) == len(CONTRACTS)
    assert len(calls) == 1  # 一次性批量写入


def test_result_payload_normalizes_pydantic_like_objects() -> None:
    class Result:
        def model_dump(self) -> dict[str, Any]:
            return {"data": [SimpleNamespace(value=1)], "audit": {"rls_applied": ["owner_only"]}}

    payload = gateway.result_payload(Result())
    assert payload["data"] == [{"value": 1}]
    assert payload["audit"]["rls_applied"] == ["owner_only"]


def test_render_helpers_are_tolerant() -> None:
    assert render.format_cell(None) == ""
    assert render.format_cell(True) == "true"
    assert render.format_cell(1.5) == "1.5"
    assert render.format_cell(1234567) == "1,234,567"
    assert render.format_cell("x" * 100).endswith("…")
    assert render.data_table([]) is not None
    assert render.metrics_table([]) is not None

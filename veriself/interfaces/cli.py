"""`veriself` —— veriself 的命令行入口（Typer）。

门面原则（`docs/00-接口契约.md`）：

* 用户/AI 只能提交**结构化查询对象**（第 3 节：metrics / dimensions / filters /
  grain / order_by / limit），CLI 没有任何可以传原生语句的参数；
* 本层从不拼装业务 SQL：一律经 `interfaces/gateway.py` 调用 `veriself.semantic`
  的冻结 API（第 7 节）；唯一例外是 `veriself audit` 只读审计表 `ops.audit_log`
  （第 6 节允许，实现在 `interfaces/auditlog.py`）；
* **所有失败路径都以非 0 退出码结束并把 `reason` 打到 stderr**，LLM 客户端据此判断
  "被拒绝了"，而不是靠自然语言猜。

退出码约定：

====  ==========================================================
0     成功
1     未预期错误（`SWH_DEBUG=1` 时改为抛出原始栈，便于调试）
2     用法错误（Typer/click 的参数解析失败）
3     契约强制校验拒绝（`EnforcementError`，reason 前缀见契约第 5 节）
4     查询对象本身非法（`QueryError`，含注入面检查）
5     环境/下游未就绪（`ContractError`、`GatewayUnavailable`、审计表不可读）
====  ==========================================================
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from veriself import config
from veriself.interfaces import auditlog, gateway, render

__all__ = ["app"]

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_BAD_REQUEST = 4
EXIT_ENV = 5

#: 演示默认挑用的指标（存在就用它，否则退化为契约里的第一个指标）。
_PREFERRED_DEMO_METRICS = ("subject.sleep_debt_7d", "subject.sleep_duration_daily")

#: 演示用的越界维度候选（取第一个不在白名单里的）。
_ILLEGAL_DIMENSION_CANDIDATES = (
    "date.hour",
    "subject.email",
    "subject.name",
    "date.weekday",
    "date.month",
)

#: `veriself query` 的几个长帮助文本抽成常量，保证代码行不超过 100 列。
_FILTERS_HELP = (
    '过滤器 JSON，如 \'{"date.between": ["2026-01-01", "2026-03-31"]}\'（也支持 @文件）'
)
_ORDER_BY_HELP = '排序：JSON 或简写 "date.day:asc"'
_LIMIT_HELP = f"行数上限，最大 {config.DEFAULT_LIMIT}"

#: 演示查询的窗口（天）。
_DEMO_WINDOW_DAYS = 30


# =========================================================================== 通用工具


def _fail(reason: str, code: int = EXIT_UNEXPECTED, *, json_mode: bool = False) -> NoReturn:
    """打印 `reason` 并以非 0 退出码结束（唯一的失败出口）。"""
    render.print_reason(reason, as_json=json_mode)
    raise typer.Exit(code)


def _error_code(exc: BaseException) -> int:
    """把契约异常映射到退出码（按类名兜底，兼容下游自定义的同名异常）。"""
    name = type(exc).__name__
    if isinstance(exc, config.EnforcementError) or name == "EnforcementError":
        return EXIT_REJECTED
    if isinstance(exc, config.QueryError) or name == "QueryError":
        return EXIT_BAD_REQUEST
    if isinstance(exc, config.ContractError) or name == "ContractError":
        return EXIT_ENV
    return EXIT_UNEXPECTED


@contextmanager
def _guard(*, json_mode: bool = False) -> Iterator[None]:
    """把下游异常统一翻译成「打印 reason + 非 0 退出」。"""
    try:
        yield
    except typer.Exit:
        raise
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        _fail("interrupted", 130, json_mode=json_mode)
    except auditlog.AuditLogUnavailable as exc:
        _fail(str(exc), EXIT_ENV, json_mode=json_mode)
    except gateway.GatewayUnavailable as exc:
        _fail(str(exc), EXIT_ENV, json_mode=json_mode)
    except Exception as exc:
        if gateway.is_project_error(exc):
            _fail(gateway.rejection_reason(exc), _error_code(exc), json_mode=json_mode)
        if os.environ.get("SWH_DEBUG"):
            raise
        _fail(f"internal_error: {type(exc).__name__}: {exc}", EXIT_UNEXPECTED, json_mode=json_mode)


def _split_multi(values: Sequence[str] | None) -> list[str]:
    """压平 `--metrics a,b` 与 `--metrics a --metrics b` 两种写法。"""
    items: list[str] = []
    for value in values or []:
        items.extend(part.strip() for part in str(value).split(","))
    return [item for item in items if item]


def _parse_json_option(name: str, raw: str | None) -> Any:
    """解析 JSON 参数；`@路径` 形式可从文件读取（方便 LLM 客户端投喂长请求）。"""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.startswith("@"):
        path = Path(text[1:])
        if not path.exists():
            _fail(f"invalid_query_object: {name} 指向的文件不存在：{path}", EXIT_BAD_REQUEST)
        text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(f"invalid_query_object: {name} 不是合法 JSON（{exc.msg} @ {exc.pos}）", EXIT_BAD_REQUEST)


def _parse_order_by(raw: str | None) -> Any:
    """解析 `--order-by`：支持契约第 3 节的 JSON 形式，也支持 `date.day:asc` 简写。

    规整（field/dir 合法性）统一交给 `gateway.normalize_order_by`，CLI 与 MCP 共用同一套规则。
    """
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text[:1] not in "[{":
        return text  # 简写形式，由 gateway 解析
    parsed = _parse_json_option("--order-by", text)
    if isinstance(parsed, Mapping):
        return [parsed]
    if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes)):
        return list(parsed)
    _fail("invalid_query_object: --order-by 必须是 JSON 数组、对象或 'date.day:asc' 简写", EXIT_BAD_REQUEST)


def _query_payload(
    *,
    metrics: Sequence[str] | None,
    dimensions: Sequence[str] | None,
    filters: str | None,
    order_by: str | None,
    grain: str | None,
    limit: int,
) -> dict[str, Any]:
    """把 CLI 参数组装成契约第 3 节的查询对象（**只允许那 6 个键**）。

    结构校验与键名规整都在 `gateway.normalize_payload` 里，MCP 侧走同一个函数。
    """
    if limit > config.DEFAULT_LIMIT:
        render.out().print(
            Text(f"提示：--limit {limit} 超过上限 {config.DEFAULT_LIMIT}，已按上限截断。", style="yellow")
        )
    return gateway.normalize_payload(
        metrics=_split_multi(metrics),
        dimensions=_split_multi(dimensions),
        filters=_parse_json_option("--filters", filters),
        grain=grain,
        order_by=_parse_order_by(order_by),
        limit=limit,
    )


def _load_contracts(metrics_dir: Path) -> dict[str, Any]:
    """加载契约；空目录返回空映射（CLI 不崩，交由各命令自行判断）。"""
    return gateway.load_contracts(metrics_dir)


def _safe_describe(contracts: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    """`describe_metric` 的容错版本（演示路径不该因为展示字段缺失而失败）。"""
    try:
        return gateway.describe(contracts, metric_id)
    except Exception as exc:  # pragma: no cover - 只在契约展示层降级
        if gateway.is_project_error(exc):
            return {"metric_id": metric_id, "enforcement_error": gateway.rejection_reason(exc)}
        raise


def _correct_request(
    contracts: Mapping[str, Any], metric_id: str, *, detail: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """按契约的维度/过滤器白名单拼一份**合法**的查询对象（对错比照里的"正确答案"）。"""
    info = detail if detail is not None else _safe_describe(contracts, metric_id)
    dimensions = [str(item) for item in info.get("allowed_dimensions") or []]
    filters = [str(item) for item in info.get("allowed_filters") or []]
    request: dict[str, Any] = {"metrics": [metric_id]}
    chosen = next((item for item in ("date.weekday", "date.month") if item in dimensions), None)
    chosen = chosen or (dimensions[0] if dimensions else None)
    if chosen:
        request["dimensions"] = [chosen]
    # 锚定**合成数据的结束日**，不用 `date.last_n_days`：后者以"今天"为锚
    # （`compiler._today()`），而数据固定止于 `SYNTH_END_DATE`，
    # 用它会让演示随运行日期漂移、并在数据结束后退化成空结果。
    end = date.fromisoformat(config.SYNTH_END_DATE)
    if "date.between" in filters:
        start = end - timedelta(days=_DEMO_WINDOW_DAYS - 1)
        request["filters"] = {"date.between": [start.isoformat(), end.isoformat()]}
    elif "date.last_n_days" in filters:
        request["filters"] = {"date.last_n_days": _DEMO_WINDOW_DAYS}
    grain = info.get("grain")
    if isinstance(grain, str) and grain:
        request["grain"] = grain
    request["limit"] = 50
    return request


# =========================================================================== 拒绝演示


def _reject_plan(
    contracts: Mapping[str, Any], metric_id: str, dimension: str | None = None
) -> dict[str, Any]:
    """构造一条**故意非法**的请求，并备好"对错比照"所需的上下文。

    两种演示形态：

    * `metric_id` 不在契约里 → 触发第 1 条校验 `registered`（`unknown_metric:`）；
    * `metric_id` 合法但维度越界 → 触发第 2 条校验 `dimensions`（`dimension_not_allowed:`）。
    """
    known = gateway.metric_ids(contracts)
    if metric_id not in set(known):
        suggestions = gateway.closest_names(metric_id, known, limit=3)
        allowed_hint = f"契约里已注册的指标，例如 {suggestions[0]}" if suggestions else "契约目录为空"
        plan: dict[str, Any] = {
            "mode": "unknown_metric",
            "request": {"metrics": [metric_id], "limit": 50},
            "comparison": [("metrics", metric_id, allowed_hint)],
            "suggestions": suggestions,
            "suggestion_title": "✅ 相近的合法指标",
            "correct": _correct_request(contracts, suggestions[0]) if suggestions else None,
        }
        return plan

    detail = _safe_describe(contracts, metric_id)
    allowed = [str(item) for item in detail.get("allowed_dimensions") or []]
    if dimension and dimension in allowed:
        render.out().print(
            Text(f"提示：{dimension} 在该指标白名单内，无法演示拒绝；自动换一个越界维度。", style="yellow")
        )
        dimension = None
    fallback = next(
        (item for item in _ILLEGAL_DIMENSION_CANDIDATES if item not in allowed),
        "subject.social_security_number",
    )
    illegal = dimension or fallback
    hints = gateway.closest_names(illegal, allowed, limit=3) if allowed else []
    return {
        "mode": "dimension_not_allowed",
        "metric_id": metric_id,
        "request": {"metrics": [metric_id], "dimensions": [illegal], "limit": 50},
        "comparison": [("dimensions", illegal, "、".join(allowed) or "该指标不允许任何维度")],
        "suggestions": hints,
        "suggestion_title": "✅ 该指标允许的相近维度",
        "correct": _correct_request(contracts, metric_id, detail=detail),
    }


def _run_rejection(
    contracts: Mapping[str, Any], metric_id: str, dimension: str | None = None
) -> tuple[dict[str, Any], str | None]:
    """真的把非法请求送进 semantic；返回 `(计划, 实际 reason)`。

    `reason is None` 表示契约**没有**拦下这条非法请求 —— 对项目而言是致命缺陷，
    调用方必须响亮地失败（红队探针）。
    """
    plan = _reject_plan(contracts, metric_id, dimension)
    try:
        gateway.query(dict(plan["request"]), contracts, role=config.Role.OWNER)
    except Exception as exc:
        if gateway.is_project_error(exc):
            return plan, gateway.rejection_reason(exc)
        raise
    return plan, None


def _render_rejection(plan: Mapping[str, Any], reason: str) -> None:
    """渲染拒绝现场：reason 面板 + 被拒请求 + 对错比照 + 正确写法。"""
    console = render.out()
    console.print(render.rejection_panel(
        reason,
        rule=reason.split(":", 1)[0],
        checks=config.ENFORCED_CHECKS,
    ))
    console.print(Text("被拒绝的查询对象（结构化，无原生语句通道）：", style="bold red"))
    rejected = json.dumps(dict(plan["request"]), ensure_ascii=False, indent=2)
    console.print(Text(rejected, style="red"))
    console.print(render.comparison_table(plan["comparison"]))
    render.print_suggestions(
        plan["suggestions"],
        correct_request=plan["correct"],
        title=str(plan.get("suggestion_title") or "✅ 相近的合法指标"),
    )
    console.print(Text("提示：这次拒绝也写进了审计（veriself audit 可查）。", style="dim"))


def _enforcement_bypass(reason: str) -> NoReturn:
    """契约没有拦下非法请求时的致命告警。"""
    render.out().print(render.rejection_panel(
        reason,
        rule="enforcement_bypass",
        checks=config.ENFORCED_CHECKS,
    ))
    _fail(reason, EXIT_UNEXPECTED)


# =========================================================================== demo 编排


def _demo_request(contracts: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """按契约白名单拼一条**合法**的演示查询（优先 `subject.sleep_debt_7d`）。"""
    known = gateway.metric_ids(contracts)
    metric_id = next((item for item in _PREFERRED_DEMO_METRICS if item in known), known[0])
    request = _correct_request(contracts, metric_id, detail=_safe_describe(contracts, metric_id))
    request["limit"] = _DEMO_WINDOW_DAYS
    return request, metric_id


# =========================================================================== Typer 应用


app = typer.Typer(
    name="veriself",
    help=(
        "veriself：给 AI 用的指标执行层。\n\n"
        "AI 只能提交结构化查询对象（metrics / dimensions / filters / grain / order_by / limit），"
        "没有 SQL 通道：veriself 替它做五条强制校验、RLS 改写与审计。\n\n"
        "退出码：0 成功 · 1 未预期错误 · 2 用法错误 · 3 契约拒绝 · 4 查询对象非法 · 5 环境/下游未就绪"
    ),
    no_args_is_help=True,
    add_completion=False,
)

metrics_app = typer.Typer(
    help="查看指标契约目录（18 个指标的展示入口）。",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(metrics_app, name="metrics")


# --------------------------------------------------------------------------- init


@app.command("init", help="建 schema → 加载契约 → 写维度表 → 物化指标（契约第 8 节）。")
def init_cmd(
    db_path: Annotated[Path, typer.Option("--db-path", help="DuckDB 路径")] = config.WAREHOUSE_PATH,
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
) -> None:
    """初始化数仓：`warehouse.loader.ensure_schema` + `upsert_dim_metric` + `materialize_all`。"""
    with _guard():
        contracts = _load_contracts(metrics_dir)
        connection = gateway.open_warehouse(db_path)
        try:
            schema_source = gateway.ensure_schema(connection, db_path=db_path)
            dim_contracts = gateway.upsert_dim_metrics(
                connection, gateway.load_definition_versions(metrics_dir)
            )
            materialized = gateway.materialize_all(connection, contracts)
        finally:
            connection.close()

    console = render.out()
    if not contracts:
        console.print(Panel(
            Text(f"没有加载到任何契约（{metrics_dir}）：请先完成 warehouse 的 metrics/*.yml。", style="yellow"),
            title="提示", title_align="left", border_style="yellow",
        ))
    if materialized is None:
        console.print(Panel(
            Text(
                "指标物化已跳过：未找到 veriself.materializer（契约第 8 节）。\n"
                "影响：指标物化结果为空，veriself query 暂时查不到数据；其余命令可用。\n"
                "处理：完成 materializer 后重跑 `veriself init` 即会补上物化。",
                style="yellow",
            ),
            title="⚠ 物化未执行", title_align="left", border_style="yellow",
        ))
        materialized_rows = 0
    else:
        materialized_rows = sum(materialized.values())
        if materialized:
            table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False)
            table.add_column("metric_id")
            table.add_column("写入行数", justify="right")
            for metric_id, rows in materialized.items():
                table.add_row(metric_id, render.format_cell(rows))
            table.caption = f"共 {len(materialized)} 个指标 / {materialized_rows} 行"
            table.caption_style = "dim"
            console.print(table)
        else:
            console.print(Text("物化没有写入任何指标（契约可能为空）。", style="yellow"))

    render.print_summary(
        [
            ("数仓", str(db_path)),
            ("建表函数", schema_source),
            ("契约目录", str(metrics_dir)),
            ("契约数量", str(len(contracts))),
            ("契约写入维度表", str(dim_contracts)),
            ("物化指标", str(len(materialized or {}))),
            ("物化行数", str(materialized_rows)),
        ],
        title="veriself init 完成",
        style="green",
    )


# --------------------------------------------------------------------------- synth


@app.command("synth", help="生成合成数据（3 年日粒度，固定种子，可复现）。")
def synth_cmd(
    db_path: Annotated[Path, typer.Option("--db-path", help="DuckDB 路径")] = config.WAREHOUSE_PATH,
    seed: Annotated[
        int | None, typer.Option("--seed", help=f"随机种子（默认 {config.SYNTH_SEED}）")
    ] = None,
) -> None:
    """调用 `veriself.synth.generate_all()` 写入合成数据。"""
    with _guard():
        connection = gateway.open_warehouse(db_path)
        try:
            schema_note = "已确保 schema"
            try:
                gateway.ensure_schema(connection, db_path=db_path)
            except gateway.GatewayUnavailable as exc:
                schema_note = f"建表步骤跳过（{exc.detail}）"
            result = gateway.generate_all(conn=connection, db_path=db_path, seed=seed)
        finally:
            connection.close()

    console = render.out()
    if isinstance(result, Mapping):
        table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False)
        table.add_column("对象")
        table.add_column("行数", justify="right")
        for key, value in result.items():
            table.add_row(str(key), render.format_cell(value))
        console.print(table)
    elif result is not None:
        shown = render.format_cell(result, max_chars=200)
        console.print(Text(f"generate_all() 返回：{shown}", style="green"))
    render.print_summary(
        [("数仓", str(db_path)), ("种子", str(seed if seed is not None else config.SYNTH_SEED)),
         ("日期范围", f"{config.SYNTH_START_DATE} → {config.SYNTH_END_DATE}"), ("建表", schema_note)],
        title="veriself synth 完成",
        style="green",
    )


# --------------------------------------------------------------------------- metrics


@metrics_app.command("list", help="列出全部指标契约摘要（18 个指标的目录）。")
def metrics_list_cmd(
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON（供程序消费）")] = False,
) -> None:
    """用 rich 表格展示 `list_metrics()` 的摘要；空目录也正常退出（不崩）。"""
    with _guard(json_mode=as_json):
        contracts = _load_contracts(metrics_dir)
        rows = gateway.list_metric_summaries(contracts)
        if as_json:
            render.print_json(
                {"metrics": rows, "count": len(rows), "metrics_dir": str(metrics_dir)}
            )
            return
        render.print_metrics_table(rows, metrics_dir=metrics_dir)


@metrics_app.command("show", help="展示单个指标的口径、血缘与 contract_hash。")
def metrics_show_cmd(
    metric_id: Annotated[str, typer.Argument(help="指标 ID，如 subject.sleep_debt_7d")],
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
    as_json: Annotated[
        bool, typer.Option("--json", help="输出完整契约 JSON（含 contract_hash 与血缘）")
    ] = False,
) -> None:
    """`describe_metric()` + rich 渲染；未知指标给近似建议并非 0 退出。"""
    with _guard(json_mode=as_json):
        contracts = _load_contracts(metrics_dir)
        if not contracts:
            _fail(f"contracts_empty: {metrics_dir} 下没有指标契约，请先完成 metrics/*.yml", EXIT_ENV)
        try:
            detail = gateway.describe(contracts, metric_id)
        except Exception as exc:
            reason = gateway.rejection_reason(exc) if gateway.is_project_error(exc) else ""
            if reason.startswith("unknown_metric:"):
                render.print_suggestions(
                    gateway.closest_names(metric_id, gateway.metric_ids(contracts), limit=5),
                    title="✅ 相近的合法指标",
                )
            raise
        if as_json:
            render.print_json(detail)
            return
        render.print_metric_detail(detail)


# --------------------------------------------------------------------------- query


@app.command("query", help="执行结构化查询对象（没有参数能传原生语句）。")
def query_cmd(
    metrics: Annotated[
        list[str] | None, typer.Option("--metrics", "-m", help="指标 ID，逗号分隔或重复传入")
    ] = None,
    dimensions: Annotated[
        list[str] | None, typer.Option("--dimensions", "-d", help="维度，逗号分隔或重复传入")
    ] = None,
    filters: Annotated[str | None, typer.Option("--filters", help=_FILTERS_HELP)] = None,
    grain: Annotated[
        str | None,
        typer.Option("--grain", help="时间粒度 day|week|month|quarter，默认取指标声明粒度"),
    ] = None,
    order_by: Annotated[str | None, typer.Option("--order-by", help=_ORDER_BY_HELP)] = None,
    role: Annotated[config.Role, typer.Option("--role", help="查询角色，决定 RLS 改写")] = config.Role.OWNER,
    limit: Annotated[int, typer.Option("--limit", help=_LIMIT_HELP)] = config.DEFAULT_LIMIT,
    db_path: Annotated[
        Path | None,
        typer.Option("--db-path", help="覆盖默认数仓路径（默认交给 semantic 自建连接）"),
    ] = None,
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
    as_json: Annotated[
        bool, typer.Option("--json", help="输出契约第 4 节的完整结构：data + audit")
    ] = False,
) -> None:
    """链路：`QueryRequest.from_json` → `compile_query`（五条校验）→ `execute_query`（+审计）。"""
    with _guard(json_mode=as_json):
        payload = _query_payload(
            metrics=metrics, dimensions=dimensions, filters=filters,
            order_by=order_by, grain=grain, limit=limit,
        )
        contracts = _load_contracts(metrics_dir)
        if not contracts:
            _fail(f"contracts_empty: {metrics_dir} 下没有指标契约，请先完成 metrics/*.yml", EXIT_ENV)
        connection = gateway.open_warehouse(db_path) if db_path is not None else None
        try:
            _compiled, result = gateway.query(payload, contracts, role=role, conn=connection)
        finally:
            if connection is not None:
                connection.close()
        normalized = gateway.result_payload(result)
    if as_json:
        render.print_json(normalized)
        return
    render.print_query_result(normalized, role=role)


# --------------------------------------------------------------------------- audit


@app.command("audit", help="展示最近的查询审计（只读审计表 ops.audit_log）。")
def audit_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="最多展示多少条")] = 20,
    db_path: Annotated[Path, typer.Option("--db-path", help="DuckDB 路径")] = config.WAREHOUSE_PATH,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON（供程序消费）")] = False,
) -> None:
    """审计表是契约允许的唯一例外：不经过编译链也必须可读（实现在 interfaces/auditlog.py）。"""
    with _guard(json_mode=as_json):
        rows = auditlog.fetch_recent(limit=limit, db_path=db_path)
        if as_json:
            render.print_json({"rows": rows, "count": len(rows), "db_path": str(db_path)})
            return
        render.print_audit_log(rows, db_path=db_path, limit=limit)


# --------------------------------------------------------------------------- reject


@app.command("reject", help="演示拒绝路径：故意提交非法请求，打印 reason 与相近指标建议。")
def reject_cmd(
    metric_id: Annotated[str, typer.Argument(help="故意写错或越权的 metric_id")],
    dimension: Annotated[
        str | None,
        typer.Option("--dimension", help="故意越界的维度（默认自动挑一个不在白名单里的）"),
    ] = None,
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
    exit_zero: Annotated[
        bool, typer.Option("--exit-zero", help="即使被拒也返回 0（默认返回非 0，与真实拒绝一致）")
    ] = False,
) -> None:
    """这是整个项目对外演示的核心：一次请求、一条 reason、一份对错比照。

    默认以**非 0** 退出码结束 —— 与 LLM 客户端真实拿到拒绝时的行为完全一致
    （客户端靠退出码 + `reason` 判断，而不是靠自然语言）。
    """
    with _guard():
        contracts = _load_contracts(metrics_dir)
        if not contracts:
            _fail(f"contracts_empty: {metrics_dir} 下没有指标契约，无法演示拒绝", EXIT_ENV)
        plan, reason = _run_rejection(contracts, metric_id, dimension)
        if reason is None:
            _enforcement_bypass(
                "enforcement_bypass: 非法查询对象通过了 compile_query（契约第 5 节强制校验未生效）"
            )
        _render_rejection(plan, reason)
    render.print_reason(reason)
    if not exit_zero:
        raise typer.Exit(EXIT_REJECTED)


# --------------------------------------------------------------------------- demo


@app.command("demo", help="一键演示：init（含物化）→ 正常查询 → 拒绝演示 → 录屏摘要。")
def demo_cmd(
    db_path: Annotated[Path, typer.Option("--db-path", help="DuckDB 路径")] = config.WAREHOUSE_PATH,
    metrics_dir: Annotated[Path, typer.Option("--metrics-dir", help="指标契约目录")] = config.METRICS_DIR,
    with_synth: Annotated[
        bool, typer.Option("--synth/--no-synth", help="演示前先生成合成数据（冷启动录屏用）")
    ] = False,
) -> None:
    """对外演示入口（README 首屏）：四步走完"建库 → 查询 → 拒绝 → 摘要"。"""
    total_steps = 4
    render.print_summary(
        [
            ("数仓", str(db_path)),
            ("契约目录", str(metrics_dir)),
            ("纪律", "AI 只能提交结构化查询对象；veriself 负责校验、RLS 改写与审计"),
        ],
        title="veriself 演示",
    )

    with _guard():
        if with_synth:
            render.print_step(0, total_steps, "合成数据（可选）")
            connection = gateway.open_warehouse(db_path)
            try:
                gateway.ensure_schema(connection, db_path=db_path)
                synth_result = gateway.generate_all(
                    conn=connection, db_path=db_path, seed=config.SYNTH_SEED
                )
            finally:
                connection.close()
            render.out().print(Text(
                f"合成数据完成：{render.format_cell(synth_result, max_chars=200)}", style="green"))

        # ---- 步骤 1：建库 + 契约 + 物化
        render.print_step(1, total_steps, "建库 · 加载契约 · 物化指标")
        contracts = _load_contracts(metrics_dir)
        if not contracts:
            _fail(
                f"contracts_empty: {metrics_dir} 下没有指标契约，请先完成 warehouse 的 metrics/*.yml",
                EXIT_ENV,
            )
        connection = gateway.open_warehouse(db_path)
        try:
            schema_source = gateway.ensure_schema(connection, db_path=db_path)
            dim_contracts = gateway.upsert_dim_metrics(
                connection, gateway.load_definition_versions(metrics_dir)
            )
            materialized = gateway.materialize_all(connection, contracts)
        finally:
            connection.close()
        if materialized is None:
            render.out().print(Panel(
                Text(
                    "请先完成 materializer（契约第 8 节）：veriself.materializer 尚不存在，\n"
                    "物化结果会是空的，veriself demo 无法继续。\n"
                    "完成后再运行 `veriself demo`，其余命令（metrics / query / reject / audit）不受影响。",
                    style="yellow",
                ),
                title="⚠ 物化不可用", title_align="left", border_style="yellow",
            ))
            _fail(
                "materializer_unavailable: veriself.materializer 未就绪，"
                "请先完成 materializer",
                EXIT_ENV,
            )
        materialized_rows = sum(materialized.values())
        render.print_summary(
            [
                ("契约数量", str(len(contracts))),
                ("建表函数", schema_source),
                ("契约写入维度表", str(dim_contracts)),
                ("物化指标", str(len(materialized))),
                ("物化行数", str(materialized_rows)),
            ],
            title="步骤 1 完成",
            style="green",
        )

        # ---- 步骤 2：一条正常查询
        request, metric_id = _demo_request(contracts)
        render.print_step(2, total_steps, f"正常查询：{metric_id}（结构化对象）")
        render.out().print(Text("提交给 veriself 的查询对象：", style="bold"))
        render.print_json(request)
        connection = gateway.open_warehouse(db_path)
        try:
            _compiled, result = gateway.query(
                request, contracts, role=config.Role.OWNER, conn=connection
            )
        finally:
            connection.close()
        payload = gateway.result_payload(result)
        render.print_query_result(payload, role=config.Role.OWNER)

        # ---- 步骤 3：拒绝演示
        render.print_step(3, total_steps, "拒绝演示：越界请求必须失败（这才是卖点）")
        plan, reason = _run_rejection(contracts, metric_id, None)
        if reason is None:
            _enforcement_bypass(
                "enforcement_bypass: 非法查询对象通过了 compile_query（契约第 5 节强制校验未生效）"
            )
        _render_rejection(plan, reason)

        # ---- 步骤 4：录屏摘要
        audit_rows: list[dict[str, Any]] = []
        try:
            audit_rows = auditlog.fetch_recent(5, db_path)
        except auditlog.AuditLogUnavailable:
            pass
        render.print_step(4, total_steps, "摘要")
        render.print_summary(
            [
                ("契约指标", f"{len(contracts)} 个"),
                ("物化", f"{len(materialized)} 个指标 / {materialized_rows} 行"),
                ("正常查询", f"{metric_id} → {len(payload.get('data') or [])} 行（含审计头）"),
                ("越界请求", f"被拒绝：{reason}"),
                ("审计", f"最近 {len(audit_rows)} 条（veriself audit --limit 5）"),
                ("结论", "AI 全程只说结构化对象；越界请求在进数仓之前就被拒绝，且每次都留痕。"),
            ],
            title="演示摘要（可录屏）",
        )
        render.out().print(Text(
            "复现完整流程：veriself synth → veriself demo；单步：veriself init / veriself metrics list / "
            "veriself query / veriself reject / veriself audit",
            style="dim",
        ))


# --------------------------------------------------------------------------- mcp


@app.command("mcp", help="以 stdio 启动 MCP server（4 个工具，供 LLM 客户端接入）。")
def mcp_cmd() -> None:
    """入口与 `python -m veriself.interfaces.mcp_server` 等价。"""
    try:
        from veriself.interfaces import mcp_server
    except ImportError as exc:  # pragma: no cover - 依赖已 vendored
        _fail(f"downstream_unavailable: mcp ({exc})", EXIT_ENV)

    with _guard():
        mcp_server.main()


if __name__ == "__main__":  # pragma: no cover - 便于 python -m 直接跑
    app()

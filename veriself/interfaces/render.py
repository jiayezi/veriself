"""`veriself` 的 rich 渲染层：表格、面板、拒绝对错比照。

约定：

* 所有 Console 都用 `markup=False`（查询结果与编译后的语句里可能含 `[`，不能让 rich
  误当标记解析），颜色一律通过 `Text(..., style=...)` 给出；
* Console 每次现建，以便 pytest 的 `CliRunner` 替换 `sys.stdout` / `sys.stderr` 后仍能捕获；
* 非终端（管道、LLM 客户端、测试）固定 120 列，保证表格里的 metric_id 不被折行拆断。
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from veriself import config

__all__ = [
    "audit_panel",
    "comparison_table",
    "data_table",
    "err",
    "format_cell",
    "metrics_table",
    "out",
    "print_audit_log",
    "print_json",
    "print_metric_detail",
    "print_metrics_table",
    "print_query_result",
    "print_reason",
    "print_step",
    "print_suggestions",
    "print_summary",
    "rejection_panel",
    "wrap_cells",
]

#: 审计头里"编译后的语句"字段候选名（契约第 4 节为 compiled_sql，这里容忍改名）。
_STATEMENT_KEYS = ("compiled_sql", "compiled", "compiled_statement", "statement")

#: 审计头里需要中文标签的字段（其余字段按 key 原样展示，容忍 semantic 的字段漂移）。
_AUDIT_LABELS: dict[str, str] = {
    "metric_versions": "指标版本",
    "contract_hashes": "契约哈希",
    "rls_applied": "RLS 改写",
    "enforced_checks": "强制校验",
    "as_of_definition": "as-of 口径",
    "queried_at": "查询时间",
}

_NON_TTY_WIDTH = 120
_MAX_TEXT = 48

#: 审计表按列限宽：时间戳截到秒，JSON 编码的列表压成一行，保证整表不超过 120 列。
_AUDIT_COLUMN_LIMITS: dict[str, int] = {
    "queried_at": 19,
    "rls_applied": 26,
    "checks_passed": 30,
    "request_json": 40,
}


def _console(file: Any) -> Console:
    """构造 Console：终端用自适应宽度，非终端固定 120 列（保证输出稳定可断言）。"""
    try:
        interactive = bool(file.isatty())
    except Exception:  # noqa: BLE001  # pragma: no cover - 某些被替换的流没有 isatty
        interactive = False
    width = None
    if not interactive:
        width = _NON_TTY_WIDTH
        try:
            width = max(width, shutil.get_terminal_size().columns)
        except Exception:  # noqa: BLE001, S110  # pragma: no cover - 取不到终端宽度就用固定宽度
            pass
    return Console(file=file, width=width, markup=False, highlight=False, emoji=False)


def out() -> Console:
    """stdout 上的 Console。"""
    return _console(sys.stdout)


def err() -> Console:
    """stderr 上的 Console。"""
    return _console(sys.stderr)


# --------------------------------------------------------------------------- 基础工具


def _compact_json(text: str) -> str | None:
    """把 `["a","b"]` / `{"k":"v"}` 这类**字符串形式的 JSON**压成一行可读文本。

    审计表里的 `rls_applied` / `checks_passed` 是 JSON 编码的列表，直接展示会又长又难读。
    """
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return ", ".join(format_cell(item, max_chars=24) for item in parsed) or "[]"
    if isinstance(parsed, Mapping):
        pairs = (f"{key}={format_cell(value, max_chars=24)}" for key, value in parsed.items())
        return "; ".join(pairs) or "{}"
    return None


def format_cell(value: Any, *, max_chars: int = _MAX_TEXT) -> str:
    """把任意单元格值格式化成单行短文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):  # NaN / Inf
            return str(value)
        text = f"{value:,.4f}".rstrip("0").rstrip(".") if abs(value) < 1e6 else f"{value:,.0f}"
    elif isinstance(value, int):
        text = f"{value:,}"
    elif isinstance(value, Mapping):
        text = json.dumps({str(k): v for k, v in value.items()}, ensure_ascii=False, default=str)
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(format_cell(item, max_chars=24) for item in value)
    else:
        text = _compact_json(str(value)) or str(value)
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _kv_lines(mapping: Mapping[str, Any]) -> str:
    """把映射渲染成 "k = v" 多行文本（空映射给占位符）。"""
    if not mapping:
        return "（无）"
    lines = (f"{key} = {format_cell(value, max_chars=64)}" for key, value in mapping.items())
    return "\n".join(lines)


def _bullet_lines(values: Sequence[Any]) -> str:
    """把序列渲染成 "- x" 多行文本（空序列给占位符）。"""
    items = [item for item in values if item not in (None, "", [], {})]
    if not items:
        return "（无）"
    return "\n".join(f"• {format_cell(item, max_chars=96)}" for item in items)


def wrap_cells(text: str, limit: int = 84) -> list[str]:
    """按**显示宽度**折行（CJK 记 2 列），供面板里的长句子使用。

    rich 自己折行时遇到超长 CJK/拉丁混排串会退化到按字符折行，续行会顶到面板边框外；
    这里先手工按列宽折好，每一行都是独立的 renderable，面板缩进就始终正确。
    """
    lines: list[str] = []
    current = ""
    used = 0
    for char in str(text):
        size = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > limit and current:
            lines.append(current)
            current, used = "", 0
        current += char
        used += size
    if current:
        lines.append(current)
    return lines or [""]


def _panel(title: str, body: RenderableType, *, style: str = "cyan") -> Panel:
    return Panel(body, title=title, title_align="left", border_style=style, padding=(0, 1))


# --------------------------------------------------------------------------- 指标目录


def metrics_table(rows: Sequence[Mapping[str, Any]], *, metrics_dir: Any = None) -> RenderableType:
    """指标目录表格（契约第 7 节 `list_metrics` 的字段）。"""
    if not rows:
        return Panel(
            Text(f"契约目录为空：{metrics_dir}\n先运行 `veriself init`，或在 metrics/*.yml 写好指标契约。", style="yellow"),
            title="指标目录",
            title_align="left",
            border_style="yellow",
        )
    columns = ("metric_id", "display_name", "unit", "direction", "grain", "status", "version")
    extra = [str(key) for row in rows for key in row if str(key) not in columns]
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False, expand=False)
    for name in columns:
        table.add_column(name, overflow="fold")
    for name in dict.fromkeys(extra):  # 容忍 semantic 返回额外字段
        table.add_column(name, overflow="fold")
    for row in rows:
        cells = [format_cell(row.get(name)) for name in columns]
        cells += [format_cell(row.get(name)) for name in dict.fromkeys(extra)]
        table.add_row(*cells)
    table.caption = f"共 {len(rows)} 个指标" + (f"（{metrics_dir}）" if metrics_dir else "")
    table.caption_style = "dim"
    return table


def print_metrics_table(rows: Sequence[Mapping[str, Any]], *, metrics_dir: Any = None) -> None:
    """打印指标目录。"""
    out().print(metrics_table(rows, metrics_dir=metrics_dir))
    if rows:
        out().print(Text("用法：veriself metrics show <metric_id> 查看口径与血缘", style="dim"))


def print_metric_detail(detail: Mapping[str, Any]) -> None:
    """打印单个指标的完整契约：口径、血缘、contract_hash、可用维度与过滤器。"""
    metric_id = str(detail.get("metric_id") or detail.get("id") or "（未知指标）")
    display_name = format_cell(detail.get("display_name"), max_chars=40)
    status = format_cell(detail.get("status"))
    header = Text()
    header.append(metric_id, style="bold white")
    if display_name:
        header.append(f"  {display_name}", style="bold cyan")
    if status:
        header.append(f"  [{status}]", style="dim")

    basics = (
        "unit", "direction", "grain", "agg", "entity", "owner",
        "version", "status", "contract_hash", "rls_policy",
    )
    basic_table = Table.grid(padding=(0, 2))
    basic_table.add_column(style="bold")
    basic_table.add_column()
    for name in basics:
        if name in detail:
            basic_table.add_row(name, format_cell(detail.get(name), max_chars=72))
    rls_policy = detail.get("rls_policy")
    if isinstance(rls_policy, str):
        try:
            roles = sorted(role.value for role in config.rls_visible_roles(rls_policy))
            basic_table.add_row("可访问角色", ", ".join(roles))
        except ValueError:
            basic_table.add_row("可访问角色", f"（未知 rls_policy：{rls_policy}）")

    blocks: list[RenderableType] = [header, _panel("基本信息", basic_table)]

    definition = detail.get("definition")
    formula = detail.get("formula_sql")
    if definition or formula:
        body = Group(
            Text(str(definition or "（无 definition）")),
            Text(""),
            Syntax(str(formula or "（无 formula_sql）"), "sql", word_wrap=True, theme="ansi_dark"),
        )
        blocks.append(_panel("口径与公式", body))

    lineage = detail.get("lineage")
    lineage = dict(lineage) if isinstance(lineage, Mapping) else {}
    lineage_table = Table.grid(padding=(0, 2))
    lineage_table.add_column(style="bold")
    lineage_table.add_column()
    lineage_table.add_row("数据源 sources", _bullet_lines(list(lineage.get("sources") or [])))
    upstream = list(lineage.get("upstream_metrics") or [])
    lineage_table.add_row("上游指标 upstream", _bullet_lines(upstream))
    blocks.append(_panel("血缘 lineage", lineage_table))

    dims = list(detail.get("allowed_dimensions") or [])
    filters = list(detail.get("allowed_filters") or [])
    use_table = Table.grid(padding=(0, 2))
    use_table.add_column(style="bold")
    use_table.add_column()
    use_table.add_row("可用维度", _bullet_lines(dims))
    use_table.add_row("可用过滤器", _bullet_lines(filters))
    blocks.append(_panel("查询能力（白名单，越界即拒绝）", use_table))

    rest = {
        key: value
        for key, value in detail.items()
        if key not in set(basics) | {"metric_id", "id", "display_name", "definition", "formula_sql",
                                     "lineage", "allowed_dimensions", "allowed_filters", "synonyms"}
    }
    if rest:
        rest_table = Table.grid(padding=(0, 2))
        rest_table.add_column(style="bold")
        rest_table.add_column()
        for key, value in rest.items():
            rest_table.add_row(str(key), format_cell(value, max_chars=96))
        blocks.append(_panel("其他字段", rest_table))

    out().print(Group(*blocks))


# --------------------------------------------------------------------------- 查询结果


def data_table(rows: Sequence[Mapping[str, Any]], *, max_rows: int = 25) -> RenderableType:
    """查询结果表格（列顺序取首行的键顺序，额外列自动补在后面）。"""
    if not rows:
        return Panel(
            Text("查询成功，但没有返回任何行。若还没生成合成数据，先运行 `veriself synth`。", style="yellow"),
            title="查询结果：0 行",
            title_align="left",
            border_style="yellow",
        )
    columns: list[str] = []
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False, expand=False)
    for name in columns:
        table.add_column(name, overflow="fold")
    for row in rows[:max_rows]:
        table.add_row(*[format_cell(row.get(name)) for name in columns])
    if len(rows) > max_rows:
        table.caption = f"共 {len(rows)} 行，仅显示前 {max_rows} 行（--json 可拿全量）"
        table.caption_style = "dim"
    return table


def audit_panel(audit: Mapping[str, Any], *, role: config.Role | None = None) -> Panel:
    """审计头面板（契约第 4 节）：版本、哈希、RLS、强制校验、as-of、编译语句。"""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    if role is not None:
        grid.add_row("查询角色", Text(role.value))
    for key, label in _AUDIT_LABELS.items():
        if key in audit:
            value = audit[key]
            if isinstance(value, Mapping):
                grid.add_row(label, Text(_kv_lines(value)))
            elif isinstance(value, (list, tuple, set)):
                grid.add_row(label, Text(" → ".join(format_cell(item) for item in value)))
            else:
                grid.add_row(label, Text(format_cell(value, max_chars=96)))
    for key, value in audit.items():
        if key in _AUDIT_LABELS or key in _STATEMENT_KEYS:
            continue
        grid.add_row(str(key), Text(format_cell(value, max_chars=96)))

    body: list[RenderableType] = [grid]
    statement = next(
        (audit.get(key) for key in _STATEMENT_KEYS if isinstance(audit.get(key), str)), None
    )
    if statement:
        body.append(Text(""))
        body.append(_panel(
            "编译后的语句（由 semantic 生成，interfaces 层从不拼装）",
            Syntax(statement, "sql", word_wrap=True, theme="ansi_dark"),
            style="dim",
        ))
    return _panel("审计头 audit（契约第 4 节）", Group(*body), style="green")


def print_query_result(payload: Mapping[str, Any], *, role: config.Role | None = None) -> None:
    """打印一次查询：结果表格 + 审计头面板。"""
    data = list(payload.get("data") or [])
    out().print(data_table(data))
    audit = payload.get("audit") or {}
    if isinstance(audit, Mapping) and audit:
        out().print(audit_panel(audit, role=role))
    else:
        out().print(Panel(
            Text("审计头缺失：契约第 4 节要求每次响应都携带 audit。", style="yellow"),
            title="审计头 audit",
            title_align="left",
            border_style="yellow",
        ))


# --------------------------------------------------------------------------- 审计日志


def print_audit_log(
    rows: Sequence[Mapping[str, Any]], *, db_path: Any = None, limit: int = 20
) -> None:
    """打印最近的查询审计（`fact_audit_log`）。"""
    if not rows:
        out().print(Panel(
            Text(f"审计日志为空（{db_path}）：还没有成功或失败的查询记录。", style="yellow"),
            title="查询审计",
            title_align="left",
            border_style="yellow",
        ))
        return
    preferred = ("audit_id", "queried_at", "actor_role", "outcome", "rls_applied", "checks_passed")
    columns = [name for name in preferred if any(name in row for row in rows)]
    if not columns:  # 列名完全不符合契约时的兜底
        columns = [str(key) for row in rows for key in row]
    skipped = sorted({str(key) for row in rows for key in row} - set(columns))
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False, expand=False)
    for name in columns:
        table.add_column(name, overflow="fold")
    for row in rows:
        cells: list[RenderableType] = []
        for name in columns:
            text = format_cell(row.get(name), max_chars=_AUDIT_COLUMN_LIMITS.get(name, _MAX_TEXT))
            if name == "outcome":
                style = "green" if text.startswith("ok") else "bold red"
                cells.append(Text(text, style=style))
            else:
                cells.append(Text(text))
        table.add_row(*cells)
    extra = f"（另有 {len(skipped)} 个字段，用 --json 看全量）" if skipped else ""
    table.caption = f"最近 {len(rows)} 条（--limit 可调整，最多 {limit}）· {db_path} {extra}"
    table.caption_style = "dim"
    out().print(table)


# --------------------------------------------------------------------------- 拒绝与建议


def rejection_panel(reason: str, *, rule: str | None = None, checks: Sequence[str] = ()) -> Panel:
    """拒绝面板：明确给出 `reason`（LLM 客户端据此判断被拒）。"""
    body: list[RenderableType] = [Text("请求已被指标契约拒绝。", style="bold red"), Text("")]
    for index, line in enumerate(wrap_cells(reason, 84)):
        label = "reason: " if index == 0 else " " * len("reason: ")
        body.append(Text(label, style="bold") + Text(line, style="bold red"))
    if rule:
        body.append(Text("触发的校验: ", style="bold") + Text(str(rule), style="red"))
    if checks:
        chain = " → ".join(str(c) for c in checks)
        body.append(Text("强制校验链: ", style="bold") + Text(chain, style="dim"))
    return _panel("❌ 拒绝", Group(*body), style="red")


def comparison_table(rows: Sequence[tuple[str, str, str]]) -> RenderableType:
    """对错比照表：字段 / 你提交的 / 契约允许的。"""
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", pad_edge=False, expand=False)
    table.add_column("字段")
    table.add_column("你提交的", style="red")
    table.add_column("契约允许的", style="green")
    for field, submitted, allowed in rows:
        table.add_row(str(field), str(submitted), str(allowed))
    return table


def print_suggestions(
    suggestions: Sequence[str],
    *,
    correct_request: Mapping[str, Any] | None = None,
    title: str = "✅ 相近的合法指标",
) -> None:
    """打印建议 + 一份"正确写法"的查询对象。"""
    lines = _bullet_lines(list(suggestions))
    body: list[RenderableType] = [Text(lines, style="green")]
    if correct_request is not None:
        body.append(Text(""))
        body.append(Text("正确写法（结构化查询对象，契约第 3 节）：", style="bold green"))
        rendered = json.dumps(dict(correct_request), ensure_ascii=False, indent=2)
        body.append(Text(rendered, style="green"))
    out().print(_panel(title, Group(*body), style="green"))


# --------------------------------------------------------------------------- 演示编排


def print_step(index: int, total: int, title: str, body: RenderableType | None = None) -> None:
    """演示步骤标题（`veriself demo` 用，录屏友好）。"""
    out().print()
    out().print(Text(f"── 步骤 {index}/{total} · {title} " + "─" * 8, style="bold magenta"))
    if body is not None:
        out().print(body)


def print_summary(
    items: Sequence[tuple[str, str]], *, title: str = "演示摘要", style: str = "magenta"
) -> None:
    """摘要面板（键值对）。"""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for key, value in items:
        table.add_row(str(key), Text(str(value)))
    out().print(_panel(title, table, style=style))


# --------------------------------------------------------------------------- 输出通道


def print_json(payload: Any) -> None:
    """按契约结构输出 JSON（stdout，无任何装饰，供 LLM 客户端直接解析）。"""
    out().print(json.dumps(payload, ensure_ascii=False, indent=2, default=str), soft_wrap=True)


def print_reason(reason: str, *, as_json: bool = False) -> None:
    """把失败原因打到 stderr：文本模式给 `reason: ...`，`--json` 模式再补一份 JSON 信封。"""
    err().print(Text("reason: ", style="bold red") + Text(str(reason), style="red"))
    if as_json:
        err().print(
            json.dumps({"ok": False, "reason": str(reason)}, ensure_ascii=False),
            soft_wrap=True,
        )

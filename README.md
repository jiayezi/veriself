# veriself

**面向 AI agent 的类型化指标执行层。**
LLM 不能写 SQL，只能组合已注册的指标——每个数字都带口径版本、粒度、血缘与审计头。

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/jiayezi/veriself/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://github.com/jiayezi/veriself/blob/main/pyproject.toml)
[![tests](https://img.shields.io/badge/tests-382%20passed-brightgreen.svg)](#现状)

---

## 30 秒演示

> **下面的输出是实跑的，不是手写的**：本机 `veriself synth && veriself demo` 原文，仅删除整行
> （删除处标 `…`）与行尾空格。面板偏宽是因为输出被重定向时宽度固定为 120 列
> （见 [`interfaces/render.py`](https://github.com/jiayezi/veriself/blob/main/veriself/interfaces/render.py) 的 `_NON_TTY_WIDTH`）。

真实用户在 MCP 客户端里说的是一句自然语言（*"我最近睡眠债有多严重？"*）。把它翻成下面这个
**结构化查询对象**是客户端的事——`veriself` 只接受这个对象，**没有任何参数能传原生语句**。
所以"LLM 写出错误 SQL"这个失败模式在这里根本不存在。

下面这条命令的前提是先跑过 `veriself synth`（生成 3 年确定性合成数据，输出见折叠区）：

```console
$ veriself demo
── 步骤 1/4 · 建库 · 加载契约 · 物化指标 ────────
┌─ 步骤 1 完成 ────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 契约数量        18                                                                                                   │
│ 建表函数        veriself.warehouse.loader.ensure_schema                                                              │
│ 契约写入维度表  20                                                                                                   │
│ 物化指标        18                                                                                                   │
│ 物化行数        14408                                                                                                │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

── 步骤 2/4 · 正常查询：subject.sleep_debt_7d（结构化对象） ────────
提交给 veriself 的查询对象：
{
  "metrics": [
    "subject.sleep_debt_7d"
  ],
  "dimensions": [
    "date.weekday"
  ],
  "filters": {
    "date.between": [
      "2026-09-01",
      "2026-09-30"
    ]
  },
  "grain": "day",
  "limit": 30
}

 date.day     date.weekday   subject.sleep_debt_7d
 ─────────────────────────────────────────────────
 2026-09-01   Tuesday        3.03
 2026-09-02   Wednesday      3.62
 2026-09-03   Thursday       3.28
 2026-09-04   Friday         3.43
 2026-09-05   Saturday       4.24
 2026-09-06   Sunday         4.67
…
 2026-09-25   Friday         5.44

    共 30 行，仅显示前 25 行（--json 可拿全量）
┌─ 审计头 audit（契约第 4 节） ────────────────────────────────────────────────────────────────────────────────────────┐
│ 查询角色        owner                                                                                                │
│ 指标版本        subject.sleep_debt_7d = 2                                                                            │
│ 契约哈希        subject.sleep_debt_7d = sha256:c046cbeddfe83d66                                                      │
│ RLS 改写        owner_only                                                                                           │
│ 强制校验        registered → dimensions → grain → ast_join_path → rls                                                │
│ as-of 口径      2026-10-01                                                                                           │
│ 查询时间        2026-10-09T10:48:06+08:00                                                                            │
│ row_versioning  valid_to IS NULL AND metric_version = contract.version                                               │
│ actor_role      owner                                                                                                │
…

── 步骤 3/4 · 拒绝演示：越界请求必须失败（这才是卖点） ────────
┌─ ❌ 拒绝 ────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 请求已被指标契约拒绝。                                                                                               │
│                                                                                                                      │
│ reason: dimension_not_allowed: 维度 'date.hour' 不在指标 'subject.sleep_debt_7d' 的 allowed_                         │
│         dimensions ['date.weekday', 'date.day_of_week', 'date.month', 'date.quarter', 'conte                         │
│         xt.is_travel', 'context.is_illness', 'context.location_type'] 中                                             │
│ 触发的校验: dimension_not_allowed                                                                                    │
│ 强制校验链: registered → dimensions → grain → ast_join_path → rls                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
…
提示：这次拒绝也写进了审计（veriself audit 可查）。
```

**这个 demo 的重点不是"它答对了"，而是"它无法答错"**：第二个请求里 LLM 想越界，架构不允许它越界——拒绝发生在**生成 SQL 之前**（拒绝原因是 `dimension_not_allowed`，指向契约里的白名单），并且这次拒绝同样写进审计。

<details>
<summary>其余命令的完整输出（实跑原文，未删改）</summary>

### `veriself synth`

```console
 对象                  行数
 ──────────────────────────
 dim_date             1,004
 dim_subject              2
 dim_source               4
 fact_subject_day     1,004
 fact_observation   106,424
 fact_event           5,882
 latent_daily         1,004
 obs_daily           10,040
 obs_intraday       144,576

┌─ veriself synth 完成 ────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 数仓      <仓库根>                                                                                                   │
│ 种子      20261001                                                                                                   │
│ 日期范围  2024-01-01 → 2026-09-30                                                                                    │
│ 建表      已确保 schema                                                                                              │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### `veriself query --json`：契约第 4 节的 `data` + `audit`

命令：`veriself query -m subject.sleep_debt_7d --filters '{"date.between": ["2026-09-28", "2026-09-30"]}' --role owner --json`

```console
{
  "data": [
    {
      "date.day": "2026-09-28",
      "subject.sleep_debt_7d": 6.970000000000001
    },
    {
      "date.day": "2026-09-29",
      "subject.sleep_debt_7d": 7.15
    },
    {
      "date.day": "2026-09-30",
      "subject.sleep_debt_7d": 6.720000000000001
    }
  ],
  "audit": {
    "metric_versions": {
      "subject.sleep_debt_7d": 2
    },
    "contract_hashes": {
      "subject.sleep_debt_7d": "sha256:c046cbeddfe83d66"
    },
    "compiled_sql": "SELECT \"d\".\"date\" AS \"date.day\", ANY_VALUE(CASE WHEN \"f\".\"metric_id\" = ? THEN \"f\".\"value\" END) AS \"subject.sleep_debt_7d\" FROM \"fact_metric_value\" AS f INNER JOIN \"dim_date\" AS d ON \"d\".\"date_key\" = \"f\".\"date_key\" WHERE \"f\".\"valid_to\" IS NULL AND (\"f\".\"metric_id\" = ? AND \"f\".\"metric_version\" = ?) AND \"f\".\"subject_id\" = ? AND \"d\".\"date\" >= CAST(? AS DATE) AND \"d\".\"date\" <= CAST(? AS DATE) GROUP BY 1 ORDER BY \"date.day\" ASC LIMIT ?",
    "rls_applied": [
      "owner_only"
    ],
    "enforced_checks": [
      "registered",
      "dimensions",
      "grain",
      "ast_join_path",
      "rls"
    ],
    "as_of_definition": "2026-10-01",
    "queried_at": "2026-10-09T10:48:55+08:00",
    "row_versioning": "valid_to IS NULL AND metric_version = contract.version",
    "actor_role": "owner"
  }
}
```

</details>

---

## 为什么存在

每个 text-to-SQL 工具都在优化同一个问题：*SQL 跑通了吗？*
但近期的研究表明，在**可执行**的查询里，仍有
[73%–99% 与提问者本意存在静默的语义分歧](https://arxiv.org/abs/2608.23569)，
而且[只对齐指标口径还不够——访问策略也必须被强制执行](https://arxiv.org/abs/2608.26157)。

我们优化的是另一件事：**让"返回一个无人能解释的数字"成为不可能。**
口径与权限由代码强制，而不是在 prompt 里请求。

---

## 这不是什么

- **不是 text-to-SQL 工具。** LLM 在这里从不写 SQL，只能组合已注册的指标——
  因此不存在"幻觉出一个数字"的攻击面。
- **不是语义格式。** 口径是 YAML，并将保持与 Open Semantic Interchange / Apache Ossie 兼容。我们不在格式上竞争。
- **不是 BI 仪表盘。** 不提供拖拽式看板，图表是下游的事。
- **不是记忆框架。** 不做检索或 embedding。我们做的是记忆框架跳过的那部分：版本、粒度、as-of、血缘、审计。
- **还不是领域无关的。** 执行引擎（contract → compile → enforce → audit）本身与领域无关，
  但**物理模型映射目前硬接在"个人/纵向数据"这个形态上**（日粒度、`(date_key, subject_id)` 骨架、
  声明的 JOIN 路径仍是模块级常量）。泛化成 `domains/*.yml` 描述符是
  **[路线图 v0.2 第 5 项](https://github.com/jiayezi/veriself/blob/main/docs/03-路线图.md)**——在此之前，换一个领域意味着改引擎代码。
- **无遥测、无云、无账号。** 一个文件，local-first。

---

## 商业版缺失的那一半

治理能力在这些项目里都被放进了商业版：

| 项目 | 被管控的部分在哪 |
| --- | --- |
| [Cube Core](https://github.com/cube-js/cube) | RBAC 与多租户 → 商业版 |
| [WrenAI](https://github.com/Canner/WrenAI) | 行/列级安全 → 商业版 |
| [dbt Semantic Layer](https://github.com/dbt-labs/metricflow/discussions/734) | 指标*查询* → dbt Cloud Team/Enterprise |
| [DataHub](https://docs.datahub.com/docs/features/feature-guides/metrics-and-semantic-models) | 指标值查询：**不支持** |

于是我们把缺的那一半开源做出来：**强制校验层。**

---

## 五条强制校验

每条查询按顺序通过以下五条；失败是拒绝，不是警告。

| # | 校验 | 失败时 |
| --- | --- | --- |
| 1 | `registered` — 指标存在且未弃用 | 拒绝：`unknown_metric:` / `deprecated_metric:` |
| 2 | `dimensions` — 请求的维度/过滤器在契约白名单内 | 拒绝：`dimension_not_allowed:` / `filter_not_allowed:` |
| 3 | `grain` — 日粒度指标不能以更细的粒度查询 | 拒绝：`grain_not_compatible:` |
| 4 | `ast_join_path` — SQL 用 [sqlglot](https://github.com/tobymao/sqlglot) 解析；只允许声明的表、声明的 JOIN 路径，禁止子查询/UNION/CTE 逃逸，禁止 `SELECT *` | 拒绝：`ast_violation:` |
| 5 | `rls` — 按角色注入行级策略（`owner_only` / `aggregate_min5` / `no_pii`） | **改写** SQL，并写入审计头 |

执行前另有基于 `EXPLAIN` 的扫描量预检。

---

## 架构

```
CLI (veriself)   MCP server   ← LLM 仅有的两个入口
     │            │
     └─────┬──────┘   只接受结构化查询对象，绝不接受 SQL 字符串
           ▼
    semantic/   契约 → 校验 → 编译（sqlglot）→ 强制校验 → 审计
           ▼
   warehouse/   DuckDB：dim_*（SCD2）→ fact_* → 物化指标值
           ▼
      synth/    确定性合成主体数据（含植入的潜在结构）
```

完整接口契约：[`docs/00-接口契约.md`](https://github.com/jiayezi/veriself/blob/main/docs/00-接口契约.md)
指标清单：[`docs/01-指标清单.md`](https://github.com/jiayezi/veriself/blob/main/docs/01-指标清单.md)

### 与业界术语的对应关系

如果你见过 dbt MetricFlow、Cube 或 LookML：这里没有为改而改地重命名概念——
这张表只是翻译器（字段名已冻结，见接口契约）：

| veriself | dbt MetricFlow / 业界 | 说明 |
| --- | --- | --- |
| `semantic_models/*.yml` | `semantic_models.yml` | 物理表之上的逻辑列：通道 → 日粒度列、事件表达式、SCD2 维度列 |
| `metrics/*.yml` | `metrics.yml` | 一个文件一个指标，带版本，每个指标有冻结的维度/过滤器白名单 |
| `formula_sql` | `type_params.expr`（派生指标） | 只有派生指标允许 SQL；这里被限制为日粒度标量表达式 |
| `bucket: {agg: ...}` | 指标的 `type_params.window` + `time_granularity` | 非日粒度的桶内聚合（声明式，不写 `OVER()` 样板） |
| `agg`（顶层） | measure `agg` | **跨粒度上卷**：请求粒度比指标粒度粗时如何聚合 |
| `direction` | `polarity` / `improves_when`（Avo） | `higher_better` / `lower_better` / `neutral` |
| `lineage.sources` / `upstream_metrics` | 语义模型的 measure + 指标依赖 | `formula_sql` 里仅有的两种引用形式 |
| `rls_policy` | data-mesh 策略 / 行级安全 | 治理写在**指标口径里**，不在旁挂目录里 |
| `contract_hash` + `metric_version` | dbt 节点唯一 ID + 工件版本 | 每个数字把口径版本与哈希带进审计头 |

---

## 快速开始

```bash
git clone <this repo> && cd veriself
uv sync --extra dev          # uv.lock 是依赖的唯一事实来源

veriself init          # 建 DuckDB schema、加载指标契约
veriself synth         # 生成 3 年确定性合成数据
veriself metrics list  # 查看 18 个已注册指标
veriself query --metrics subject.sleep_debt_7d --filters '{"date.last_n_days": 30}' --role owner
veriself reject subject.focus_skore    # 看拒绝原因与相近建议
veriself audit --limit 5               # 每次查询都留痕
```

不想激活环境时，把 `veriself` 换成 `uv run veriself`。

MCP（stdio）可用于任何 MCP 客户端：

```jsonc
// claude_desktop_config.json
{ "mcpServers": { "veriself": { "command": "veriself", "args": ["mcp"] } } }
```

---

## 关于列式存储

我本职工作里的其中一个数仓是 MySQL InnoDB，我反对过把它换成列式引擎。本项目把它的每一条约束都反过来了：

| | 本职工作 | 本项目 |
| --- | --- | --- |
| 查询形态 | 点查 | 跨多年、多维度聚合 |
| 表形态 | 窄 EAV | 宽星型模型 |
| 写入模式 | 删+插（毁掉历史） | 追加 + SCD2（历史就是功能） |
| 部署 | MySQL | 一个本地文件，秒级重建 |

**同样的问题，不同的约束，不同的答案**——这就是这里用 DuckDB 的原因。

---

## 现状

`v0.1.0` —— 强制校验层与合成数据集是必须做对的部分；Web UI 有意推迟。

**验证结果（本机实测）**：

| 项 | 结果 |
| --- | --- |
| 测试 | `uv run python -m pytest tests` → **382 passed** |
| 端到端红队验收 | `uv run python -m pytest tests/test_e2e_redteam.py` → **23 passed**（真实链路，非 mock） |
| 合成数据 | 1,004 天 · `fact_observation` 106,424 行 · `fact_event` 5,882 行 |
| 指标物化 | 18 个指标 · 14,408 行 · **重算幂等**（无变化时零写入，不累积历史） |
| 植入效应可检出 | 睡眠 ≤6h 的次日专注度 44.14 vs ≥7.5h 的 50.06（Welch t=−5.29, p=2.3e−06） |
| 内部自洽 | 独立重算"近 7 日睡眠债" vs 物化结果：最大绝对误差 **0.0** |
| 双时间轴 | 同一 `(metric, subject, date_key)` 在当前版本内多行有效 = **0**；值变化才留痕 |
| 周内排序 | `date.day_of_week` 按星期序（1→7）；`date.weekday` 是名字，按它排序是字母序 |

逐文件用例明细、改动后必须满足的验收条件、以及各模块的边界，见 [`AGENTS.md`](https://github.com/jiayezi/veriself/blob/main/AGENTS.md)。

**已知限制**：

- `*_7d` 滚动指标的 `agg=mean`：day→month 上卷得到的是"滚动值的均值"，不等于月度均值。逐日展示不受影响。
- `metrics/history/` 会写入 `dim_metric`（主键 `(metric_id, version)`）。查询和物化仍只使用当前目录里的口径。读某行指标的 `grain` 必须同时匹配 `version = metric_version`。
- **容器化尚未交付**：v0.1 没有 `Dockerfile` / `compose`（记在 `docs/03-路线图.md`）。
- **同一指标可同时存在多个有效版本**：物化只作用于声明的 `metric_version`，升级口径不会关闭旧版本的
  有效行。因此**读取必须带 `metric_version` 过滤**——`semantic` 已如此实现，
  但裸查 `fact_metric_value` 会同时读到多个版本。

## 许可证

Apache-2.0 — 见 [LICENSE](https://github.com/jiayezi/veriself/blob/main/LICENSE)。

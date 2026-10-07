# 贡献指南

## 这个项目最看重什么

一句话：**可追溯性优先于表达力**。

当"让用户问得更多"和"让每个数字都能被追溯"冲突时，这个项目永远选后者。提 PR 前请先确认你的改动不削弱任何一条强制校验。

## 开发环境

```bash
git clone <repo> && cd veriself
uv sync --extra dev
uv run python -m pytest tests -q
```

## 架构边界（改动前必读）

完整接口定义见 [`docs/00-接口契约.md`](00-接口契约.md)。核心铁律：

| 层 | 可以 | 不可以 |
| --- | --- | --- |
| `interfaces/`（CLI、MCP） | 调用 `semantic` 的公开 API、渲染输出 | **拼 SQL、直接查业务表**（唯一例外：`veriself audit` 读审计表） |
| `semantic/` | 加载契约、编译 SQL、执行强制校验 | 依赖 `synth` / `interfaces` |
| `warehouse/` | DDL、装载、改写数据结构 | 实现校验逻辑 |
| `synth/` | 生成合成数据 | 修改契约或校验规则 |

`veriself/config.py` 是各方共享的冻结常量，**不要修改**。

## 新增一个指标

1. 在 `metrics/` 加一个 YAML（字段见契约第 2 节），一个文件一个指标；
2. `formula_sql` 里引用的每个 `表.列` 必须出现在 `lineage.sources`；引用的每个上游指标必须出现在 `lineage.upstream_metrics`（用 `metric('...')` 形式）；
3. 明确 `agg`（跨粒度上卷方式）——**填错会算出错误的数**：睡眠债用 `sum`，专注度用 `mean`；
4. 在 [`docs/01-指标清单.md`](01-指标清单.md) 补一行；
5. 跑 `veriself init && veriself metrics show <your.metric>` 确认能加载。

## 新增一个强制校验

1. 先想清楚它拦住了什么真实攻击；
2. 在 `config.REASON_PREFIXES` 加固定前缀（拒绝原因必须可被程序匹配）；
3. 在 `tests/test_enforcement.py` 的 `redteam` 用例集合里加一条**能复现该攻击**的测试；
4. 更新 `README.md` 的 "五条强制校验" 表格。

## 提交规范

- 提交信息用祈使句，中文或英文均可；涉及口径变更的提交必须说明**口径差异**与**影响范围**。
- PR 描述里请包含：改了什么、为什么、怎么验证的（贴真实命令与输出）。
- 不要提交 `data/`、`vendor/`、`.duckdb` 文件。

## 报告问题

请附上：`veriself query` 的完整命令、返回的拒绝 `reason`（如果有）、以及 `veriself audit --limit 1` 的输出。审计头就是这个项目的"可复现最小用例"。

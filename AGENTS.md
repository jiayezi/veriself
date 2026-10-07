# veriself — Agent 约定

本仓库是一个**面向 AI agent 的类型化指标执行层**：LLM 不能写 SQL，只能组合已注册的指标；
每个数字都带口径版本、粒度、血缘与审计头。

> **一句话定位**：不要做「更准的 text-to-SQL」，要做「**不可能算错、且能证明自己没算错**」的指标执行层。
> 任何削弱强制校验的改动都不接受。

---

## 数据链路

```
契约 metrics/*.yml
  → load_contracts()  校验（引用/枚举/无环/列存在性）
  → materializer      按 lineage 拓扑分层求值 → fact_metric_value（双时间轴）
  → semantic          结构化查询对象 → 编译(sqlglot) → 五条强制校验 → RLS 改写 → 审计头
  → interfaces        CLI(veriself) / MCP server  ← AI 只能从这里进
```

| 对象 | 说明 |
| --- | --- |
| `metrics/*.yml` | 唯一指标口径来源；改它等于改系统行为（**无需改 Python**） |
| `semantic_models/*.yml` | **逻辑列定义**（通道→日粒度列、事件逻辑列、维度列 + 表别名）；materializer 与契约校验都从这里读 |
| `veriself/config.py` | 冻结常量（角色、枚举、校验链、拒绝前缀） |
| `veriself/contract_hash.py` | **唯一**哈希实现；其他模块必须 import，不得各自实现 |
| `veriself/warehouse/schema.sql` | **唯一** DDL 来源 |
| `data/` | 生成产物，gitignore；测试用 `data/` 下的自管目录，**不要清理别人的** |

---

## 铁律

1. **LLM 不写 SQL**：`interfaces` 层不得拼 SQL、不得直接 `import duckdb` 查业务表。
   唯一例外：`interfaces/auditlog.py` 只读 `fact_audit_log`，且用 DuckDB relation API（零查询关键字）。
   所有业务 SQL 只能由 `semantic.compile_query` 产出。
2. **五条校验的组成与规范顺序**（`config.ENFORCED_CHECKS`）：
   `registered → dimensions → grain → ast_join_path → rls`。
   拒绝一律抛 `config.EnforcementError(rule, detail)`，`detail` 前缀取 `config.REASON_PREFIXES`。
   ⚠️ **实际执行顺序在第 4/5 条上不同**：`rls` 先于 `ast_join_path`
   （先算 RLS 改写计划，再对最终 SQL 做 AST 校验）。
   审计头 `enforced_checks` 记录**实际执行过**的校验（按规范顺序输出），
   **不是**回显 `config.ENFORCED_CHECKS`——别把它改回常量，那会让审计栏变成空头支票。
3. **`config.py` 与 `contract_hash.py` 不得改**（前者是所有约定的事实来源，后者三方共用）。
4. **双时间轴不得退化为覆盖写**：写入前 `UPDATE ... SET valid_to = now()` 关闭旧行，
   **禁止物理 DELETE**（删了就永久失去 as-of 复现能力）。
5. **不许为了让测试/演示好看而放宽强制校验或隐私闸门**。
   宁可拒绝，不可静默改数：`limit` 超限是拒绝，不是截断。
6. **`formula_sql` 里写物理表名**（`fact_observation.sleep_hours`），
   窗口排序用**裸 `date_key`**，含 `OVER()` 的公式在物化时**不得再套一层聚合**（否则滚动和被二次平均）。
   公式是日粒度标量表达式，**不能写普通聚合**。
   非日粒度指标优先用声明式 **`bucket: {agg: ...}`**（桶内聚合由物化器自动完成），
   与手写 `OVER()` 窗口公式互斥（加载期拒绝）。
7. **指标数量与 RLS 分布已冻结**：18 个指标；`owner_only` 8 / `aggregate_min5` 6 / `no_pii` 4。
   增删指标必须同步 `docs/01-指标清单.md`。

---

## 模块地图与依赖方向

依赖只能单向：`interfaces → semantic → warehouse/materializer → config`；`synth` 只被 `interfaces` 与测试调用。

| 模块 | 职责 |
| --- | --- |
| `metrics/*.yml` | 指标契约（口径/血缘/白名单/RLS/上卷方式） |
| `semantic_models/*.yml` + `semantic_model.py` | **来源语义模型**：逻辑列定义与加载校验 |
| `veriself/config.py` | 常量与异常 |
| `contract_hash.py` | 契约哈希（审计头可信的基础） |
| `sqlrefs.py` | **唯一**公式解析/重写实现（sqlglot AST；materializer 与 semantic 共用） |
| `warehouse/schema.sql` + `loader.py` | DDL / 单版本维度写入 / 双时间轴原语 |
| `materializer/` | **契约公式求值**：`views`（语义模型→视图）+ `topo`（分层）+ `sqlbuild`（求值 SQL）+ `merge`（双时间轴合并） |
| `semantic/` | **契约加载（含列存在性校验）→ 编译 → 五条校验 → RLS → 审计** |
| `synth/` | 确定性合成数据（含**植入的潜在结构**） |
| `interfaces/` | `veriself` CLI + MCP server（4 工具） |

---

## 开发环境

**`uv.lock` 是依赖的唯一事实来源。用 `uv`，不要用 `pip`，不要手改 `.venv`。**

```bash
uv sync --extra dev          # 按 uv.lock 建/更新 .venv
uv run <命令>                 # 不激活环境直接跑（推荐）
```

Windows 终端另需 `$env:PYTHONIOENCODING='utf-8'`，否则中文输出在管道里乱码。

调用 `veriself` 的三种方式（任选）：

```bash
uv run veriself metrics list                  # 推荐：走 uv.lock
.venv/Scripts/veriself.exe metrics list       # Windows，已激活或绝对路径
.venv/bin/veriself metrics list               # macOS / Linux
```

MCP（stdio）入口：`veriself mcp`，等价 `python -m veriself.interfaces.mcp_server`。
要求 **`mcp>=2`**（1.x 的 `FastMCP` 在 2.0 已更名 `MCPServer`）。

### 静态检查

```bash
uv run ruff check .                             # 风格/规则（[tool.ruff] + per-file-ignores）
uv run --with pyright pyright .                 # 类型检查：与 VS Code 的 Pylance 同引擎，同档位
uv run --with pyright pyright . --level error   # 只看 error
uv run --with pyright pyright . --outputjson    # 机读；有 error 时退出码 1，可直接进 CI
```

Pylance 没有 CLI，但它的引擎 Pyright 有。仓库根的 `pyrightconfig.json` 负责把两者对齐：
**Pyright CLI 不读 VS Code 的 `settings.json`**，而 `pyrightconfig.json` 的优先级**高于**
`python.analysis.*`——改 `typeCheckingMode` 时两边要同步。`--with pyright` 是 uv 的临时叠加，
不会写进 `pyproject.toml` / `uv.lock`。

⚠️ CLI 与编辑器的诊断**不完全相同**，这不是配置错误：

- Pylance 默认 `python.analysis.diagnosticMode = openFilesOnly`，**只报已打开的文件**；
  Pyright CLI 永远扫 `include` 下的全部文件。想让编辑器也全量：改成 `"workspace"`（会明显变慢）。
- Pylance 自带常用库的 **bundled stubs**（`python.analysis.disableBundledStubs`），Pyright CLI 没有。
  `pandas` 不带 `py.typed`、也没装 `pandas-stubs`，于是 CLI 会多报
  `Cannot access attribute "dayofweek" for class "DatetimeIndex"` 这类编辑器不报的错。
  要抹平：`uv add --dev pandas-stubs`（`scipy` 同理，用 `types-*` / `*-stubs`）。

---

## 测试与验证

```bash
uv run python -m pytest tests                          # 全量
uv run python -m pytest tests/test_e2e_redteam.py      # 端到端红队（真实链路，非 mock）
uv run python -m pytest tests/test_enforcement.py -k redteam   # 五条校验的精选用例
uv run veriself demo                                        # 一条命令跑通全链路
```

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_e2e_redteam.py` | 端到端红队（**发布依据**）：合成→物化→编译→执行→审计 |
| `test_enforcement.py` | 五条校验的边界；`-k redteam` 精选；校验实际执行记录；`date.day_of_week` 排序 |
| `test_warehouse_contracts.py` | 契约字段集、RLS 分布、DDL 一致性、**数值黄金基准**、`date_key` 桶语义、SCD2 约束 |
| `test_interfaces.py` | CLI 退出码、MCP schema 封闭性、源码扫描（无 SQL 拼接） |
| `test_synth_fidelity.py` | 统计显著性（植入效应存在 / 未植入效应不存在）+ 事件唯一性 + **重跑幂等** |
| `test_materializer.py` | 数值语义（滚动窗口、NaN 过滤、幂等合并、通道隔离、声明式桶） |

**改动后必须做到**：全量测试全绿 + `veriself demo` 退出码 0。
数值类改动要更新 `test_warehouse_contracts.py` 里的黄金值断言——那是唯一能挡住数值漂移的防线。

**README 的 CLI 输出片段必须与实跑一致**：README「30 秒演示」与折叠区里贴的是本机实跑原文
（只删除整行、去除行尾空格，不改写）。改 CLI 输出后要重跑 `veriself synth && veriself demo` 并同步，
核对方法见 README 首屏的说明。

⚠️ **写断言前先问：它能不能失败？** 只断言"某字段 == 配置常量"往往是**同义反复**。
正确做法是断言**可观察的行为**（例如用 spy 证明五个校验真的被调用）。

**退出码约定**：`0` 成功 · `1` 未预期 · `2` 用法错 · `3` 契约拒绝 · `4` 查询对象非法 · `5` 环境/下游未就绪。

---

## 阅读顺序（**不要从 `main`/`cli.py` 开始**）

本项目是"**契约决定一切**"：口径、可用维度、权限、血缘、上卷方式全在 `metrics/*.yml` 里，
Python 只是执行者。

```
metrics/subject.sleep_debt_7d.yml                    # 一个完整契约
  → metrics/subject.sleep_need_deviation_daily.yml   # 派生指标：metric('...') 引用上游
  → metrics/subject.focus_score_weekly.yml           # 非日粒度：bucket 桶聚合
  → semantic_models/observation.yml                  # 逻辑列定义（通道 → 日粒度列）
  → veriself/config.py                      # 全部约定常量
  → veriself/contract_hash.py               # 解释审计头凭什么可信
  → veriself/warehouse/schema.sql           # 注意 fact_metric_value 的双时间轴
  → veriself/materializer/sqlbuild.py       # 指标怎么算（求值 SQL 构造）
  → veriself/materializer/merge.py          # 双时间轴合并（无变化检测）
  → veriself/semantic/contract.py           # 契约加载与校验
  → veriself/semantic/query.py              # 结构化查询对象（无 SQL 通道）
  → veriself/semantic/enforcement.py        # 五条强制校验（重点看 ast_join_path）
  → veriself/semantic/compiler.py           # 编译 SQL + 审计头
  → veriself/interfaces/                    # 最后才看
```

---

## 易错点（写代码前必读）

- **观测通道必须带 `FILTER (WHERE channel = ...)`**：`fact_observation` 是"通道-值"窄表，
  漏掉过滤会让每个通道列变成"当天全部通道的聚合"——**行数正常、数值全错**，极难发现。
  公式里写裸 `date_key`（不要写 `o.date_key`；来源日期列已改名为 `obs_date_key`/`evt_date_key`）。
- **`agg` 是"跨粒度上卷方式"**，不是同一粒度内的聚合：`sleep_debt_7d` 用 `sum`，
  `focus_score` 用 `mean`。填错会静默算出错误的数。桶内聚合用 `bucket.agg`，两者语义分离。
- **`dim_context` 与事实表没有连接键**：`dim_context.is_travel` 目前不可用，**不要**放进
  `allowed_dimensions` / `allowed_filters`。
- **单主体数据下 `partner`/`researcher` 查 `aggregate_min5` 指标恒为 0 行**——这是隐私闸门
  （`HAVING COUNT(DISTINCT subject_id) >= 5`）的正确行为，不是 bug。**不要为了演示好看而放宽它。**
- **测试各自管理独立目录**：用 `data/` 下的自管目录，不要依赖 `tmp_path`。
  谁创建的目录谁清理，**不要清理别人在 `data/` 下的活动目录**。
- **改写文件要用编辑工具，不要用 shell 重定向**（`Set-Content` / `>` / `Out-File` 会改编码）。
  临时批处理写成独立脚本由 Python 读写文件。
- **双时间轴合并用三步，不要用单条 `MERGE`**（DuckDB 的 `MERGE` 不能承载"值变化"语义）：
  落临时表 → 关变化键 → 插新键 → 关消失键，见 `materializer/merge.py::build_merge_sql`。
- **值比较必须带浮点容差**（`materializer.VALUE_REL_TOL`）：DOUBLE 求和顺序不稳定，
  精确相等会让每次重算都留噪声历史，把"重算幂等"打穿。
- **改 `schema.sql` 不会升级存量库**（DuckDB 不支持 `DROP CONSTRAINT` / `ADD CHECK` /
  部分索引，`CREATE TABLE IF NOT EXISTS` 遇旧表静默跳过）。升级方式 = **重建库**
  （`veriself synth && veriself init`）；需要就地升级时用独立语句（如 `CREATE UNIQUE INDEX IF NOT EXISTS`，
  它不被跳过、且对脏数据报错）。
- **`fact_observation` 的主键是业务自然键 `(subject_id, observed_at, channel)`**，
  `observation_id` 只是带唯一索引的普通列。`fact_event` **刻意没有自然键唯一约束**——
  同一分钟两笔消费是合法数据。
- **`synth` 的事件必须在全部业务列上唯一**（`fact_event` 无自然键约束，唯一性只能由生成器保证）：
  判重必须用**全部业务列**——只用时间戳会把合法的"同分钟两笔交易"误判为重复。
  改 `synth` 里任何事件类型时，跑 `pytest tests/test_synth_fidelity.py -k "duplicate or unique"`。
- **DuckDB 陷阱：持久化库上「同一显式事务内 `DELETE` → `INSERT` 同一键」会撞唯一索引**（实测复现）：
  ```
  session1: CREATE TABLE + CREATE UNIQUE INDEX + INSERT → close
  session2: BEGIN; DELETE; INSERT; COMMIT  → ❌ Duplicate key（索引条目没随 DELETE 失效）
  session3: DELETE; INSERT（均自动提交）    → ✅ OK
  ```
  只在**库是持久化文件、且索引由上一会话建好**时触发——同一会话内建表+索引+增删的测试**发现不了**。
  所以 `synth` 装载（`synth/generator.py::load_into_duckdb`）**刻意不用一个事务包住全部表**，
  改为每张表各自 `DELETE` / `INSERT` 自动提交：代价是失去跨表原子性
  （synth 产出的是可重建派生数据，失败响亮、重跑即修），换来"第二次运行不炸"。
  ⚠️ 给任何**会被 `synth` 重装的表**加唯一索引后，务必跑
  `pytest tests/test_synth_fidelity.py -k rerun_on_existing`（连跑两次 `generate_all` 的回归测试）。
- **`fact_metric_value` 的读取必须带版本过滤**：`WHERE valid_to IS NULL AND metric_version = <契约当前版本>`。
  裸查询会同时读到多个版本的行。

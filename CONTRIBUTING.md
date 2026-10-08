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

1. 在 `veriself/metrics/` 加一个 YAML（字段见契约第 2 节），一个文件一个指标；
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

## 发布

发布走 [`.github/workflows/release.yml`](.github/workflows/release.yml)，**Trusted Publishing（OIDC），不需要任何 token**。
触发方式是**在 GitHub 上发布一个 Release**。

### 一次性配置（只需做一次）

1. **PyPI 账号**：注册 [pypi.org](https://pypi.org)，并**开启 2FA**（不开启无法上传）。
2. **配置 pending publisher**（项目还不存在时用这个；Account → Publishing → GitHub）：

   | 字段 | 填什么 |
   | --- | --- |
   | PyPI Project Name | `veriself` |
   | Owner | `jiayezi` |
   | Repository name | `veriself` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   ⚠️ **环境名必须与工作流里 `publish` job 的 `environment: pypi` 一致**，否则 OIDC 校验失败。
   若这一栏留空，请把工作流里那一行删掉。

   ⚠️ pending publisher **不会预留包名**（PyPI 官方文档明确："does not create a project or reserve
   a project's name until it is actually used to publish"）。真正占住 `veriself` 这个名字的，
   只有**第一次成功发布**；期间若被别人抢注，这个 pending publisher 会失效。

### 每次发布的步骤

1. 改 `pyproject.toml` 的 `version`（例如 `0.1.0` → `0.1.1`），提交。
2. 打一个**与版本号一致**的 tag 并推送：`git tag v0.1.1 && git push origin v0.1.1`。
   （工作流会校验 tag 与 `pyproject` 版本一致，不一致直接失败——避免发错版本号。）
3. 在 GitHub 上基于该 tag **发布 Release**。
4. 工作流会依次：跑全量测试 → `uv build` → **把 wheel 装进干净 venv 跑 `synth` + `demo`** → 用 OIDC 发布到 PyPI。

### 本地预演（推荐先做一次）

Workflow 之外也可以本地演练，用 TestPyPI（**需要单独注册账号**，与 PyPI 不通用）：

```bash
uv build
uv publish --dry-run                                  # 只校验，不上传
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-你的TestPyPI令牌
```

然后装一遍验证（**这一步能抓到"装完不能用"这类问题**）：

```bash
uv venv /tmp/probe
uv pip install --python /tmp/probe/bin/python \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ veriself
/tmp/probe/bin/veriself metrics list          # 必须能列出 18 个指标
```

### 三条不可逆的红线

| 规则 | 说明 |
| --- | --- |
| 版本号 + 文件名不可重用 | 传过 `veriself-0.1.0` 后**永久**不能再用该版本号（删了也不行，会 `400 File already exists`），只能发新版本 |
| 只能 yank，不能撤回 | 出问题可以 yank（`pip install` 默认不再选它），但包仍在 PyPI 上可被显式安装 |
| 名字归一化 | PyPI 把 `veriself` / `VeriSelf` / `veri_self` / `veri-self` 视为冲突，占住一个即守住变体 |

> 发布相关的不变量由 `tests/test_packaging.py` 守着（包内契约是否进 wheel、
> 环境变量能否覆盖路径、入口点是否可导入）。改 `pyproject.toml` 的打包配置或 `config.py`
> 的路径常量后，务必跑 `uv run python -m pytest tests/test_packaging.py`。

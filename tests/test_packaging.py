"""发布相关的回归测试：`pip install` 之后包能不能用。

背景（实测）：`metrics/` 与 `semantic_models/` 在**仓库根、不在包内**，
所以只构建不配置的话，`pip install veriself` 拿到的 wheel 里没有契约，
安装后的 CLI 会以 exit 5 报 `指标契约目录不存在：…/site-packages/metrics`。

保证可用需要**两件事同时成立**，本文件各测一半：
1. `pyproject.toml` 的 `force-include` 把两个目录带进 wheel（测是否带、带去哪）；
2. `config._resolve_dir` 在源码目录不存在时**回退到包内默认值**（测三分优先级）。

⚠️ 这里断言的都尽量是**可观察行为**（目录是否被解析到、目标是否可导入），
不是"某字段等于某常量"式的同义反复。
"""

from __future__ import annotations

import importlib
import pathlib
import tomllib

import pytest

from veriself import config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- config 路径三分
# 注意：不使用 `tmp_path`（本机沙箱下系统临时目录不可用，见 AGENTS.md）。
# `_resolve_dir` 只做 `is_dir()` 判断，因此用"仓库里真实存在/不存在"的路径即可，
# 无需创建任何文件、也无需清理。


def test_resolve_dir_prefers_source_dir_when_present() -> None:
    """源码布局（目录存在）→ 用源码目录，**行为与改造前一致**。"""
    source = REPO_ROOT / "metrics"          # 真实存在
    packaged = REPO_ROOT / "_no_such_pkg" / "_defaults" / "metrics"

    assert source.is_dir()
    assert config._resolve_dir("VERISELF_TEST_UNSET_ENV", source, packaged) == source


def test_resolve_dir_falls_back_to_packaged_dir() -> None:
    """安装布局（源码目录不存在）→ 回退到**包内默认值**。

    可失败性：把 `_resolve_dir` 改回"直接返回 source_dir"，本测试即红——
    而那正是 `pip install` 用户拿到 exit 5 的原因。
    """
    missing = REPO_ROOT / "_no_such_site_packages" / "metrics"   # 模拟安装后的 PROJECT_ROOT/metrics
    packaged = REPO_ROOT / "_no_such_site_packages" / "veriself" / "_defaults" / "metrics"

    assert not missing.is_dir()
    assert config._resolve_dir("VERISELF_TEST_UNSET_ENV", missing, packaged) == packaged


def test_resolve_dir_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先级最高——用户可以用自己的契约目录（连源码目录存在时也优先）。"""
    source = REPO_ROOT / "metrics"          # 真实存在，用于证明"覆盖仍优先于源码目录"
    packaged = REPO_ROOT / "_no_such_pkg" / "_defaults" / "metrics"
    custom = REPO_ROOT / "_no_such_pkg" / "my-contracts"
    monkeypatch.setenv("VERISELF_TEST_OVERRIDE", str(custom))

    assert source.is_dir()
    assert config._resolve_dir("VERISELF_TEST_OVERRIDE", source, packaged) == custom


# ---------------------------------------------------------------- 打包元数据


def test_wheel_ships_contract_dirs() -> None:
    """`force-include` 必须把 `metrics/` 与 `semantic_models/` 带进 wheel。

    可失败性：删掉 `pyproject.toml` 里那一行 `force-include`，本测试即红。
    只测"声明存在"还不够，因此同时断言**源目录真实存在且内容非空**（改名/挪走也会红）。
    """
    wheel_cfg = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    force = wheel_cfg.get("force-include")
    assert force, "pyproject 缺少 tool.hatch.build.targets.wheel.force-include"

    for source_name, dest in force.items():
        assert dest.startswith("veriself/"), f"{source_name} 的目标不在包内：{dest}"
        source_dir = REPO_ROOT / source_name
        assert source_dir.is_dir(), f"force-include 的源目录不存在：{source_dir}"
        assert list(source_dir.glob("*.yml")), f"{source_dir} 下没有 yml"

    assert len(list((REPO_ROOT / "metrics").glob("*.yml"))) == 18, "指标契约应为 18 个"
    assert len(list((REPO_ROOT / "semantic_models").glob("*.yml"))) == 3, "语义模型应为 3 个"


def test_console_script_entry_point_is_importable() -> None:
    """`[project.scripts]` 指向的模块与属性必须真实存在。

    可失败性：改名漏改 `veriself.interfaces.cli:app`（或写错模块名）时本测试即红，
    而 `uv run veriself` 在源码布局下仍然能跑（走 .venv 的旧入口）——所以这条不是同义反复。
    """
    scripts = _pyproject()["project"]["scripts"]
    assert "veriself" in scripts, f"缺少 veriself 入口点：{scripts}"

    module_name, _, attribute = scripts["veriself"].partition(":")
    assert module_name and attribute, f"入口点格式应为 'module:attr'，实际 {scripts['veriself']!r}"

    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute)), f"{module_name} 里没有可调用对象 {attribute}"

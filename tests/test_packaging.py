"""发布相关的回归测试：`pip install` 之后包能不能用。

契约目录在包内（`veriself/metrics`、`veriself/semantic_models`），与 `schema.sql` 同一规则进 wheel。
本文件守两件事：

1. `config` 在未设置环境变量时指向包内目录，设置后让环境变量优先；
2. 实际打出的 wheel 里带有契约文件，且没有旧的 `veriself/_defaults/` 副本。

⚠️ 这里断言的都尽量是**可观察行为**（目录是否被解析到、wheel 里是否真有文件、入口点是否可导入），
不是"某字段等于某常量"式的同义反复。
"""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import tomllib
import zipfile

import pytest

from veriself import config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PKG_DIR = pathlib.Path(config.__file__).resolve().parent
#: 本文件自管的构建产物目录（gitignore 的 `data/` 下），测完即删。
_WHEEL_OUT = REPO_ROOT / "data" / "_packaging_wheel"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- config 路径
# 注意：不使用 `tmp_path`（本机沙箱下系统临时目录不可用，见 AGENTS.md）。


def test_contract_dirs_live_inside_package() -> None:
    """未设置环境变量时，契约目录就是包内目录，且内容数量与冻结清单一致。

    可失败性：把目录搬回仓库根、或改 `_resolve_dir` 不再指向 `_PKG_DIR`，本测试即红。
    """
    assert config.METRICS_DIR == PKG_DIR / "metrics"
    assert config.SEMANTIC_MODELS_DIR == PKG_DIR / "semantic_models"
    assert len(list(config.METRICS_DIR.glob("*.yml"))) == 18, "指标契约应为 18 个"
    assert len(list(config.SEMANTIC_MODELS_DIR.glob("*.yml"))) == 3, "语义模型应为 3 个"
    assert list((config.METRICS_DIR / "history").glob("*.yml")), "历史口径应仍在 metrics/history"


def test_resolve_dir_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先级最高——用户可以用自己的契约目录（包内目录存在时也优先）。"""
    custom = REPO_ROOT / "_no_such_pkg" / "my-contracts"
    monkeypatch.setenv("VERISELF_TEST_OVERRIDE", str(custom))

    assert config.METRICS_DIR.is_dir()
    assert config._resolve_dir("VERISELF_TEST_OVERRIDE", config.METRICS_DIR) == custom


# ---------------------------------------------------------------- 打包产物


def test_wheel_ships_contract_dirs() -> None:
    """wheel 必须带上包内契约，且不再有 `_defaults` 副本。

    可失败性：把 yml 移出包、或 hatch 排除 `*.yml`、或重新复制到 `_defaults/`，本测试即红。
    只断言 pyproject 声明发现不了「文件没进 wheel」。
    """
    if _WHEEL_OUT.exists():
        shutil.rmtree(_WHEEL_OUT)
    _WHEEL_OUT.mkdir(parents=True)
    try:
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(_WHEEL_OUT)],
            cwd=REPO_ROOT,
            check=True,
        )
        wheels = list(_WHEEL_OUT.glob("*.whl"))
        assert len(wheels) == 1, f"应只产出一个 wheel，实际 {wheels}"
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
        assert "veriself/metrics/subject.sleep_debt_7d.yml" in names
        assert "veriself/metrics/history/subject.sleep_debt_7d.yml" in names
        assert "veriself/semantic_models/observation.yml" in names
        assert not any(name.startswith("veriself/_defaults/") for name in names)
    finally:
        shutil.rmtree(_WHEEL_OUT, ignore_errors=True)


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

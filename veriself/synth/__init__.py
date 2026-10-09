"""合成数据生成器（``synth``）。

对外只暴露一个入口 :func:`generate_all`：按固定种子生成 2024-01-01..2026-09-30 的
个人纵向数据，写出 ``data/synth/*.parquet`` 中间产物与 ``data/warehouse.duckdb``
的 6 张来源表（``dim_date`` / ``dim_subject`` / ``dim_source`` / ``fact_subject_day`` /
``fact_observation`` / ``fact_event``）。

模块划分：

* :mod:`~veriself.synth.latent` —— 潜在结构与真值通道（睡眠/恢复/专注/情绪/压力）；
* :mod:`~veriself.synth.observations` —— 观测通道（日粒度 + 日内心率分解）；
* :mod:`~veriself.synth.dimensions` —— 维表（含 SCD2 的 ``dim_subject``）；
* :mod:`~veriself.synth.events` —— 外生窗口与 ``fact_event``；
* :mod:`~veriself.synth.generator` —— 编排与落盘。
"""

from __future__ import annotations

from veriself.synth.generator import generate_all

__all__ = ["generate_all"]

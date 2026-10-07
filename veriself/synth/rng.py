"""确定性随机数流。

所有随机性都必须来自 :data:`veriself.config.SYNTH_SEED` 派生出的独立流。
禁止使用 ``np.random`` 的全局状态（``np.random.seed`` / ``np.random.normal`` 等），
否则同一进程内多次生成无法保证逐元素相等。

设计：标签 -> CRC32 -> ``SeedSequence([SYNTH_SEED, stream])``，因此

* 同一标签在同一 ``SYNTH_SEED`` 下永远产生相同序列；
* 不同标签之间相互独立（不依赖调用顺序）；
* 不依赖 ``PYTHONHASHSEED``（不用内置 ``hash``）。
"""

from __future__ import annotations

import zlib

import numpy as np

from veriself import config

__all__ = ["ar1", "child_rng", "starts_of"]


def child_rng(tag: str) -> np.random.Generator:
    """按语义标签派生一个独立随机流。

    Args:
        tag: 流的语义标签，如 ``"latent.sleep"``。

    Returns:
        以 ``config.SYNTH_SEED`` 与标签派生的随机数生成器。
    """

    stream = zlib.crc32(tag.encode("utf-8")) & 0xFFFF_FFFF
    return np.random.default_rng([config.SYNTH_SEED, stream])


def ar1(innovations: np.ndarray, phi: float, x0: float = 0.0) -> np.ndarray:
    """一阶自回归过程 ``x[t] = phi * x[t-1] + e[t]``。

    Args:
        innovations: 新息序列 ``e``，形状 ``(n,)``。
        phi: 自回归系数，要求 ``|phi| < 1``。
        x0: 初值。

    Returns:
        长度与 ``innovations`` 相同的序列。
    """

    values = np.asarray(innovations, dtype=float)
    if values.ndim != 1:
        raise ValueError("innovations 必须是一维序列")
    if abs(phi) >= 1.0:
        raise ValueError("phi 必须满足 |phi| < 1")

    out = np.empty(values.shape[0], dtype=float)
    prev = float(x0)
    for i in range(values.shape[0]):
        prev = phi * prev + values[i]
        out[i] = prev
    return out


def starts_of(mask: np.ndarray) -> np.ndarray:
    """返回布尔掩码中每个连续 ``True`` 段的起始下标。"""

    values = np.asarray(mask, dtype=bool).astype(np.int8)
    diff = np.diff(np.concatenate(([0], values, [0])))
    return np.flatnonzero(diff == 1)

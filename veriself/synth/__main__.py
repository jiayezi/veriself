"""``python -m veriself.synth``：一键重建合成数据与数仓 6 张契约表。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from veriself.synth.generator import CONTRACT_TABLES, generate_all


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="python -m veriself.synth",
        description="生成 veriself 的合成个人纵向数据（确定性，固定种子）。",
    )
    parser.add_argument("--db-path", default=None, help="DuckDB 输出路径，默认 data/warehouse.duckdb")
    parser.add_argument("--synth-dir", default=None, help="Parquet 中间产物目录，默认 data/synth")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码（0 表示成功）。
    """

    args = _build_parser().parse_args(argv)
    counts = generate_all(
        db_path=Path(args.db_path) if args.db_path else None,
        synth_dir=Path(args.synth_dir) if args.synth_dir else None,
    )
    print("生成完成：")
    for name in CONTRACT_TABLES:
        print(f"  {name:<18} {counts[name]:>8} 行")
    for name, rows in counts.items():
        if name not in CONTRACT_TABLES:
            print(f"  {name:<18} {rows:>8} 行（中间产物）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

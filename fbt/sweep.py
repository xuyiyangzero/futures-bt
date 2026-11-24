"""参数扫描: 同一策略、同一份数据, 遍历 SimConfig 参数组合, 输出对比表 (用于定性比较, 不追求绝对收益)。

    grid = {"queue_ratio": [0, 0.5, 1], "inside_spread_mode": ["linear_bid_ask", "at_last"]}
    table = run_sweep("simple_mm:SimpleMM", "data", grid, params=dict(max_pos=5))
"""
from __future__ import annotations
import itertools
from dataclasses import replace
import pandas as pd
from .core import SimConfig
from .runner import run_backtest


def run_sweep(strategy, data, grid: dict, base_cfg: SimConfig | None = None, params=None,
              n_workers: int | None = None, out_csv: str | None = None, **kw) -> pd.DataFrame:
    base_cfg = base_cfg or SimConfig()
    keys, rows = list(grid), []
    for combo in itertools.product(*grid.values()):
        cfg = replace(base_cfg, **dict(zip(keys, combo)))
        res = run_backtest(strategy, data, params=params, cfg=cfg, n_workers=n_workers, **kw)
        row = dict(zip(keys, combo))
        row.update(n_orders=res.n_orders.sum(), n_trades=res.n_trades.sum(), volume=res.volume.sum(),
                   pnl_net=res.pnl_net.sum(), max_abs_pos=res.max_abs_pos.max(),
                   pnl_per_lot=(res.pnl_net.sum() / res.volume.sum()) if res.volume.sum() else 0.0)
        rows.append(row)
        print(row, flush=True)
    out = pd.DataFrame(rows)
    if out_csv: out.to_csv(out_csv, index=False)
    return out

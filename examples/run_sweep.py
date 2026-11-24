"""参数敏感性对比: 同一策略、同一数据, 只改撮合假设。  用法: python run_sweep.py [行情文件或文件夹]"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
from fbt import run_sweep, SimConfig

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    data = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "data")
    P = dict(size=1, max_pos=5)
    tables = []
    for name, grid in {
        "排队位置 queue_ratio": {"queue_ratio": [0.0, 0.25, 0.5, 0.75, 1.0]},
        "撤单推进 cancel_model": {"cancel_model": ["none", "min", "prob"]},
        "均价来源 avg_source": {"avg_source": ["turnover", "last"]},
        "买卖之间的分配 inside_spread_mode": {"inside_spread_mode": ["linear_bid_ask", "last_anchored", "at_last", "nearest"]},
    }.items():
        t = run_sweep("simple_mm:SimpleMM", data, grid, base_cfg=SimConfig(), params=P, n_workers=1)
        t.insert(0, "group", name); tables.append(t)
    out = pd.concat(tables)
    out.to_csv(os.path.join(here, "sweep_result.csv"), index=False)
    pd.set_option("display.width", 220)
    print(out.to_string(index=False))

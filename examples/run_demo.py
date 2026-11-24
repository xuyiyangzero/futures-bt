"""用法:  python run_demo.py [行情文件或文件夹]     默认 ../data  (里面放 snap_*.xlsx / csv / parquet)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
from fbt import run_backtest, SimConfig

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    data = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "data")
    cfg = SimConfig(submit_latency_ms=5, cancel_latency_ms=5, timer_ms=10,
                    queue_ratio=1.0,          # 1=队尾 0=队首
                    cancel_model="prob", fee_per_lot=0.0)
    res = run_backtest("simple_mm:SimpleMM", data=data,
                       params=dict(half_spread_ticks=1, size=1, max_pos=5),
                       cfg=cfg, n_workers=None, out_dir=os.path.join(here, "out"))
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(res.T if len(res) == 1 else res)
    print("\n总 PnL(net):", res.pnl_net.sum())
